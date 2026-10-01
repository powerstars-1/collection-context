"""Confirmed installer downloads only; fixed catalog, public TLS, no credentials.

This source is called by RuntimeInstaller after it owns its installation lease.
It does not install, execute, alter PATH, enable synchronization, or call models.
Downloads use the existing bounded, DNS-pinned transport without proxy inheritance.
Cancellation is observed before and after its bounded network operation, not
misrepresented as immediate cancellation of an outstanding socket read.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.public_http import PublicHTTP
from collection_context.infrastructure.runtime_installation import ArtifactPlan
from collection_context.infrastructure.runtime_stream import download_into

DOWNLOAD_HOSTS = frozenset(
    {"cdn.playwright.dev", "playwright.download.prss.microsoft.com", "storage.googleapis.com"}
)
MAX_DOWNLOAD_BYTES = 2_147_483_648
BUFFERED_DOWNLOAD_LIMIT = 128_000_000


class RuntimeDownloads:
    """One caller-owned download scope; no URL or credential supplied by an AI.

    The caller passes the product catalog, not a user-provided JSON catalog. Temporary
    archives live under the already-owned private runtime root and are discarded
    after the installer has verified and copied them. Failed installations retain
    their own generation evidence according to the installer contract.
    """

    def __init__(
        self,
        runtime_dir: Path,
        *,
        catalog: Mapping[str, ArtifactPlan],
        stop: threading.Event | None = None,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._catalog = MappingProxyType(dict(catalog))
        self._stop = stop
        self._temporary: list[tempfile.TemporaryDirectory[str]] = []

    def _check_stop(self) -> None:
        if self._stop is not None and self._stop.is_set():
            raise ContextError("runtime_install_cancelled", "安装已取消；不会继续发布安装收据。")

    def __call__(self, plan: ArtifactPlan) -> Path:
        if not isinstance(plan, ArtifactPlan) or self._catalog.get(plan.id) != plan:
            raise ContextError("runtime_download_catalog", "下载项不属于固定产品清单；未请求网络。")
        if type(plan.bytes) is not int or not 0 < plan.bytes <= MAX_DOWNLOAD_BYTES:
            raise ContextError("runtime_download_limit", "此安装包超过当前下载器上限；未请求网络。")
        self._check_stop()
        transport = PublicHTTP(hosts=DOWNLOAD_HOSTS)
        transport.validate(plan.source_url)  # Before creating files, DNS or TLS.
        if plan.bytes > BUFFERED_DOWNLOAD_LIMIT:
            return self._large_archive(transport, plan)
        try:
            data = transport.get(plan.source_url, max_bytes=plan.bytes, timeout=120).data
        except ContextError:
            raise ContextError("runtime_download_failed", "组件下载未完成；未自动重试或安装。") from None
        self._check_stop()
        if len(data) != plan.bytes or hashlib.sha256(data).hexdigest() != plan.sha256:
            raise ContextError("runtime_download_integrity", "下载内容与固定清单不符；未安装。")
        temporary = tempfile.TemporaryDirectory(prefix=".download-", dir=self._runtime_dir)
        self._temporary.append(temporary)
        root = Path(temporary.name)
        with SafeFiles(root) as files:
            files.write("artifact", data)
        self._check_stop()
        return root / "artifact"

    def _large_archive(self, transport: PublicHTTP, plan: ArtifactPlan) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix=".download-", dir=self._runtime_dir)
        self._temporary.append(temporary)
        root = Path(temporary.name)
        with SafeFiles(root) as files:
            fd = os.open(
                "artifact", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=files.fd
            )
            try:
                size, digest = download_into(
                    transport, plan.source_url, fd, expected_bytes=plan.bytes, check_cancel=self._check_stop
                )
                files.check_root()
            finally:
                os.close(fd)
        if size != plan.bytes or digest != plan.sha256:
            raise ContextError("runtime_download_integrity", "下载内容与固定清单不符；未安装。")
        self._check_stop()
        return root / "artifact"

    def close(self) -> None:
        for temporary in reversed(self._temporary):
            temporary.cleanup()
        self._temporary.clear()

    def __enter__(self) -> RuntimeDownloads:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
