"""Normalize observed platform responses without importing authentication/signing projects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from collection_context.application.contracts import ContextError, digest, validate_source

VERSION = "douyin_observed_v1"


@dataclass(frozen=True)
class MediaAsset:
    kind: str
    page_index: int | None
    # Signed CDN addresses are transient secrets, never printed or put into a public source card.
    urls: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class ObservedItem:
    source: dict[str, Any]
    assets: tuple[MediaAsset, ...]
    author_id: str | None


def source_asset_identity(observed: ObservedItem) -> str:
    """Stable offered resource identity, not a claim that a same-URL remote file cannot change."""

    def address(url: str) -> tuple:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        identities = tuple(
            (key, tuple(query[key])) for key in ("video_id", "vid", "image_id", "aweme_id") if key in query
        )
        return parts.hostname, parts.path, identities

    return digest(
        [
            [
                asset.kind,
                asset.page_index,
                sorted({address(url) for url in asset.urls}),
            ]
            for asset in observed.assets
        ]
    )


def _urls(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict) or not isinstance(value.get("url_list"), list):
        raise ContextError("source_media_missing", "平台未返回可用媒体地址；不会推断下载成功。")
    urls = value["url_list"]
    if not urls or len(urls) > 16:
        raise ContextError("source_shape_changed", "媒体地址结构发生变化或超过边界。")
    output = []
    for url in urls:
        if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
            raise ContextError("source_shape_changed", "媒体地址结构发生变化。")
        try:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.port not in {None, 443}
                or "\\" in url
            ):
                raise ValueError
        except ValueError:
            raise ContextError("unsafe_media_url", "平台媒体地址不符合安全下载要求。") from None
        output.append(url)
    return tuple(dict.fromkeys(output))


def normalize_item(raw: Any, *, expected_id: str | None = None) -> ObservedItem:
    if not isinstance(raw, dict) or not isinstance(raw.get("aweme_id"), str):
        raise ContextError("source_shape_changed", "作品数据结构变化，未按猜测字段入库。")
    native_id = raw["aweme_id"]
    if not re.fullmatch(r"[0-9]{1,32}", native_id) or (expected_id is not None and expected_id != native_id):
        raise ContextError("source_identity_mismatch", "页面返回了不同作品，未将推荐内容当成指定作品。")
    status = raw.get("status", {})
    if not isinstance(status, dict):
        raise ContextError("source_shape_changed", "作品可见性状态不可识别。")
    if status.get("is_delete") or status.get("is_prohibited") or status.get("private_status", 0):
        raise ContextError("source_unavailable", "作品已不可见或受限；未绕过访问控制。")
    desc = raw.get("desc")
    author = raw.get("author")
    if (
        not isinstance(desc, str)
        or not isinstance(author, dict)
        or not isinstance(author.get("nickname"), str)
    ):
        raise ContextError("source_shape_changed", "作品正文或作者字段缺失，未生成空正文代替。")
    published = raw.get("create_time")
    if published is not None:
        if type(published) is not int or not 0 < published < 253402300800:
            raise ContextError("source_shape_changed", "作品发布时间格式变化。")
        published = datetime.fromtimestamp(published, UTC).isoformat()
    author_id = author.get("sec_uid")
    if author_id is not None and (
        not isinstance(author_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", author_id)
    ):
        raise ContextError("source_shape_changed", "博主身份字段格式变化。")
    images = raw.get("images")
    assets = []
    if images is not None and not isinstance(images, list):
        raise ContextError("source_shape_changed", "图文页面结构变化。")
    if images:
        if len(images) > 240:
            raise ContextError("source_limit", "原图超过240页，未截断或冒充完整。")
        media_type = "image"
        for index, image in enumerate(images):
            assets.append(MediaAsset("image", index, _urls(image)))
    else:
        media_type = "video"
        video = raw.get("video")
        if not isinstance(video, dict):
            raise ContextError("source_media_missing", "未取得视频资料，不能判为无音轨。")
        assets.append(MediaAsset("video", None, _urls(video.get("play_addr"))))
    source = validate_source(
        {
            "native_id": native_id,
            "media_type": media_type,
            "title": desc.split("\n", 1)[0][:500] or "无标题作品",
            "body": desc,
            "author": author["nickname"],
            "published_at": published,
        }
    )
    return ObservedItem(source, tuple(assets), author_id)
