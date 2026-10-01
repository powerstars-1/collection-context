"""Recognize only bounded public Douyin links; never retain share tracking parameters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from collection_context.application.contracts import ContextError

PAGE_HOSTS = frozenset({"www.douyin.com", "douyin.com", "www.iesdouyin.com", "iesdouyin.com"})
SHORT_HOSTS = frozenset({"v.douyin.com"})
_URL = re.compile(r"https?://[^\s<>\"“”]+", re.IGNORECASE)
_NUMERIC = re.compile(r"[0-9]{1,32}")
_USER = re.compile(r"[A-Za-z0-9_-]{1,160}")


@dataclass(frozen=True)
class DouyinLink:
    kind: str
    identity: str | None
    url: str


def parse_link(value: str) -> DouyinLink:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8192
        or any(ord(c) < 32 and c not in "\r\n\t" for c in value)
    ):
        raise ContextError("invalid_source_link", "请输入一条完整的抖音作品或博主链接。")
    candidates = list(dict.fromkeys(match.rstrip("。，、；;!！)）]}") for match in _URL.findall(value)))
    if len(candidates) != 1:
        raise ContextError("invalid_source_link", "一次只接收一个抖音链接；分享文字可以保留。")
    url = candidates[0]
    try:
        parts = urlsplit(url)
        if (
            parts.scheme.lower() != "https"
            or parts.hostname not in PAGE_HOSTS | SHORT_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or "\\" in url
            or "%" in parts.path
        ):
            raise ValueError
    except ValueError:
        raise ContextError(
            "invalid_source_link", "仅支持官方 HTTPS 抖音链接，不能指定账号信息或其他端口。"
        ) from None
    path = parts.path.rstrip("/")
    if parts.hostname in SHORT_HOSTS:
        token = path.removeprefix("/")
        if not _USER.fullmatch(token) or len(token) > 80:
            raise ContextError("invalid_source_link", "抖音短链接格式无效。")
        return DouyinLink("short", None, f"https://v.douyin.com/{token}/")
    chunks = path.strip("/").split("/")
    if chunks[0] == "share":
        chunks = chunks[1:]
    if len(chunks) == 2 and chunks[0] in {"video", "note"} and _NUMERIC.fullmatch(chunks[1]):
        return DouyinLink("item", chunks[1], f"https://www.douyin.com/{chunks[0]}/{chunks[1]}")
    # Normal web shares sometimes route through modal_id on the homepage or a creator page.
    modal = parse_qs(parts.query).get("modal_id", [])
    if modal and (path in {"", "/"} or (len(chunks) == 2 and chunks[0] == "user")):
        if len(modal) != 1 or not _NUMERIC.fullmatch(modal[0]):
            raise ContextError("invalid_source_link", "作品链接包含冲突或无效的作品身份。")
        return DouyinLink("item", modal[0], f"https://www.douyin.com/video/{modal[0]}")
    if len(chunks) == 2 and chunks[0] == "user" and _USER.fullmatch(chunks[1]):
        return DouyinLink("creator", chunks[1], f"https://www.douyin.com/user/{chunks[1]}")
    raise ContextError("unsupported_source_link", "这不是支持的作品或博主主页链接。")
