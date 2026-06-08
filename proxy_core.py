#!/usr/bin/env python3
# coding: utf-8
"""Core Feishu Project status transition logic for the proxy service."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


STATUS_MAP = {
    "待办": {"label": "待办", "state_key": "Not started"},
    "not started": {"label": "待办", "state_key": "Not started"},
    "进行中": {"label": "进行中", "state_key": "In Progress"},
    "in progress": {"label": "进行中", "state_key": "In Progress"},
    "修改中": {"label": "修改中", "state_key": "4m5jzvqqy"},
    "验收中": {"label": "验收中", "state_key": "bcoksgha8"},
    "资产验收通过": {"label": "资产验收通过", "state_key": "itl0cpgq4"},
    "已完成": {"label": "已完成", "state_key": "0gmbrd0o7"},
    "done": {"label": "已完成", "state_key": "0gmbrd0o7"},
}

DEFAULT_BASE_URL = "https://project.feishu.cn/open_api"
DEFAULT_PROJECT_KEY = "rzoecp"
DEFAULT_WORK_ITEM_TYPE = "69ca097070c61cbef714a50f"
PAGE_INTERVAL_SECONDS = 0.2


class FeishuError(RuntimeError):
    """Raised when a Feishu Project API call or workflow step fails."""


def normalize_base_url(value: str) -> str:
    text = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    if text.endswith("/open_api"):
        return text
    return f"{text}/open_api"


def normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    return text


def clean_query_line(line: str) -> str:
    text = str(line or "").strip()
    if re.fullmatch(r"\d+", text):
        return text
    text = re.sub(r"^\s*(?:[-*]\s*|\d+[.)、]\s*)", "", text)
    return text.strip()


def iter_strings(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
        return
    if isinstance(value, (int, float, bool)):
        yield str(value)
        return
    if isinstance(value, dict):
        for key in ("label", "name", "title", "text", "value", "id", "state_key"):
            if key in value:
                yield from iter_strings(value[key])
        for nested in value.values():
            yield from iter_strings(nested)
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def field_entries(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = item.get("fields") or []
    if isinstance(fields, dict):
        return [
            {"field_key": key, "field_alias": key, "field_value": value}
            for key, value in fields.items()
        ]
    if isinstance(fields, list):
        return [field for field in fields if isinstance(field, dict)]
    return []


def field_text_by_alias(item: Dict[str, Any], aliases: Sequence[str]) -> str:
    alias_set = {normalize(alias) for alias in aliases}
    for field in field_entries(item):
        labels = [
            field.get("field_alias"),
            field.get("alias"),
            field.get("name"),
            field.get("label"),
            field.get("field_key"),
            field.get("key"),
        ]
        if any(normalize(label) in alias_set for label in labels if label):
            return " ".join(iter_strings(field.get("field_value", field.get("value"))))
    return ""


def item_title(item: Dict[str, Any]) -> str:
    for key in ("name", "title", "work_item_name"):
        if item.get(key):
            return str(item[key]).strip()
    text = field_text_by_alias(item, ["name", "标题", "名称", "任务名称"])
    return text.strip()


def item_asset_name(item: Dict[str, Any]) -> str:
    return field_text_by_alias(
        item,
        ["资产名", "asset_name", "Asset Name", "英文名", "英文名(唯一)", "中文名", "中文名(唯一)"],
    ).strip()


def item_status_value(item: Dict[str, Any]) -> str:
    status_val = item.get("work_item_status")
    if isinstance(status_val, dict):
        return str(
            status_val.get("state_key")
            or status_val.get("id")
            or status_val.get("label")
            or ""
        )
    if isinstance(status_val, str):
        return status_val
    for field in field_entries(item):
        if field.get("field_key") == "work_item_status":
            val = field.get("field_value")
            if isinstance(val, dict):
                return str(val.get("state_key") or val.get("id") or val.get("label") or "")
            if isinstance(val, str):
                return val
    return ""


def item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("work_item_id") or ""),
        "name": item_title(item),
        "asset_name": item_asset_name(item),
        "status": item_status_value(item),
    }


def item_exact_keys(item: Dict[str, Any]) -> List[str]:
    values = [item_title(item), item_asset_name(item), str(item.get("id") or "")]
    return [normalize(value) for value in values if normalize(value)]


def item_corpus(item: Dict[str, Any]) -> str:
    values = set(iter_strings(item))
    values.update([item_title(item), item_asset_name(item), str(item.get("id") or "")])
    return " ".join(normalize(value) for value in values if value)


def match_score(query: str, item: Dict[str, Any]) -> int:
    q = normalize(query)
    if not q:
        return 0
    if q in item_exact_keys(item):
        return 100
    title = normalize(item_title(item))
    asset = normalize(item_asset_name(item))
    if q and (q in title or q in asset):
        return 80
    if len(q) >= 4 and q in item_corpus(item):
        return 40
    return 0


def err_code(data: Dict[str, Any]) -> Optional[Any]:
    if not isinstance(data, dict):
        return None
    if data.get("err_code") not in (None, 0):
        return data.get("err_code")
    error = data.get("error") or {}
    if isinstance(error, dict) and error.get("code") not in (None, 0):
        return error.get("code")
    err = data.get("err") or {}
    if isinstance(err, dict) and err.get("code") not in (None, 0):
        return err.get("code")
    return 0


def http_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {exc.code} {url}: {raw[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"request failed {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"non-JSON response {url}: {raw[:1000]}") from exc


class FeishuProjectClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        project_key: str = DEFAULT_PROJECT_KEY,
        user_key: str = "",
        plugin_token: str = "",
        plugin_id: str = "",
        plugin_secret: str = "",
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.project_key = str(project_key or DEFAULT_PROJECT_KEY).strip()
        self.user_key = str(user_key or "").strip()
        self.plugin_token = str(plugin_token or "").strip()
        self.plugin_id = str(plugin_id or "").strip()
        self.plugin_secret = str(plugin_secret or "").strip()
        self._token_expire_at = 0.0

    def get_plugin_token(self) -> str:
        if self.plugin_token and not self.plugin_id:
            return self.plugin_token
        now = time.time()
        if self.plugin_token and now < self._token_expire_at - 300:
            return self.plugin_token
        if not self.plugin_id or not self.plugin_secret:
            raise FeishuError(
                "missing PROJECT_USER_PLUGIN_TOKEN or PROJECT_PLUGIN_TOKEN or PROJECT_PLUGIN_ID/PROJECT_PLUGIN_SECRET"
            )
        data = http_json(
            "POST",
            f"{self.base_url}/authen/plugin_token",
            {"Content-Type": "application/json"},
            {"plugin_id": self.plugin_id, "plugin_secret": self.plugin_secret, "type": 0},
        )
        if err_code(data) not in (None, 0):
            raise FeishuError(f"get plugin token failed: {json.dumps(data, ensure_ascii=False)}")
        self.plugin_token = str(data.get("data", {}).get("token") or "")
        self._token_expire_at = now + float(data.get("data", {}).get("expire_time") or 7200)
        if not self.plugin_token:
            raise FeishuError(f"plugin token missing in response: {json.dumps(data, ensure_ascii=False)}")
        return self.plugin_token

    def headers(self) -> Dict[str, str]:
        token = self.get_plugin_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-PLUGIN-TOKEN": token,
        }
        if self.user_key:
            headers["X-USER-KEY"] = self.user_key
        return headers

    def api(self, method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = http_json(method, url, self.headers(), payload)
        if err_code(data) not in (None, 0):
            raise FeishuError(json.dumps(data, ensure_ascii=False))
        return data

    def list_work_items(self, work_item_type: str, page_size: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_num = 1
        while True:
            data = self.api(
                "POST",
                f"{self.project_key}/work_item/filter",
                {
                    "work_item_type_keys": [work_item_type],
                    "page_size": page_size,
                    "page_num": page_num,
                },
            )
            page_items = extract_items(data)
            if not page_items:
                break
            items.extend(page_items)
            if len(page_items) < page_size:
                break
            page_num += 1
            time.sleep(PAGE_INTERVAL_SECONDS)
        return items

    def query_workflow(self, work_item_type: str, work_item_id: str) -> List[Dict[str, Any]]:
        data = self.api(
            "POST",
            f"{self.project_key}/work_item/{work_item_type}/{work_item_id}/workflow/query",
            {"flow_type": 1},
        )
        payload = data.get("data") or {}
        if isinstance(payload, dict):
            return payload.get("connections") or []
        return []

    def transition(self, work_item_type: str, work_item_id: str, transition_id: str) -> Dict[str, Any]:
        return self.api(
            "POST",
            f"{self.project_key}/workflow/{work_item_type}/{work_item_id}/node/state_change",
            {"transition_id": str(transition_id)},
        )


def extract_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = data.get("data")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "work_items", "list", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def find_transition_path(
    connections: Sequence[Dict[str, Any]],
    source_state_key: str,
    target_state_key: str,
) -> List[str]:
    if not connections:
        return []
    if str(source_state_key) == str(target_state_key):
        return []
    queue: List[Tuple[str, List[str]]] = [(str(source_state_key), [])]
    visited = {str(source_state_key)}
    while queue:
        state_key, path = queue.pop(0)
        for conn in connections:
            src = str(conn.get("source_state_key", ""))
            dst = str(conn.get("target_state_key", ""))
            transition_id = str(conn.get("transition_id") or "")
            if src != state_key or not dst or dst in visited:
                continue
            next_path = path + ([transition_id] if transition_id else [])
            if dst == str(target_state_key):
                return next_path
            visited.add(dst)
            queue.append((dst, next_path))
    return []


def load_queries(names: Sequence[str]) -> List[str]:
    deduped: List[str] = []
    seen = set()
    for name in names:
        cleaned = clean_query_line(name)
        key = normalize(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            deduped.append(cleaned)
    return deduped


def resolve_target(target: str) -> Dict[str, str]:
    key = normalize(target)
    if key in STATUS_MAP:
        return STATUS_MAP[key]
    for info in STATUS_MAP.values():
        if normalize(info["state_key"]) == key or normalize(info["label"]) == key:
            return info
    raw = str(target or "").strip()
    if raw:
        return {"label": raw, "state_key": raw}
    raise FeishuError(f"unsupported target status: {target}")


def canonical_state_key(value: str) -> str:
    key = normalize(value)
    if not key:
        return ""
    if key in STATUS_MAP:
        return STATUS_MAP[key]["state_key"]
    for info in STATUS_MAP.values():
        if normalize(info["state_key"]) == key or normalize(info["label"]) == key:
            return info["state_key"]
    return str(value or "")


def match_queries(queries: Sequence[str], items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for query in queries:
        scored = [(match_score(query, item), item) for item in items]
        scored = [(score, item) for score, item in scored if score > 0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            results.append({"query": query, "status": "not_found", "matches": []})
            continue
        best_score = scored[0][0]
        best = [item for score, item in scored if score == best_score]
        status = "matched" if len(best) == 1 else "ambiguous"
        results.append(
            {
                "query": query,
                "status": status,
                "score": best_score,
                "matches": [item_summary(item) for item in best[:10]],
            }
        )
    return results


def build_actions(
    client: FeishuProjectClient,
    work_item_type: str,
    target: Dict[str, str],
    matched: Sequence[Dict[str, Any]],
    item_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for result in matched:
        if result["status"] != "matched":
            continue
        item_id = str(result["matches"][0]["id"])
        item = item_by_id.get(item_id)
        if not item:
            continue
        current_state_raw = item_status_value(item)
        current_state = canonical_state_key(current_state_raw)
        action = {
            "id": item_id,
            "name": item_title(item),
            "asset_name": item_asset_name(item),
            "current_state": current_state,
            "current_state_raw": current_state_raw,
            "target_state": target["state_key"],
            "target_label": target["label"],
            "transition_ids": [],
            "status": "pending",
        }
        if current_state == target["state_key"]:
            action["status"] = "skipped_same_state"
            actions.append(action)
            continue
        if not current_state:
            action["status"] = "missing_current_state"
            actions.append(action)
            continue
        connections = client.query_workflow(work_item_type, item_id)
        path = find_transition_path(connections, current_state, target["state_key"])
        action["transition_ids"] = path
        action["status"] = "ready" if path else "no_transition_path"
        if not path:
            action["workflow_edges"] = [
                f"{conn.get('source_state_key', '?')}->{conn.get('target_state_key', '?')}"
                for conn in connections
            ]
        actions.append(action)
    return actions


def summarize_output(matches: Sequence[Dict[str, Any]], actions: Sequence[Dict[str, Any]], updated: int) -> Dict[str, int]:
    return {
        "matched": sum(1 for item in matches if item["status"] == "matched"),
        "ambiguous": sum(1 for item in matches if item["status"] == "ambiguous"),
        "not_found": sum(1 for item in matches if item["status"] == "not_found"),
        "ready": sum(1 for item in actions if item["status"] == "ready"),
        "updated": updated,
        "skipped": sum(1 for item in actions if str(item["status"]).startswith("skipped")),
        "blocked": sum(
            1 for item in actions if item["status"] in {"missing_current_state", "no_transition_path"}
        ),
    }


def preview_or_execute(
    *,
    client: FeishuProjectClient,
    target_name: str,
    queries: Sequence[str],
    work_item_type: str = DEFAULT_WORK_ITEM_TYPE,
    page_size: int = 100,
    execute: bool = False,
) -> Dict[str, Any]:
    deduped_queries = load_queries(queries)
    if not deduped_queries:
        raise FeishuError("provide at least one query, title, asset name, or work item id")

    target = resolve_target(target_name)
    items = client.list_work_items(work_item_type, page_size)
    item_by_id = {str(item.get("id") or ""): item for item in items if item.get("id")}
    matches = match_queries(deduped_queries, items)
    actions = build_actions(client, work_item_type, target, matches, item_by_id)

    updated = 0
    if execute:
        for action in actions:
            if action["status"] != "ready":
                continue
            for transition_id in action["transition_ids"]:
                client.transition(work_item_type, action["id"], transition_id)
            action["status"] = "updated"
            updated += 1

    return {
        "dry_run": not execute,
        "target": target,
        "query_count": len(deduped_queries),
        "work_item_count": len(items),
        "matches": matches,
        "actions": actions,
        "summary": summarize_output(matches, actions, updated),
    }
