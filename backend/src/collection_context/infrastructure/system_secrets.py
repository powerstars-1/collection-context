"""Exact system credential items with private, key-free local confirmation metadata.

No keychain enumeration, foreign-key import, plaintext fallback, overwrite, or
automatic retry. The caller owns the proof that a new reference was not delivered
to a committed model profile before asking for rollback. An uncertain outcome
preserves the system item and requires explicit diagnosis rather than guessing.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, NoReturn, Protocol

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import SYSTEM_SECRET_MANIFEST, FileSecrets

MANIFEST = SYSTEM_SECRET_MANIFEST
_HEX = re.compile(r"[0-9a-f]{32}")
_REF = re.compile(r"k_[0-9a-f]{32}")
_ERRORS = {
    "credential_locked": "系统凭据库已锁定；未回显凭据。",
    "credential_denied": "系统拒绝凭据访问；未回显凭据。",
    "credential_conflict": "系统凭据引用已存在；没有覆盖或重试。",
    "invalid_credential_reference": "系统凭据引用无效；未回显输入。",
    "invalid_credential": "系统凭据输入无效；未回显凭据。",
    "credential_missing": "系统凭据不存在；没有读取其他条目或回退到文件。",
    "system_secret_unsupported": "此平台尚未支持系统凭据；没有降级为明文文件。",
    "credential_unavailable": "系统凭据服务不可用；没有降级为明文文件。",
}


class _SystemItems(Protocol):
    def create(self, service: str, account: str, value: str) -> None: ...

    def read(self, service: str, account: str) -> str: ...

    def delete(self, service: str, account: str) -> None: ...


def _backend_or_default(backend: _SystemItems | None) -> _SystemItems:
    if backend is not None:
        return backend  # Private offline-test seam, never a request/job parameter.
    if sys.platform != "darwin":
        raise ContextError("system_secret_unsupported", _ERRORS["system_secret_unsupported"])
    try:
        from collection_context.infrastructure.macos_credentials import MacOSKeychain

        return MacOSKeychain()
    except BaseException:
        raise ContextError("credential_unavailable", _ERRORS["credential_unavailable"]) from None


def _private_root(files: SafeFiles) -> None:
    files.require_private_root()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError


def _unknown() -> ContextError:
    return ContextError(
        "system_secret_outcome_unknown",
        "系统凭据提交或回退结果尚未确认，可能存在的条目保留；不要自动重试或覆盖。",
    )


class SystemSecrets:
    """macOS-only storage; constructor and initialize never query a system item.

    Only fresh successfully-created references can be rolled back by their owning
    instance. Successful get or close removes that rollback eligibility. Because
    this protocol cannot see a library's commit, callers must not discard a
    reference once it has been delivered elsewhere or the commit is uncertain.
    No other instance or old FileSecrets directory can supply rollback authority.
    """

    storage_kind = "macos_keychain"

    def __init__(self, root: Path, *, _backend: _SystemItems | None = None):
        self._backend = _backend_or_default(_backend)
        self.files = SafeFiles(root)
        self._lock = threading.RLock()
        self._new: set[str] = set()
        self._closed = False
        try:
            _private_root(self.files)
            manifest = self._load(MANIFEST, directory=True)
            if (
                set(manifest) != {"schema_version", "backend", "namespace"}
                or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != 1
                or manifest["backend"] != "macos_keychain"
                or not isinstance(manifest["namespace"], str)
                or not _HEX.fullmatch(manifest["namespace"])
            ):
                raise ValueError
            self._manifest = manifest
            self._service = "org.collection-context.credentials." + manifest["namespace"]
        except (ValueError, TypeError, KeyError):
            self.files.close()
            raise ContextError(
                "system_secret_directory_invalid", "目录不是已确认的系统凭据目录；不会迁移旧密钥。"
            ) from None
        except BaseException:
            self.files.close()
            raise

    @classmethod
    def initialize(cls, root: Path, *, _backend: _SystemItems | None = None) -> SystemSecrets:
        backend = _backend_or_default(_backend)
        root = root.absolute()
        if root.is_symlink() or root.exists():
            raise ContextError("secret_directory_exists", "只创建新的独立系统凭据目录；不迁移已有目录。")
        try:
            root.mkdir(parents=True, mode=0o700)
        except OSError:
            raise ContextError("storage_unavailable", "系统凭据目录未能建立；未访问系统凭据。") from None
        with SafeFiles(root) as files:
            _private_root(files)
            files.write(
                MANIFEST,
                canonical_bytes(
                    {"schema_version": 1, "backend": "macos_keychain", "namespace": uuid.uuid4().hex}
                ),
            )
        return cls(root, _backend=backend)

    def _load(self, name: str, *, directory: bool = False) -> dict[str, Any]:
        try:
            payload = self.files.read(name, max_bytes=4096, private=True)
            value = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant
            )
            if not isinstance(value, dict):
                raise ValueError
            canonical_bytes(value)  # Reject unpaired Unicode and noncanonical values without echoing them.
            return value
        except ContextError as error:
            if error.code == "not_found":
                code = "system_secret_directory_invalid" if directory else "credential_missing"
                raise ContextError(code, "凭据确认元数据不存在；未访问系统条目或读取旧密钥。") from None
            raise
        except (ValueError, UnicodeError, TypeError):
            raise ContextError(
                "system_secret_metadata_invalid", "凭据确认元数据损坏；未访问系统条目。"
            ) from None

    def _check(self) -> None:
        if self._closed:
            raise ContextError("system_secret_closed", "此系统凭据句柄已关闭。")
        _private_root(self.files)
        if canonical_bytes(self._load(MANIFEST, directory=True)) != canonical_bytes(self._manifest):
            raise ContextError(
                "system_secret_directory_changed", "系统凭据目录身份已变化；未访问其他命名空间。"
            )

    @staticmethod
    def _ref(ref: str) -> str:
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise ContextError("invalid_credential_reference", "系统凭据引用无效，不接受文件路径。")
        return ref

    def _metadata(self, ref: str) -> dict[str, Any]:
        return {**self._manifest, "ref": ref, "confirmed": True}

    def _confirmed(self, ref: str) -> None:
        if canonical_bytes(self._load(ref)) != canonical_bytes(self._metadata(ref)):
            raise ContextError("system_secret_metadata_invalid", "凭据确认不匹配；未访问其他系统条目。")

    @staticmethod
    def _system_error(error: BaseException, *, mutation: bool) -> ContextError:
        if (
            isinstance(error, ContextError)
            and error.code in _ERRORS
            and not (mutation and error.code == "credential_unavailable")
        ):
            return ContextError(error.code, _ERRORS[error.code])
        if mutation:
            return _unknown()
        return ContextError("credential_unavailable", _ERRORS["credential_unavailable"])

    def put(self, value: str) -> str:
        with self._lock:
            self._check()
            value = FileSecrets._key(value)
            ref = "k_" + uuid.uuid4().hex
            try:
                self._backend.create(self._service, ref, value)
            except BaseException as error:
                raise self._system_error(error, mutation=True) from None
            try:
                self._check()
                self.files.write(ref, canonical_bytes(self._metadata(ref)))
                self._confirmed(ref)
            except BaseException:
                # Publication may have happened before fsync/verification failed.
                # Never delete a possibly referenced system item on this evidence.
                raise _unknown() from None
            self._new.add(ref)
            return ref

    def get(self, ref: str) -> str:
        with self._lock:
            self._check()
            ref = self._ref(ref)
            self._confirmed(ref)
            # A reference used for retrieval is no longer an un-delivered save.
            self._new.discard(ref)
            try:
                value = self._backend.read(self._service, ref)
            except BaseException as error:
                raise self._system_error(error, mutation=False) from None
            self._check()
            self._confirmed(ref)
            return FileSecrets._key(value)

    def discard_new(self, ref: str) -> None:
        with self._lock:
            self._check()
            ref = self._ref(ref)
            if ref not in self._new:
                raise ContextError(
                    "system_secret_rollback_forbidden", "仅当前实例明确未交付的新引用可回退；未删除系统条目。"
                )
            self._confirmed(ref)
            # Any attempted delete consumes rollback authority, even when the OS
            # returns an ambiguous result. Never retry a potentially finished delete.
            self._new.remove(ref)
            try:
                self._backend.delete(self._service, ref)
            except BaseException as error:
                raise self._system_error(error, mutation=True) from None
            try:
                self._check()
                self._confirmed(ref)
                self.files.unlink(ref)
            except BaseException:
                raise _unknown() from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._new.clear()
            self.files.close()

    def __repr__(self) -> str:
        return "SystemSecrets(macos_keychain; no credential values)"
