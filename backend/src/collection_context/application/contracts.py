"""Versioned data and business errors; no network, global configuration or model calls."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
SOURCE_KINDS = frozenset({"liked", "saved", "collection", "creator", "link"})
ARTIFACT_KINDS = frozenset({"original", "audio", "screen", "summary", "image", "user_note"})
JOB_STATES = frozenset({"queued", "running", "succeeded", "partial", "failed", "cancelled", "blocked"})
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_ID = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class ContextError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        next_action: str | None = None,
        possibly_charged: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.possibly_charged = possibly_charged

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "possibly_charged": self.possibly_charged,
        }


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContextError("invalid_reference", "资料或任务引用无效。")
    return value


def item_id(platform: str, native_id: str) -> str:
    if platform != "douyin" or not isinstance(native_id, str) or not re.fullmatch(r"[0-9]{1,32}", native_id):
        raise ContextError("invalid_source_item", "请输入有效的抖音作品身份。")
    return "i_" + uuid.uuid5(uuid.NAMESPACE_URL, f"douyin:{native_id}").hex


def relation_id(item: str, kind: str, scope: str) -> str:
    valid_id(item)
    valid_id(scope)
    if kind not in SOURCE_KINDS:
        raise ContextError("invalid_source_kind", "来源须为喜欢、收藏、收藏夹、博主或单链接。")
    return "r_" + digest([item, kind, scope])[:32]


def envelope(
    data: Any = None, *, error: ContextError | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": error is None,
        "data": data if error is None else None,
        "error": error.as_dict() if error else None,
        "meta": {"schema_version": SCHEMA_VERSION, "request_id": "q_" + uuid.uuid4().hex, **(meta or {})},
    }


def validate_source(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize an explicitly supplied source item, never infer user action time."""
    identity = item_id(value.get("platform", "douyin"), value.get("native_id", ""))
    media_type = value.get("media_type", "video")
    if media_type not in {"video", "image"}:
        raise ContextError("invalid_source_item", "首版只接收视频或图文。")
    strings = {}
    for field, limit in (("title", 500), ("body", 200_000), ("author", 500), ("source_url", 2048)):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > limit or "\x00" in text:
            raise ContextError("invalid_source_item", f"资料字段 {field} 无效或过长。")
        strings[field] = text
    # Public sources are always reconstructed from identity; do not store a signed share URL.
    strings["source_url"] = (
        f"https://www.douyin.com/{'video' if media_type == 'video' else 'note'}/{value['native_id']}"
    )
    published = value.get("published_at")
    if published is not None:
        published = validate_time(published)
    return {
        "id": identity,
        "platform": "douyin",
        "native_id": value["native_id"],
        "media_type": media_type,
        "published_at": published,
        **strings,
    }


def validate_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        raise ContextError("invalid_time", "时间须包含时区；未知操作时间应保留为空。") from None
