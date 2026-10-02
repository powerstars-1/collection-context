"""Large fixed software downloads, separate from the ordinary media size limit."""

from __future__ import annotations

import hashlib
import http.client
import os
import stat
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from urllib.parse import urljoin

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.file_stream import MAX_STREAM_BYTES, STREAM_CHUNK_BYTES
from collection_context.infrastructure.public_http import PublicHTTP, _PinnedHTTPS, public_addresses


def _validate_download(expected_bytes: int, timeout: float, check_cancel: Callable[[], None]) -> None:
    if (
        type(expected_bytes) is not int
        or not 0 < expected_bytes <= MAX_STREAM_BYTES
        or type(timeout) not in {int, float}
        or not 0 < timeout <= 600
        or not callable(check_cancel)
    ):
        raise ContextError("runtime_download_limit", "固定软件下载上限无效；未请求网络。")


@contextmanager
def download_chunks(
    transport: PublicHTTP,
    url: str,
    *,
    expected_bytes: int,
    timeout: float = 300,
    check_cancel: Callable[[], None],
) -> Iterator[Iterator[bytes]]:
    """Own the network producer even when a downstream file sink stops early.

    Cancellation is checked between operations. DNS/blocked socket reads cannot
    be described as instantly cancelled. No proxy, auth, cookies, mirrors, or
    automatic retry; redirects remain independently checked and DNS-pinned.
    """
    _validate_download(expected_bytes, timeout, check_cancel)
    transport.validate(url)
    source = _body(transport, url, expected_bytes, timeout, check_cancel)
    try:
        yield source
    finally:
        source.close()


def _body(
    transport: PublicHTTP, url: str, expected_bytes: int, timeout: float, check_cancel: Callable[[], None]
) -> Generator[bytes, None, None]:
    deadline = time.monotonic() + timeout
    for _ in range(6):
        check_cancel()
        host, path = transport.validate(url)
        addresses = public_addresses(host)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContextError("runtime_download_failed", "软件包下载已超时；没有自动重试。")
        connection = _PinnedHTTPS(host, addresses[0], min(30, remaining))
        try:
            connection.request(
                "GET",
                path,
                headers={"Host": host, "Accept-Encoding": "identity", "User-Agent": "CollectionContext/0.2"},
            )
            response = connection.getresponse()
            check_cancel()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ContextError("runtime_download_failed", "软件来源跳转缺少目标。")
                url = urljoin(url, location)
                transport.validate(url)
                continue
            if (
                response.status != 200
                or response.getheader("Content-Encoding", "identity").lower() != "identity"
            ):
                raise ContextError("runtime_download_failed", "软件来源未返回完整的原始包。")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) != expected_bytes):
                raise ContextError("runtime_download_integrity", "软件包长度与固定清单不符。")
            count = 0
            while True:
                check_cancel()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock is not None:
                    connection.sock.settimeout(min(30, remaining))
                chunk = response.read1(min(STREAM_CHUNK_BYTES, expected_bytes + 1 - count))
                check_cancel()
                if type(chunk) is not bytes or len(chunk) > STREAM_CHUNK_BYTES:
                    raise ContextError("runtime_download_integrity", "软件来源分块内容无效。")
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_bytes:
                    raise ContextError("runtime_download_integrity", "软件包超过固定清单长度。")
                yield chunk
            if count != expected_bytes:
                raise ContextError("runtime_download_integrity", "软件包未完整下载。")
            check_cancel()
            return
        except ContextError:
            raise
        except (OSError, http.client.HTTPException):
            raise ContextError(
                "runtime_download_failed", "软件包下载未完成；未回显响应或自动重试。"
            ) from None
        finally:
            connection.close()
    raise ContextError("runtime_download_failed", "软件来源跳转超过上限；未自动重试。")


def download_into(
    transport: PublicHTTP,
    url: str,
    fd: int,
    *,
    expected_bytes: int,
    timeout: float = 300,
    check_cancel: Callable[[], None],
) -> tuple[int, str]:
    """Legacy POSIX sink only; business downloads use the portable chunk contract."""
    _validate_download(expected_bytes, timeout, check_cancel)
    before = os.fstat(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size != 0
        or os.lseek(fd, 0, os.SEEK_CUR) != 0
    ):
        raise ContextError("runtime_download_unsafe", "下载目标不是新的私有普通文件。")
    count, digest = 0, hashlib.sha256()
    try:
        with download_chunks(
            transport, url, expected_bytes=expected_bytes, timeout=timeout, check_cancel=check_cancel
        ) as chunks:
            for chunk in chunks:
                count += len(chunk)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    check_cancel()
                    written = os.write(fd, view)
                    if not 0 < written <= len(view):
                        raise OSError
                    view = view[written:]
            after = os.fstat(fd)
            if after.st_size != count or (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_nlink,
                before.st_mode,
            ) != (after.st_dev, after.st_ino, after.st_uid, after.st_nlink, after.st_mode):
                raise ContextError("runtime_download_integrity", "下载目标身份或大小变化。")
            os.fsync(fd)
            check_cancel()
            return count, digest.hexdigest()
    except OSError:
        raise ContextError("runtime_download_failed", "下载文件写入失败；未自动重试。") from None
