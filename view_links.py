#!/usr/bin/env python3
# coding: utf-8
"""Helpers for parsing Feishu Project view links."""

from __future__ import annotations

from typing import Dict, Tuple
from urllib.parse import parse_qs, urlparse


DEFAULT_ASSET_SUBTASK_TYPE = "69ca097070c61cbef714a50f"


def success(**kwargs: object) -> Dict[str, object]:
    payload: Dict[str, object] = {"ok": True, "supported_link_mode": True}
    payload.update(kwargs)
    return payload


def failure(project_key: str, reason: str, **kwargs: object) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "ok": False,
        "supported_link_mode": False,
        "project_key": project_key,
        "reason": reason,
    }
    payload.update(kwargs)
    return payload


def parse_path(path: str) -> Tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def parse_link(url: str, asset_subtask_type: str = DEFAULT_ASSET_SUBTASK_TYPE) -> Dict[str, object]:
    parsed = urlparse(url)
    parts = parse_path(parsed.path)
    query = parse_qs(parsed.query)

    if parsed.scheme not in {"http", "https"} or parsed.netloc != "project.feishu.cn":
        return failure("", "not a project.feishu.cn link", input_url=url)
    if len(parts) < 2:
        return failure("", "path is too short to identify a view", input_url=url)

    project_key = parts[0]
    common = {
        "input_url": url,
        "project_key": project_key,
        "node": query.get("node", [""])[0],
        "scope": query.get("scope", [""])[0],
    }

    view_kind = parts[1]
    if view_kind == "issueView" and len(parts) >= 3:
        return success(
            view_kind="issueView",
            work_item_type_key="issue",
            view_id=parts[2],
            **common,
        )
    if view_kind == "storyView" and len(parts) >= 3:
        return success(
            view_kind="storyView",
            work_item_type_key="story",
            view_id=parts[2],
            **common,
        )
    if view_kind == "multiProjectView" and len(parts) >= 3:
        return failure(
            project_key,
            "multiProjectView is not directly supported by this proxy yet; use a regular view link, screenshot, or pasted task list instead",
            view_kind="multiProjectView",
            view_id=parts[2],
            quick_filter_id=query.get("quickFilterId", [""])[0],
            **{k: v for k, v in common.items() if k != "project_key"},
        )
    if view_kind == "asset_subtask" and len(parts) >= 3 and parts[2] == "homepage":
        return failure(
            project_key,
            "asset_subtask homepage does not include a concrete view id; open a specific saved view instead",
            view_kind="asset_subtask_homepage",
            **{k: v for k, v in common.items() if k != "project_key"},
        )
    if view_kind == "workObjectView" and len(parts) >= 4:
        object_type = parts[2]
        work_item_type_key = object_type
        if object_type == "asset_subtask":
            work_item_type_key = asset_subtask_type or DEFAULT_ASSET_SUBTASK_TYPE
        return success(
            view_kind="workObjectView",
            object_type=object_type,
            work_item_type_key=work_item_type_key,
            view_id=parts[3],
            **common,
        )

    return failure(
        project_key,
        "unrecognized Feishu Project view link format",
        input_url=url,
        path=parsed.path,
    )
