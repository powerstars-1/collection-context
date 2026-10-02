"""Create/read/delete for cooperating product instances sharing ONE private root.

CredWriteW itself is upsert, not create-only. The product's directory-anchored,
nonblocking WindowsWriterLease protects absence-check plus write for cooperating
instances bound to this root/namespace. Different copied roots and external
same-user programs that call Credential Manager directly are NOT protected.
Consumers must enforce one root per namespace; neither root nor SDK injection
is a request/job parameter. Public SystemSecrets/platform gates stay closed.

No credential value is written to files; the existing lease stores only its
nonce/PID. Constructors inspect existing handles/DACL, never initialize a root,
acquire a write lease or query Credential Manager. Uncertain writes preserve the
possible item: no automatic retry, rollback, delete, enumeration or takeover.

https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
"""

from __future__ import annotations

import re
import threading
from typing import Protocol

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.windows_files import WindowsFiles
from collection_context.infrastructure.windows_native import WindowsNative
from collection_context.infrastructure.windows_ownership import WindowsWriterLease
from collection_context.infrastructure.windows_system_secrets import (
    WindowsCredentialManager,
    _target,
    _value,
)

_NAMESPACE = re.compile(r"[0-9a-f]{32}\Z")


class _Manager(Protocol):
    def set(self, service: str, account: str, value: str) -> None: ...

    def get(self, service: str, account: str) -> str: ...

    def delete(self, service: str, account: str) -> None: ...

    def close(self) -> None: ...


def _error(code: str) -> ContextError:
    messages = {
        "unsupported_platform": "此凭据适配需要本机 Windows 原生能力。",
        "invalid_credential_reference": "凭据不属于此实例固定的产品命名空间。",
        "invalid_credential": "凭据正文无效或超过 Windows 字节上限。",
        "credential_conflict": "此系统凭据已存在；没有覆盖或删除。",
        "credential_missing": "对应系统凭据不存在。",
        "credential_locked": "凭据目录正被其他协同实例使用或系统凭据集不可用。",
        "credential_denied": "凭据目录权限或系统访问被拒绝。",
        "credential_unavailable": "凭据目录或系统结果无法安全确认。",
        "credential_outcome_unknown": "系统凭据更改结果未确认；保留可能存在的条目，不自动重试或清理。",
    }
    return ContextError(code, messages[code])


def _read_error(error: BaseException) -> ContextError:
    if isinstance(error, ContextError):
        if error.code in {
            "unsupported_platform",
            "invalid_credential_reference",
            "invalid_credential",
            "credential_conflict",
            "credential_missing",
            "credential_locked",
            "credential_denied",
        }:
            return _error(error.code)
        if error.code == "writer_busy":
            return _error("credential_locked")
        if error.code == "unsafe_secret_permissions":
            return _error("credential_denied")
    return _error("credential_unavailable")


class WindowsCredentialItems:
    """Cooperative create-only items, not a global Credential Manager CAS primitive."""

    storage_kind = "windows_credential_manager"

    def __init__(
        self,
        root: str,
        namespace: str,
        *,
        _manager: _Manager | None = None,
        _native: WindowsNative | None = None,
    ) -> None:
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise _error("invalid_credential_reference")
        self._service = "org.collection-context.credentials." + namespace
        self._lock = threading.RLock()
        self._closed = False
        self._owns_manager = _manager is None
        self.files: WindowsFiles | None = None
        self._manager: _Manager | None = None
        try:
            self.files = WindowsFiles(root, _native=_native)
            self.files.require_private_root()
            self._identity = self.files.identity
            self._manager = _manager if _manager is not None else WindowsCredentialManager()
        except BaseException as error:
            if self.files is not None:
                try:
                    self.files.close()
                except BaseException:
                    pass  # Preserve original failure, never retry native cleanup.
            self._closed = True
            raise _read_error(error) from None

    def _reference(self, service: str, account: str) -> None:
        _target(service, account)
        if service != self._service:
            raise _error("invalid_credential_reference")

    def _check(self, lease: WindowsWriterLease | None = None) -> WindowsFiles:
        if self._closed or self.files is None:
            raise _error("credential_unavailable")
        self.files.require_private_root()
        if self.files.identity != self._identity:
            raise _error("credential_unavailable")
        if lease is not None:
            lease.check()
            lease.files.require_private_root()
            if lease.files.identity != self._identity:
                raise _error("credential_unavailable")
            self.files.require_private_root()
            lease.check()
        return self.files

    def create(self, service: str, account: str, value: str) -> None:
        self._reference(service, account)
        _value(value)  # Before acquiring a lease or querying the OS.
        with self._lock:
            attempted = False
            try:
                files = self._check()
                with WindowsWriterLease(
                    files.root, _native=files.native, _expected_identity=self._identity
                ) as lease:
                    self._check(lease)
                    assert self._manager is not None
                    try:
                        self._manager.get(service, account)
                    except ContextError as error:
                        if error.code != "credential_missing":
                            raise
                    else:
                        # Existence, not equality with the requested value, wins.
                        raise _error("credential_conflict")
                    self._check(lease)
                    attempted = True
                    self._manager.set(service, account, value)
                    self._check(lease)
                    if self._manager.get(service, account) != value:
                        raise _error("credential_outcome_unknown")
                    self._check(lease)
                self._check()  # Closing the kernel lease must complete before success.
            except BaseException as error:
                raise (_error("credential_outcome_unknown") if attempted else _read_error(error)) from None

    def read(self, service: str, account: str) -> str:
        self._reference(service, account)
        with self._lock:
            try:
                files = self._check()
                with WindowsWriterLease(
                    files.root, _native=files.native, _expected_identity=self._identity
                ) as lease:
                    self._check(lease)
                    assert self._manager is not None
                    result = self._manager.get(service, account)
                    _value(result)
                    self._check(lease)
                self._check()
                return result
            except BaseException as error:
                raise _read_error(error) from None

    def delete(self, service: str, account: str) -> None:
        self._reference(service, account)
        with self._lock:
            attempted = False
            try:
                files = self._check()
                with WindowsWriterLease(
                    files.root, _native=files.native, _expected_identity=self._identity
                ) as lease:
                    self._check(lease)
                    assert self._manager is not None
                    # Confirm absence before mutation so caller rollback on an
                    # already-missing new reference can remain explicit/idempotent.
                    self._manager.get(service, account)
                    self._check(lease)
                    attempted = True
                    self._manager.delete(service, account)
                    self._check(lease)
                    try:
                        self._manager.get(service, account)
                    except ContextError as error:
                        if error.code != "credential_missing":
                            raise
                    else:
                        raise _error("credential_outcome_unknown")
                    self._check(lease)
                self._check()
            except BaseException as error:
                raise (_error("credential_outcome_unknown") if attempted else _read_error(error)) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            failed = False
            for resource in (self.files, self._manager if self._owns_manager else None):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException:
                        failed = True
            if failed:
                raise _error("credential_unavailable")
