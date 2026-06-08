#!/usr/bin/env python3
# coding: utf-8
"""HTTP proxy for shared Feishu Project status preview and execution."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from proxy_core import (
    DEFAULT_BASE_URL,
    DEFAULT_PROJECT_KEY,
    DEFAULT_WORK_ITEM_TYPE,
    FeishuError,
    FeishuProjectClient,
    clean_query_line,
    preview_or_execute,
)
from view_cache_resolver import ViewResolutionError, resolve_view_items
from view_links import DEFAULT_ASSET_SUBTASK_TYPE, parse_link


VERSION = "2026-06-08.3"
DEFAULT_PORT = 8787


class HttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def env_text(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def first_text(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def public_env_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "project_key_default": env_text("PROJECT_KEY", DEFAULT_PROJECT_KEY),
        "base_url_default": env_text("PROJECT_BASE_URL", DEFAULT_BASE_URL),
        "work_item_type_default": env_text("PROJECT_DEFAULT_WORK_ITEM_TYPE", DEFAULT_WORK_ITEM_TYPE),
        "asset_subtask_type_default": env_text(
            "PROJECT_ASSET_SUBTASK_TYPE",
            DEFAULT_ASSET_SUBTASK_TYPE,
        ),
        "has_project_user_plugin_token": bool(env_text("PROJECT_USER_PLUGIN_TOKEN")),
        "has_project_plugin_token": bool(env_text("PROJECT_PLUGIN_TOKEN")),
        "has_project_plugin_id_secret": bool(
            env_text("PROJECT_PLUGIN_ID") and env_text("PROJECT_PLUGIN_SECRET")
        ),
        "has_project_user_key": bool(env_text("PROJECT_USER_KEY")),
        "allow_caller_user_key": env_bool("ALLOW_CALLER_USER_KEY", False),
        "require_caller_user_key": env_bool("REQUIRE_CALLER_USER_KEY", False),
        "allow_execute": env_bool("ALLOW_EXECUTE", True),
        "has_relay_shared_secret": bool(env_text("RELAY_SHARED_SECRET")),
        "audit_log_path": resolve_audit_log_path(),
    }


def send_json(handler: BaseHTTPRequestHandler, payload: Dict[str, Any], status_code: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(400, f"invalid JSON body: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    raise HttpError(400, "JSON body must be an object")


def normalize_request_shape(
    request_method: str,
    request_path: str,
    request_headers: Dict[str, str],
    payload: Dict[str, Any],
) -> Tuple[str, str, Dict[str, str], Dict[str, Any]]:
    method = request_method
    path = request_path
    merged_headers = dict(request_headers)
    body = payload

    wrapped_method = first_text(payload.get("method"))
    wrapped_path = first_text(payload.get("path"))
    wrapped_headers = payload.get("headers")
    wrapped_body = payload.get("body")

    if wrapped_method:
        method = wrapped_method.upper()
    if wrapped_path:
        path = wrapped_path
    if isinstance(wrapped_headers, dict):
        for key, value in wrapped_headers.items():
            if value is not None:
                merged_headers[str(key)] = str(value)
    if isinstance(wrapped_body, dict):
        body = wrapped_body
    return method, path, merged_headers, body


def assert_relay_auth(headers: Dict[str, str]) -> None:
    expected = env_text("RELAY_SHARED_SECRET")
    if not expected:
        return
    bearer = first_text(headers.get("Authorization"), headers.get("authorization"))
    relay_token = first_text(
        headers.get("X-Relay-Token"),
        headers.get("x-relay-token"),
    )
    if bearer.lower().startswith("bearer "):
        bearer = bearer[7:].strip()
    else:
        bearer = ""
    if bearer == expected or relay_token == expected:
        return
    raise HttpError(401, "missing or invalid relay shared secret")


def collect_queries(body: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("queries", "names", "items", "ids", "name"):
        raw = body.get(key)
        if isinstance(raw, list):
            for item in raw:
                cleaned = clean_query_line(str(item or ""))
                if cleaned:
                    values.append(cleaned)
        elif isinstance(raw, str):
            cleaned = clean_query_line(raw)
            if cleaned:
                values.append(cleaned)
    for key in ("names_text", "queries_text"):
        raw_text = body.get(key)
        if isinstance(raw_text, str):
            for line in raw_text.splitlines():
                cleaned = clean_query_line(line)
                if cleaned:
                    values.append(cleaned)
    return values


def resolve_audit_log_path() -> str:
    raw = env_text("AUDIT_LOG_PATH")
    if not raw:
        return ""
    if os.path.isabs(raw):
        return raw
    return os.path.join(os.path.dirname(__file__), raw)


def append_audit_log(
    *,
    event_type: str,
    handler: BaseHTTPRequestHandler,
    body: Dict[str, Any],
    headers: Dict[str, str],
    status_code: int,
    result: Optional[Dict[str, Any]] = None,
    error: str = "",
) -> None:
    path = resolve_audit_log_path()
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    queries = collect_queries(body)
    target = first_text(
        body.get("target"),
        body.get("target_status"),
        ((result or {}).get("target") or {}).get("label"),
    )
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "status_code": status_code,
        "remote_addr": (handler.client_address or ("", 0))[0],
        "acting_user_key": first_text(
            (result or {}).get("acting_user_key"),
            body.get("project_user_key"),
            body.get("user_key"),
            headers.get("X-Project-User-Key"),
            headers.get("x-project-user-key"),
        ),
        "project_key": first_text(
            (result or {}).get("project_key"),
            body.get("project_key"),
            env_text("PROJECT_KEY", DEFAULT_PROJECT_KEY),
        ),
        "work_item_type": first_text(
            (result or {}).get("work_item_type"),
            body.get("work_item_type"),
            body.get("work_item_type_key"),
            env_text("PROJECT_DEFAULT_WORK_ITEM_TYPE", DEFAULT_WORK_ITEM_TYPE),
        ),
        "target": target,
        "view_link": first_text(
            body.get("view_link"),
            body.get("url"),
            ((result or {}).get("resolved_view") or {}).get("view_link"),
        ),
        "query_count": len(queries),
        "queries_preview": queries[:20],
        "summary": (result or {}).get("summary"),
        "error": error,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def resolve_user_key(body: Dict[str, Any], headers: Dict[str, str]) -> str:
    default_user_key = env_text("PROJECT_USER_KEY")
    allow_caller = env_bool("ALLOW_CALLER_USER_KEY", False)
    require_caller = env_bool("REQUIRE_CALLER_USER_KEY", False)
    caller_user_key = first_text(
        body.get("project_user_key"),
        body.get("user_key"),
        headers.get("X-Project-User-Key"),
        headers.get("x-project-user-key"),
    )
    if require_caller and not allow_caller:
        raise HttpError(500, "REQUIRE_CALLER_USER_KEY=1 requires ALLOW_CALLER_USER_KEY=1")
    if not allow_caller:
        return default_user_key
    if require_caller and not caller_user_key:
        raise HttpError(
            400,
            "missing project_user_key or X-Project-User-Key; this proxy requires caller-supplied user keys",
        )
    return first_text(caller_user_key, default_user_key)


def build_client(body: Dict[str, Any], headers: Dict[str, str]) -> Tuple[FeishuProjectClient, str]:
    project_key = first_text(body.get("project_key"), env_text("PROJECT_KEY", DEFAULT_PROJECT_KEY))
    base_url = first_text(body.get("base_url"), body.get("project_base_url"), env_text("PROJECT_BASE_URL", DEFAULT_BASE_URL))
    work_item_type = first_text(
        body.get("work_item_type"),
        body.get("work_item_type_key"),
        env_text("PROJECT_DEFAULT_WORK_ITEM_TYPE", DEFAULT_WORK_ITEM_TYPE),
    )
    user_plugin_token = env_text("PROJECT_USER_PLUGIN_TOKEN")
    if user_plugin_token:
        client = FeishuProjectClient(
            base_url=base_url,
            project_key=project_key,
            plugin_token=user_plugin_token,
            user_key="",
        )
        return client, work_item_type

    plugin_token = env_text("PROJECT_PLUGIN_TOKEN")
    plugin_id = env_text("PROJECT_PLUGIN_ID")
    plugin_secret = env_text("PROJECT_PLUGIN_SECRET")
    user_key = resolve_user_key(body, headers)

    if plugin_token and not user_key:
        raise HttpError(
            500,
            "PROJECT_PLUGIN_TOKEN is configured but no acting user key is available; set PROJECT_USER_KEY or require callers to pass project_user_key",
        )
    if plugin_id and plugin_secret and not user_key:
        raise HttpError(
            500,
            "PROJECT_PLUGIN_ID/PROJECT_PLUGIN_SECRET are configured but no acting user key is available; set PROJECT_USER_KEY or require callers to pass project_user_key",
        )
    if not user_plugin_token and not plugin_token and not (plugin_id and plugin_secret):
        raise HttpError(
            500,
            "server auth is not configured; set PROJECT_USER_PLUGIN_TOKEN or PROJECT_PLUGIN_TOKEN or PROJECT_PLUGIN_ID/PROJECT_PLUGIN_SECRET",
        )

    client = FeishuProjectClient(
        base_url=base_url,
        project_key=project_key,
        user_key=user_key,
        plugin_token=plugin_token,
        plugin_id=plugin_id,
        plugin_secret=plugin_secret,
    )
    return client, work_item_type


def preview_or_execute_response(body: Dict[str, Any], headers: Dict[str, str], execute: bool) -> Dict[str, Any]:
    resolved_view: Optional[Dict[str, Any]] = None
    queries = collect_queries(body)
    target = first_text(body.get("target"), body.get("target_status"))
    if not target:
        raise HttpError(400, "missing target or target_status")
    if not queries:
        view_link = first_text(body.get("view_link"), body.get("url"))
        if view_link:
            resolved_view = resolve_view_items(
                view_link,
                asset_subtask_type=env_text("PROJECT_ASSET_SUBTASK_TYPE", DEFAULT_ASSET_SUBTASK_TYPE),
            )
            queries = list(resolved_view.get("queries") or [])
            if queries and not first_text(body.get("work_item_type"), body.get("work_item_type_key")):
                body = dict(body)
                body["work_item_type"] = first_text(resolved_view.get("work_item_type_key"))
    if not queries:
        raise HttpError(400, "missing queries, names, ids, names_text, or view_link")
    if execute and not env_bool("ALLOW_EXECUTE", True):
        raise HttpError(403, "execute is disabled on this proxy")
    if execute and body.get("confirm_execute") is not True:
        raise HttpError(400, "execute requires confirm_execute=true")

    client, work_item_type = build_client(body, headers)
    page_size = int(body.get("page_size") or 100)
    result = preview_or_execute(
        client=client,
        target_name=target,
        queries=queries,
        work_item_type=work_item_type,
        page_size=page_size,
        execute=execute,
    )
    result["project_key"] = client.project_key
    result["work_item_type"] = work_item_type
    result["acting_user_key"] = client.user_key
    if resolved_view:
        result["resolved_view"] = {
            "view_link": first_text(resolved_view.get("view_link")),
            "source": first_text(resolved_view.get("source")),
            "item_count": int(resolved_view.get("item_count") or 0),
            "work_item_type_key": first_text(resolved_view.get("work_item_type_key")),
            "cache_request_ts_ms": int(resolved_view.get("cache_request_ts_ms") or 0),
            "candidate_count": int(resolved_view.get("candidate_count") or 0),
        }
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "FeishuProjectStatusProxy/1.0"

    def do_GET(self) -> None:
        try:
            if self.path in {"/", "/health"}:
                send_json(self, {"ok": True, "version": VERSION, "data": public_env_state()})
                return
            if self.path == "/config":
                send_json(self, {"ok": True, "version": VERSION, "data": public_env_state()})
                return
            if self.path == "/openapi.json":
                openapi_path = os.path.join(os.path.dirname(__file__), "openapi.json")
                with open(openapi_path, "r", encoding="utf-8") as handle:
                    send_json(self, json.load(handle))
                return
            raise HttpError(404, "not found")
        except HttpError as exc:
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, exc.status_code)
        except Exception as exc:  # pragma: no cover
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = self.path
        merged_headers = {key: value for key, value in self.headers.items()}
        body: Dict[str, Any] = {}
        try:
            payload = read_json_body(self)
            method, path, merged_headers, body = normalize_request_shape(
                self.command,
                self.path,
                merged_headers,
                payload,
            )

            if method == "GET" and path in {"/", "/health"}:
                send_json(self, {"ok": True, "version": VERSION, "data": public_env_state()})
                return

            if path == "/parse-view-link":
                view_link = first_text(body.get("view_link"), body.get("url"))
                if not view_link:
                    raise HttpError(400, "missing view_link or url")
                asset_subtask_type = env_text("PROJECT_ASSET_SUBTASK_TYPE", DEFAULT_ASSET_SUBTASK_TYPE)
                send_json(
                    self,
                    {
                        "ok": True,
                        "version": VERSION,
                        "data": parse_link(view_link, asset_subtask_type=asset_subtask_type),
                    },
                )
                return

            if path == "/resolve-view-items":
                assert_relay_auth(merged_headers)
                view_link = first_text(body.get("view_link"), body.get("url"))
                if not view_link:
                    raise HttpError(400, "missing view_link or url")
                send_json(
                    self,
                    {
                        "ok": True,
                        "version": VERSION,
                        "data": resolve_view_items(
                            view_link,
                            asset_subtask_type=env_text(
                                "PROJECT_ASSET_SUBTASK_TYPE",
                                DEFAULT_ASSET_SUBTASK_TYPE,
                            ),
                        ),
                    },
                )
                return

            if path == "/preview-status":
                assert_relay_auth(merged_headers)
                result = preview_or_execute_response(body, merged_headers, execute=False)
                append_audit_log(
                    event_type="preview_status",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=200,
                    result=result,
                )
                send_json(self, {"ok": True, "version": VERSION, "data": result})
                return

            if path == "/execute-status":
                assert_relay_auth(merged_headers)
                result = preview_or_execute_response(body, merged_headers, execute=True)
                append_audit_log(
                    event_type="execute_status",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=200,
                    result=result,
                )
                send_json(self, {"ok": True, "version": VERSION, "data": result})
                return

            raise HttpError(404, "not found")
        except HttpError as exc:
            if path in {"/preview-status", "/execute-status"}:
                append_audit_log(
                    event_type=f"{path.lstrip('/').replace('-', '_')}_error",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=exc.status_code,
                    error=str(exc),
                )
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, exc.status_code)
        except FeishuError as exc:
            if path in {"/preview-status", "/execute-status"}:
                append_audit_log(
                    event_type=f"{path.lstrip('/').replace('-', '_')}_error",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=502,
                    error=str(exc),
                )
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, 502)
        except ViewResolutionError as exc:
            if path in {"/preview-status", "/execute-status"}:
                append_audit_log(
                    event_type=f"{path.lstrip('/').replace('-', '_')}_error",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=400,
                    error=str(exc),
                )
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            if path in {"/preview-status", "/execute-status"}:
                append_audit_log(
                    event_type=f"{path.lstrip('/').replace('-', '_')}_error",
                    handler=self,
                    body=body,
                    headers=merged_headers,
                    status_code=500,
                    error=str(exc),
                )
            send_json(self, {"ok": False, "version": VERSION, "error": str(exc)}, 500)

    def log_message(self, format: str, *args: object) -> None:
        message = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            format % args,
        )
        try:
            os.write(2, message.encode("utf-8", errors="replace"))
        except OSError:
            pass


def main() -> int:
    port = int(env_text("PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"feishu project status proxy listening on {port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
