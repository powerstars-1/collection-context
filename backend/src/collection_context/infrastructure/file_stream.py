"""Bounded byte streams for private file publication, independent of OS IO."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Iterator

from collection_context.application.contracts import ContextError

STREAM_CHUNK_BYTES = 65_536
MAX_STREAM_BYTES = 2_147_483_648


def validate_stream(expected_size: int, expected_sha256: str, replace: bool = False) -> None:
    if (
        type(expected_size) is not int
        or not 0 <= expected_size <= MAX_STREAM_BYTES
        or type(expected_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(replace) is not bool
    ):
        raise ContextError("invalid_argument", "分块文件大小、校验值或覆盖开关无效。")


def verified_chunks(
    source: Iterable[bytes], *, expected_size: int, expected_sha256: str, check_cancel: Callable[[], None]
) -> Iterator[bytes]:
    """Source is trusted wiring, never a producer supplied by a CLI/AI request.

    The owner must close its producer scope on success and on any sink failure.
    Yield at most 64KiB per chunk; hash/length mismatch is detected at EOF before
    publication. No buffering of the whole file and no automatic retry.
    """
    validate_stream(expected_size, expected_sha256)
    if not callable(check_cancel):
        raise ContextError("invalid_argument", "分块取消检查无效。")
    try:
        iterator = iter(source)
    except TypeError:
        raise ContextError("invalid_argument", "分块来源无效。") from None

    def consume() -> Iterator[bytes]:
        total, digest = 0, hashlib.sha256()
        while True:
            check_cancel()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            check_cancel()
            if type(chunk) is not bytes or not 0 < len(chunk) <= STREAM_CHUNK_BYTES:
                raise ContextError("file_stream_integrity", "分块内容或长度不符合受控边界。")
            total += len(chunk)
            if total > expected_size:
                raise ContextError("file_stream_integrity", "分块文件超过固定大小。")
            digest.update(chunk)
            yield chunk
        check_cancel()
        if total != expected_size or digest.hexdigest() != expected_sha256:
            raise ContextError("file_stream_integrity", "分块文件不完整或校验值不符；未发布。")

    return consume()
