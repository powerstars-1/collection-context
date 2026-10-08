"""Download observed assets through an explicit platform CDN allowlist, never browser cookies."""

from __future__ import annotations


from collection_context.application.contracts import ContextError
from collection_context.infrastructure.public_http import Download, PublicDownloadError, PublicHTTP
from collection_context.sources.douyin import MediaAsset, ObservedItem
from collection_context.infrastructure.download_limits import DEFAULT_DOWNLOAD_MB, MB, MAX_SOURCE_BYTES


def media_mime(data: bytes, kind: str) -> str:
    # A MIME header alone is not proof: do not save a login/verification HTML page as a video.
    if kind == "video" and len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if kind == "video" and data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    if kind == "image" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if kind == "image" and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if kind == "image" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ContextError("source_media_format", "返回内容不是支持的媒体格式；没有保存HTML或伪装文件。")


class DouyinDownloads:
    def __init__(self, transport: PublicHTTP | None = None, *, max_bytes: int = DEFAULT_DOWNLOAD_MB * MB):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SOURCE_BYTES:
            raise ContextError("invalid_download_limit", "下载大小设置无效。")
        self.max_bytes = max_bytes
        # New hosts require review; a payload cannot authorize an arbitrary download domain.
        self.transport = transport or PublicHTTP(
            hosts=frozenset({"www.douyin.com"}), suffixes=frozenset({"douyinvod.com", "byteimg.com"}),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            referer="https://www.douyin.com/",
        )

    def _asset(self, asset: MediaAsset) -> Download:
        # Normal page responses offer multiple CDN mirrors for the same asset. Try at most three
        # distinct offered URLs, each through the same allowlist/DNS/TLS checks. Never synthesize a
        # token, import cookies or repeat the same URL indefinitely. Respect rate limiting.
        for index, url in enumerate(asset.urls[:3]):
            try:
                return self.transport.get(url, max_bytes=self.max_bytes, timeout=300)
            except PublicDownloadError as error:
                if index == min(len(asset.urls), 3) - 1 or error.http_status not in {
                    403,
                    404,
                    410,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise
        raise ContextError("source_media_missing", "页面没有提供原媒体地址。")

    def fetch(self, item: ObservedItem) -> list[tuple[bytes, str]]:
        output = []
        total = 0
        for asset in item.assets:
            result = self._asset(asset)
            if len(result.data) > self.max_bytes:
                raise ContextError("download_limit", f"原媒体超过单文件 {self.max_bytes // MB} MB 上限，未保存不完整内容。")
            total += len(result.data)
            if total > MAX_SOURCE_BYTES:
                raise ContextError("source_limit", "此作品原媒体总量超过2048MB；没有截断图文页面。")
            output.append((result.data, media_mime(result.data, asset.kind)))
        return output
