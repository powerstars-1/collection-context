"""Bounded public HTTPS downloads: pinned DNS, verified TLS, no credentials or proxy inheritance."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from collection_context.application.contracts import ContextError


@dataclass(frozen=True)
class Download:
    data: bytes = field(repr=False)
    content_type: str


class PublicDownloadError(ContextError):
    """Keep only an HTTP status for an internal bounded mirror decision, never headers or body."""

    def __init__(self, status: int):
        super().__init__(
            "download_rejected",
            "来源未返回完整媒体；可能已过期或需要在平台重新打开。",
            retryable=status in {429, 500, 502, 503, 504},
        )
        self.http_status = status


def public_addresses(host: str) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(str(row[4][0]) for row in results))
        if not addresses or any(
            not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
            for address in addresses
        ):
            raise ValueError
        return addresses
    except (OSError, ValueError):
        raise ContextError(
            "unsafe_public_address", "来源地址解析失败或指向非公开网络；未发出请求。"
        ) from None


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self):
        # Do not resolve host again at connect time; SNI/certificate validation still uses the original host.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class PublicHTTP:
    def __init__(self, *, hosts: frozenset[str], suffixes: frozenset[str] = frozenset()):
        self.hosts = hosts
        self.suffixes = suffixes

    def validate(self, url: str) -> tuple[str, str]:
        try:
            if not isinstance(url, str) or not url or len(url) > 8192 or any(ord(c) < 33 for c in url):
                raise ValueError
            parts = urlsplit(url)
            host = parts.hostname
            if (
                parts.scheme != "https"
                or not host
                or parts.username is not None
                or parts.password is not None
                or parts.port not in {None, 443}
                or "\\" in url
                or parts.fragment
                or not (host in self.hosts or any(host.endswith("." + suffix) for suffix in self.suffixes))
            ):
                raise ValueError
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            return host, path
        except ValueError:
            raise ContextError("unsafe_public_url", "下载或跳转地址不在允许的公开HTTPS来源中。") from None

    def get(self, url: str, *, max_bytes: int = 128_000_000, timeout: float = 60) -> Download:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 128_000_000 or not 0 < timeout <= 120:
            raise ContextError("invalid_download_limit", "下载大小或时限无效。")
        deadline = time.monotonic() + timeout
        for _ in range(6):
            host, path = self.validate(url)
            addresses = public_addresses(host)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContextError("download_timeout", "下载超时，未保存为完整媒体。", retryable=True)
            connection = _PinnedHTTPS(host, addresses[0], remaining)
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Host": host,
                        "Accept-Encoding": "identity",
                        "User-Agent": "CollectionContext/0.2",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ContextError("download_redirect_invalid", "来源跳转缺少目标。")
                    url = urljoin(url, location)
                    # Validate the next hop before DNS or connect; never forward Cookie/Authorization/Referer.
                    self.validate(url)
                    continue
                if response.status != 200:
                    raise PublicDownloadError(response.status)
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ContextError(
                        "download_encoding_unsupported", "来源返回压缩传输，未按不明大小解压。"
                    )
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise ContextError("download_limit", "媒体超过下载上限或长度无效，未截断保存。")
                chunks, total = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65_536, max_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ContextError("download_limit", "媒体超过下载上限，未保存不完整内容。")
                if not total or (length is not None and total != int(length)):
                    raise ContextError(
                        "download_incomplete", "媒体为空或传输未完成，未登记为成功。", retryable=True
                    )
                return Download(
                    b"".join(chunks), response.getheader("Content-Type", "").split(";", 1)[0].lower()
                )
            except ContextError:
                raise
            except TimeoutError:
                raise ContextError(
                    "download_timeout", "下载超时，未保存为完整媒体。", retryable=True
                ) from None
            except (OSError, http.client.HTTPException):
                raise ContextError(
                    "download_failed", "公开媒体下载失败；未回显地址、凭据或响应正文。", retryable=True
                ) from None
            finally:
                connection.close()
        raise ContextError("download_redirect_limit", "来源跳转次数超过上限。")
