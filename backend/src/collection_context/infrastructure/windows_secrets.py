"""Unified system-secret metadata with native Windows private-root/item adapters.

This explicit class is not an enabled public platform gate or Windows release
acceptance. Private injected WindowsStorage/items are solely offline SDK seams.
No plaintext fallback, old-key import, OS item enumeration or implicit adoption.
"""

from __future__ import annotations

import sys
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.storage import StorageBackend, WindowsStorage
from collection_context.infrastructure.system_secrets import (
    SystemSecrets,
    _ItemsFactory,
    _SystemItems,
)


class WindowsSystemSecrets(SystemSecrets):
    storage_kind = "windows_credential_manager"

    @classmethod
    def _configuration(
        cls,
        storage: StorageBackend | None,
        backend: _SystemItems | None,
        factory: _ItemsFactory | None,
    ) -> tuple[StorageBackend, _ItemsFactory | None]:
        if storage is None:
            if sys.platform != "win32":
                raise ContextError("system_secret_unsupported", "此后端需要本机 Windows 原生凭据能力。")
            storage = WindowsStorage()
        if not isinstance(storage, WindowsStorage):
            raise ContextError(
                "system_secret_unsupported", "Windows 系统凭据需要明确的私有 HANDLE 文件后端。"
            )
        if backend is not None and factory is not None:
            raise ContextError("invalid_argument", "凭据测试后端只能明确选择一种。")
        if backend is None and factory is None:
            from collection_context.infrastructure.windows_credential_items import WindowsCredentialItems

            native = storage._native

            def default_items(root: Path, namespace: str) -> _SystemItems:
                return WindowsCredentialItems(str(root), namespace, _native=native)

            factory = default_items
        return storage, factory
