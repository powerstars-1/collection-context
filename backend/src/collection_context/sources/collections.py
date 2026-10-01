"""Bounded normal-page folder observations; protocol candidate, not proof of live compatibility."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from collection_context.application.contracts import ContextError


@dataclass(frozen=True)
class FolderPage:
    folders: tuple[dict[str, str], ...]
    cursor: str | None
    has_more: bool


def folder_page(value: Any) -> FolderPage:
    if not isinstance(value, dict) or type(value.get("status_code")) is not int:
        raise ContextError("source_shape_changed", "收藏夹列表结构未知，未当作空列表。")
    if value["status_code"] != 0:
        raise ContextError("source_access_required", "平台未允许读取收藏夹，请检查独立登录或验证。")
    rows, more, cursor = value.get("collects_list"), value.get("has_more"), value.get("cursor")
    if not isinstance(rows, list) or len(rows) > 100 or type(more) not in {bool, int} or more not in {0, 1}:
        raise ContextError("source_shape_changed", "收藏夹条目或分页标记无法识别。")
    if cursor is None and not more:
        cursor = None
    elif type(cursor) not in {str, int} or not re.fullmatch(r"[0-9]{1,32}", str(cursor)):
        raise ContextError("source_cursor_invalid", "收藏夹游标缺失或无效，未猜测翻页。")
    else:
        cursor = str(cursor)
    folders = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ContextError("source_shape_changed", "收藏夹记录不是明确条目。")
        identity, name = row.get("collects_id_str"), row.get("collects_name")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[0-9]{1,32}", identity) is None
            or not isinstance(name, str)
            or not 1 <= len(name) <= 500
            or "\x00" in name
            or identity in seen
        ):
            raise ContextError("source_shape_changed", "收藏夹稳定身份/名称无效或页内重复。")
        folders.append({"collection_id": identity, "name": name})
        seen.add(identity)
    return FolderPage(tuple(folders), cursor, bool(more))


class FolderBatch:
    def __init__(self, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContextError("invalid_source_limit", "收藏夹发现上限为1至100个。")
        self.limit = limit
        self.folders: dict[str, dict[str, str]] = {}
        self.pages = 0
        self.cursor: str | None = None
        self.cursors: set[str] = set()
        self.done = False
        self.complete = False

    def accept(self, page: FolderPage, request_cursor: str) -> None:
        if self.done:
            return
        if request_cursor != (self.cursor if self.pages else "0"):
            raise ContextError("source_cursor_mismatch", "收藏夹分页不连续，未合并不同列表。")
        if page.has_more and (page.cursor == request_cursor or page.cursor in self.cursors):
            raise ContextError("source_cursor_stalled", "收藏夹分页未推进，停止重复访问。")
        truncated = False
        for value in page.folders:
            identity = value["collection_id"]
            if identity in self.folders:
                if self.folders[identity] != value:
                    raise ContextError("source_snapshot_changed", "分页期间收藏夹名称变化，请重新发现。")
                continue
            if len(self.folders) >= self.limit:
                truncated = True
                break
            self.folders[identity] = value
        self.pages += 1
        self.cursor = page.cursor
        if self.cursor is not None:
            self.cursors.add(self.cursor)
        self.done = len(self.folders) >= self.limit or not page.has_more or self.pages >= 10
        self.complete = not page.has_more and not truncated
