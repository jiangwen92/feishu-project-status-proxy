#!/usr/bin/env python3
# coding: utf-8
"""CLI client for the shared Feishu Project status proxy."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_PROXY_BASE_URL = "http://127.0.0.1:8787"


def env_text(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def clean_query_line(line: str) -> str:
    text = str(line or "").strip()
    if re.fullmatch(r"\d+", text):
        return text
    text = re.sub(r"^\s*(?:[-*]\s*|\d+[.)、]\s*)", "", text)
    return text.strip()


def collect_queries(names: Sequence[str], names_file: str) -> List[str]:
    queries: List[str] = []
    for name in names:
        cleaned = clean_query_line(name)
        if cleaned:
            queries.append(cleaned)
    if names_file:
        with open(names_file, "r", encoding="utf-8") as handle:
            for line in handle:
                cleaned = clean_query_line(line)
                if cleaned:
                    queries.append(cleaned)
    deduped: List[str] = []
    seen = set()
    for query in queries:
        key = query.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


def request_json(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base_url = env_text("FEISHU_STATUS_PROXY_BASE_URL", DEFAULT_PROXY_BASE_URL).rstrip("/")
    shared_secret = env_text("FEISHU_STATUS_PROXY_SHARED_SECRET")
    headers = {"Content-Type": "application/json"}
    if shared_secret:
        headers["Authorization"] = f"Bearer {shared_secret}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc}") from exc
    return json.loads(raw)


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_status_payload(args: argparse.Namespace, execute: bool) -> Dict[str, Any]:
    user_key = env_text("FEISHU_PROJECT_USER_KEY")
    payload: Dict[str, Any] = {
        "target": args.target,
        "queries": collect_queries(args.name, args.names_file),
    }
    if args.work_item_type:
        payload["work_item_type"] = args.work_item_type
    if args.project_key:
        payload["project_key"] = args.project_key
    if args.user_key or user_key:
        payload["project_user_key"] = args.user_key or user_key
    if args.page_size:
        payload["page_size"] = args.page_size
    if execute:
        payload["confirm_execute"] = True
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Fetch proxy health and public config")

    parse_view = subparsers.add_parser("parse-view-link", help="Parse a Feishu Project view link")
    parse_view.add_argument("view_link")

    for name in ("preview", "execute"):
        sub = subparsers.add_parser(name, help=f"{name} a status transition batch")
        sub.add_argument("--target", required=True, help="目标状态，例如：修改中、验收中、进行中")
        sub.add_argument("--name", action="append", default=[], help="任务标题、资产名或工作项ID，可重复")
        sub.add_argument("--names-file", default="", help="一行一个任务标题、资产名或工作项ID")
        sub.add_argument("--work-item-type", default="", help="工作项类型，例如 issue、story 或资产子任务类型key")
        sub.add_argument("--project-key", default="", help="项目key，默认读代理服务配置")
        sub.add_argument("--user-key", default="", help="仅当代理允许 caller user key 时生效")
        sub.add_argument("--page-size", type=int, default=100)

    args = parser.parse_args(argv)

    if args.command == "health":
        print_json(request_json("GET", "/health"))
        return 0
    if args.command == "parse-view-link":
        print_json(request_json("POST", "/parse-view-link", {"view_link": args.view_link}))
        return 0
    if args.command == "preview":
        print_json(request_json("POST", "/preview-status", build_status_payload(args, execute=False)))
        return 0
    if args.command == "execute":
        print_json(request_json("POST", "/execute-status", build_status_payload(args, execute=True)))
        return 0

    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
