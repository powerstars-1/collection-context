"""Conservative observed pagination contracts; an empty/unparseable response is not a successful list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.sources.douyin import ObservedItem, normalize_item


@dataclass(frozen=True)
class ObservedPage:
    items: tuple[ObservedItem, ...]
    cursor: str | None
    has_more: bool


def work_page(payload: Any, *, cursor_field: str) -> ObservedPage:
    if cursor_field not in {"max_cursor", "cursor"}:
        raise ContextError("invalid_source_scope", "分页类型未注册。")
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise ContextError("source_shape_changed", "作品列表结构变化，未判为空列表。")
    if payload["status_code"] != 0:
        raise ContextError("source_access_required", "平台未允许读取作品列表，请检查独立浏览器登录或验证。")
    rows = payload.get("aweme_list")
    more = payload.get("has_more")
    if not isinstance(rows, list) or len(rows) > 50 or type(more) not in {bool, int} or more not in {0, 1}:
        raise ContextError("source_shape_changed", "列表条目或分页标记不可识别，未保存部分结果冒充完整。")
    cursor = payload.get(cursor_field)
    if cursor is None and not more:
        cursor = None
    elif type(cursor) not in {int, str} or not re.fullmatch(r"[0-9]{1,32}", str(cursor)):
        raise ContextError("source_cursor_invalid", "分页游标缺失或无效，未猜测下一页。")
    else:
        cursor = str(cursor)
    items = tuple(normalize_item(raw) for raw in rows)
    return ObservedPage(items, cursor, bool(more))


def creator_page(payload: Any, creator_id: str) -> ObservedPage:
    if not isinstance(creator_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", creator_id):
        raise ContextError("invalid_source_scope", "博主身份无效。")
    page = work_page(payload, cursor_field="max_cursor")
    items = page.items
    if any(item.author_id != creator_id for item in items):
        raise ContextError("source_scope_mismatch", "作品列表混入其他作者，未当作指定博主作品。")
    return page


class SourceBatch:
    """A bounded newest-first observation. Never implies all history or a stable snapshot."""

    def __init__(self, limit: int):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ContextError("invalid_source_limit", "本轮来源同步数量为1至20条。")
        self.limit = limit
        self.items: dict[str, ObservedItem] = {}
        self.pages = 0
        self.cursor: str | None = None
        self.has_more: bool | None = None
        self.cursors: set[str] = set()
        self.done = False
        self.response_limited = False

    def accept(self, page: ObservedPage, request_cursor: str):
        if self.done:
            return
        if self.pages and request_cursor != self.cursor:
            raise ContextError("source_cursor_mismatch", "网页分页请求不连续，未合并不同范围。")
        if not self.pages and request_cursor != "0":
            raise ContextError("source_cursor_mismatch", "首批不是从列表起点开始，未冒充近期覆盖。")
        if page.has_more and (page.cursor == request_cursor or page.cursor in self.cursors):
            raise ContextError("source_cursor_stalled", "平台分页没有推进，停止重复请求。")
        self.pages += 1
        self.cursor, self.has_more = page.cursor, page.has_more
        if page.cursor is not None:
            self.cursors.add(page.cursor)
        for item in page.items:
            if len(self.items) == self.limit:
                self.response_limited = True
                break
            self.items.setdefault(item.source["native_id"], item)
        self.done = len(self.items) >= self.limit or not page.has_more

    def coverage(self) -> dict[str, Any]:
        return {
            "observed_count": len(self.items),
            "pages": self.pages,
            "complete": False,
            "requested_limit": self.limit,
            "limit_reached": len(self.items) >= self.limit,
            "history_exhausted": self.has_more is False and not self.response_limited,
            "has_more": self.has_more,
            "source_snapshot": "not_guaranteed",
            "note": "只报告本轮实际作品覆盖；平台可能置顶、删帖或排序变化，不保证全历史或严格发布时间顺序。",
        }


# Preserve the original creator entry point while all list types share the same bounded contract.
CreatorBatch = SourceBatch
