"""Explicit private service files outside the library; not an encrypted desktop credential backend."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from collection_context.application.contracts import ContextError, valid_id
from collection_context.infrastructure.storage import FileAccess, StorageBackend, storage_backend

SYSTEM_SECRET_MANIFEST = "collection-system-secrets.json"


@runtime_checkable
class CredentialBackend(Protocol):
    files: FileAccess
    storage_kind: str

    def put(self, value: str) -> str: ...

    def get(self, ref: str) -> str: ...

    def discard_new(self, ref: str) -> None: ...

    def close(self) -> None: ...


class FileSecrets:
    storage_kind = "private_service_files_not_encrypted"

    def __init__(self, root: Path, *, _storage: StorageBackend | None = None):
        self.storage = _storage if _storage is not None else storage_backend()
        self.files = self.storage.open_files(root)
        try:
            self._check()
        except BaseException:
            self.files.close()
            raise

    @classmethod
    def initialize(cls, root: Path, *, _storage: StorageBackend | None = None) -> FileSecrets:
        storage = _storage if _storage is not None else storage_backend()
        with storage.initialize(root, allow_empty=False) as files:
            files.require_private_root()
        return cls(root, _storage=storage)

    def _check(self) -> None:
        self.files.require_private_root()
        try:
            # Presence alone (including a broken link or damaged manifest) means
            # this cannot be a legacy plaintext backend. Never parse metadata as
            # an API key or fall back after a system-directory identity failure.
            exists = self.files.entry_exists(SYSTEM_SECRET_MANIFEST)
        except ContextError:
            raise ContextError(
                "credential_backend_mismatch", "凭据目录后端身份无法确认；未读取密钥。"
            ) from None
        if exists:
            raise ContextError(
                "credential_backend_mismatch", "系统凭据目录不能作为普通文件凭据打开；未读取密钥。"
            )
        self.files.require_private_root()

    @staticmethod
    def _ref(ref: str) -> str:
        valid_id(ref)
        if not ref.startswith("k_") or len(ref) != 34:
            raise ContextError("invalid_credential_reference", "凭据引用无效，不接受文件路径。")
        return ref

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c in value for c in "\r\n\x00"):
            raise ContextError("invalid_credential", "模型凭据无效；未回显输入。")
        return value

    def put(self, value: str) -> str:
        self._check()
        ref = "k_" + uuid.uuid4().hex
        self.files.write(ref, self._key(value).encode("utf-8"))
        return ref

    def get(self, ref: str) -> str:
        self._check()
        try:
            return self._key(self.files.read(self._ref(ref), max_bytes=16_384, private=True).decode("utf-8"))
        except ContextError as error:
            if error.code == "not_found":
                raise ContextError(
                    "credential_missing", "任务需要的独立模型凭据缺失；请恢复对应配置，不自动换用其他密钥。"
                ) from None
            raise
        except UnicodeError:
            raise ContextError("invalid_credential", "凭据文件编码无效；未回显内容。") from None

    def discard_new(self, ref: str) -> None:
        """Only the creator may use this to undo a definitively unreferenced new save."""
        self._check()
        try:
            self.files.unlink(self._ref(ref))
        except ContextError as error:
            if error.code != "not_found":
                raise
        except FileNotFoundError:
            pass
        except OSError:
            raise ContextError(
                "storage_unavailable", "新凭据回退未确认，文件保留；未删除已有凭据。"
            ) from None

    def close(self) -> None:
        self.files.close()

    def __repr__(self) -> str:
        return "FileSecrets(private service backend)"
