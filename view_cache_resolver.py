#!/usr/bin/env python3
# coding: utf-8
"""Resolve Feishu Project saved-view rows from local browser cache."""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from view_links import DEFAULT_ASSET_SUBTASK_TYPE, parse_link


RUNTIME_AND_STRUCTURE_URL = "https://project.feishu.cn/goapi/v5/search/general/runtime_and_structure"
DEFAULT_CHROME_PROFILE_DIRS = [
    "~/Library/Application Support/Google/Chrome/Default",
    "~/Library/Application Support/Google/Chrome/Profile *",
]


class ViewResolutionError(RuntimeError):
    """Raised when a saved view cannot be resolved into concrete rows."""


def configured_profile_dirs() -> List[Path]:
    raw = str(os.getenv("VIEW_CACHE_PROFILE_DIRS", "") or "").strip()
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        values = list(DEFAULT_CHROME_PROFILE_DIRS)
    dirs: List[Path] = []
    seen = set()
    for value in values:
        expanded = os.path.expanduser(value)
        matches = [Path(expanded)]
        if "*" in expanded or "?" in expanded:
            matches = [Path(path) for path in glob.glob(expanded)]
        for match in matches:
            key = str(match)
            if key not in seen and match.exists():
                seen.add(key)
                dirs.append(match)
    return dirs


def iter_cache_files(profile_dir: Path) -> Iterable[Path]:
    root = profile_dir / "Service Worker" / "CacheStorage"
    if not root.exists():
        return
    for path in root.rglob("*_0"):
        if path.is_file():
            yield path


def iter_event_payloads(text: str) -> Iterable[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = text.find("data: ", cursor)
        if start < 0:
            return
        start += len("data: ")
        try:
            payload, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start
            continue
        if isinstance(payload, dict):
            yield payload
        cursor = start + consumed


def parse_runtime_and_structure(text: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    runtime: Dict[str, Any] = {}
    structure: Dict[str, Any] = {}
    for payload in iter_event_payloads(text):
        event = str(payload.get("event") or "")
        if event == "runtime":
            runtime = payload.get("runtime_config") or {}
        elif event == "structure":
            structure = payload.get("structure") or {}
        if runtime and structure:
            break
    if not runtime or not structure:
        raise ViewResolutionError("runtime_and_structure event stream is incomplete in local cache")
    return runtime, structure


def parse_cached_request_ts_ms(text: str) -> int:
    start = text.find(RUNTIME_AND_STRUCTURE_URL)
    if start < 0:
        return 0
    data_marker = text.find("data: ", start)
    if data_marker < 0:
        return 0
    segment = text[start:data_marker]
    match = re.search(r"(\d{13,})\s*$", segment)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def flatten_rows(structure: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    detail = structure.get("work_item_detail_v2") or {}
    if not isinstance(detail, dict):
        return rows
    for bucket in detail.values():
        if isinstance(bucket, list):
            rows.extend(item for item in bucket if isinstance(item, dict))
    return rows


def group_info_map(structure: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    table = ((structure.get("work_item_structure") or {}).get("table")) or {}
    groups = table.get("list") or []
    if not isinstance(groups, list):
        return result
    for group in groups:
        if not isinstance(group, dict):
            continue
        infos = group.get("group_info") or []
        if not isinstance(infos, list):
            continue
        for info in infos:
            if not isinstance(info, dict):
                continue
            node = str(info.get("struct_node_id") or "")
            if node and node not in result:
                result[node] = info
    return result


def first_row_data(ui_map: Dict[str, Any], suffix: str) -> Dict[str, Any]:
    for key, value in ui_map.items():
        if key.endswith(suffix) and isinstance(value, dict):
            return value
    return {}


def summarize_row(row: Dict[str, Any], groups: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    ui_map = row.get("uiDataMap") or {}
    if not isinstance(ui_map, dict):
        ui_map = {}
    name_entry = first_row_data(ui_map, "_name")
    status_entry = first_row_data(ui_map, "_work_item_status")
    name_data = ((name_entry.get("uiValue") or {}).get("nameWithComment")) or {}
    status_values = (((status_entry.get("uiValue") or {}).get("workItemStatus")) or {}).get("value") or []
    status_info = status_values[0] if isinstance(status_values, list) and status_values else {}
    node = str(row.get("struct_node_id") or "")
    group = groups.get(node) or {}
    return {
        "id": str(row.get("work_item_id") or row.get("storyID") or ""),
        "name": str(name_data.get("value") or ""),
        "status_label": str(status_info.get("label") or ""),
        "status_state_key": str(status_info.get("value") or ""),
        "group_display_name": str(group.get("display_name") or ""),
        "group_display_value": str(group.get("display_value") or ""),
        "work_item_type_key": str(row.get("work_item_type_key") or ""),
        "project_key_internal": str(row.get("project_key") or ""),
    }


def build_candidate(
    *,
    path: Path,
    profile_dir: Path,
    parsed_view: Dict[str, Any],
    text: str,
) -> Dict[str, Any]:
    runtime, structure = parse_runtime_and_structure(text)
    runtime_config = (runtime.get("runtime_config") or {})
    source_view_id = str(runtime_config.get("source_view_id") or "")
    if source_view_id != str(parsed_view.get("view_id") or ""):
        raise ViewResolutionError("cached response source_view_id does not match requested view")

    table = ((structure.get("work_item_structure") or {}).get("table")) or {}
    data_sources = table.get("data_sources") or []
    first_source = data_sources[0] if isinstance(data_sources, list) and data_sources else {}
    work_item_type_key = str(first_source.get("work_item_type_key") or "")
    rows = flatten_rows(structure)
    groups = group_info_map(structure)
    items = [summarize_row(row, groups) for row in rows]
    request_ts_ms = parse_cached_request_ts_ms(text)
    if not request_ts_ms:
        request_ts_ms = int(path.stat().st_mtime * 1000)
    return {
        "view_link": str(parsed_view.get("input_url") or ""),
        "parsed_view": parsed_view,
        "source": "chrome_service_worker_cache",
        "profile_dir": str(profile_dir),
        "cache_file": str(path),
        "cache_request_ts_ms": request_ts_ms,
        "work_item_type_key": work_item_type_key,
        "project_key_internal": str(first_source.get("project_key") or ""),
        "item_count": len(items),
        "queries": [item["id"] for item in items if item.get("id")],
        "items": items,
    }


def resolve_view_items(
    view_link: str,
    *,
    asset_subtask_type: str = DEFAULT_ASSET_SUBTASK_TYPE,
) -> Dict[str, Any]:
    parsed_view = parse_link(view_link, asset_subtask_type=asset_subtask_type)
    if not parsed_view.get("ok") or not parsed_view.get("supported_link_mode"):
        reason = str(parsed_view.get("reason") or "unsupported view link")
        raise ViewResolutionError(reason)
    view_id = str(parsed_view.get("view_id") or "")
    if not view_id:
        raise ViewResolutionError("view link does not contain a concrete view_id")

    candidates: List[Dict[str, Any]] = []
    checked_profiles: List[str] = []
    for profile_dir in configured_profile_dirs():
        checked_profiles.append(str(profile_dir))
        for path in iter_cache_files(profile_dir):
            raw = path.read_bytes()
            if RUNTIME_AND_STRUCTURE_URL.encode("utf-8") not in raw:
                continue
            if b"work_item_detail_v2" not in raw:
                continue
            if view_id.encode("utf-8") not in raw:
                continue
            text = raw.decode("utf-8", errors="ignore")
            try:
                candidate = build_candidate(
                    path=path,
                    profile_dir=profile_dir,
                    parsed_view=parsed_view,
                    text=text,
                )
            except ViewResolutionError:
                continue
            if candidate["item_count"] > 0:
                candidates.append(candidate)

    if not candidates:
        profiles_text = ", ".join(checked_profiles) if checked_profiles else "(none)"
        raise ViewResolutionError(
            "saved view rows are not available in local Chrome cache yet; open the view in Chrome on the proxy host once, then retry. "
            f"checked profiles: {profiles_text}"
        )

    candidates.sort(
        key=lambda item: (
            int(item.get("cache_request_ts_ms") or 0),
            int(item.get("item_count") or 0),
        ),
        reverse=True,
    )
    best = dict(candidates[0])
    best["candidate_count"] = len(candidates)
    return best
