"""Exact Windows Credential Manager primitives, NOT a create-only backend.

CredWriteW is an upsert. This module must not be substituted for SystemSecrets'
create-only items until owned cross-process creation semantics are implemented.
No enumeration, file storage, shell, prompts, automatic fallback or retries.
The public platform/SystemSecrets gates remain unchanged. SDK injection is only
for offline tests, not a request, job, user configuration or platform override.

Original bindings based on Microsoft's public definitions:
https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw
https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew
https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw
https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-creddeletew
https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credfree
"""

from __future__ import annotations

import ctypes as C
import os
import platform
import re
import threading
from dataclasses import dataclass
from typing import Any

from collection_context.application.contracts import ContextError

U32 = C.c_uint32
BOOL = C.c_int32
PTR = C.c_void_p
_SERVICE = re.compile(r"org\.collection-context\.credentials\.[0-9a-f]{32}\Z")
_ACCOUNT = re.compile(r"k_[0-9a-f]{32}\Z")
MAX_CREDENTIAL_BYTES = 2560  # CRED_MAX_CREDENTIAL_BLOB_SIZE, not Python characters.
_MAX_BLOCK_SPAN = 8192  # Conservative pointer/read budget, not an allocation-size proof.
_GENERIC = 1
_LOCAL_MACHINE = 2  # Same user's subsequent sessions, not enterprise/roaming.


class FileTime(C.Structure):
    _fields_ = [("low", U32), ("high", U32)]


class CredentialW(C.Structure):
    # Explicit UTF-16 pointers: c_wchar/c_wchar_p have a different ABI on macOS.
    _fields_ = [
        ("Flags", U32),
        ("Type", U32),
        ("TargetName", PTR),
        ("Comment", PTR),
        ("LastWritten", FileTime),
        ("CredentialBlobSize", U32),
        ("CredentialBlob", PTR),
        ("Persist", U32),
        ("AttributeCount", U32),
        ("Attributes", PTR),
        ("TargetAlias", PTR),
        ("UserName", PTR),
    ]


@dataclass(frozen=True, repr=False)
class _NativeAPI:
    advapi32: Any
    get_last_error: Any


def _error(code: str) -> ContextError:
    messages = {
        "unsupported_platform": "Windows 系统凭据原语仅支持本机 Windows x64。",
        "invalid_credential_reference": "系统凭据引用不属于固定产品命名空间。",
        "invalid_credential": "系统凭据正文无效或超过 Windows 字节上限。",
        "credential_missing": "对应 Windows 系统凭据不存在。",
        "credential_locked": "Windows 登录会话没有可用凭据集，请在系统中恢复登录状态。",
        "credential_denied": "Windows 拒绝系统凭据操作。",
        "credential_unavailable": "Windows 系统凭据操作或返回结构无法确认。",
        "credential_outcome_unknown": "系统凭据更改结果未确认；保留可能存在的条目，不自动重试。",
    }
    return ContextError(code, messages[code])


def _sanitized(error: ContextError, *, mutation: bool = False) -> ContextError:
    allowed = {
        "unsupported_platform",
        "invalid_credential_reference",
        "invalid_credential",
        "credential_missing",
        "credential_locked",
        "credential_denied",
        "credential_unavailable",
        "credential_outcome_unknown",
    }
    code = (
        error.code
        if error.code in allowed
        else ("credential_outcome_unknown" if mutation else "credential_unavailable")
    )
    return _error(code)


def _check_layout() -> None:
    expected = {
        "Flags": 0,
        "Type": 4,
        "TargetName": 8,
        "Comment": 16,
        "LastWritten": 24,
        "CredentialBlobSize": 32,
        "CredentialBlob": 40,
        "Persist": 48,
        "AttributeCount": 52,
        "Attributes": 56,
        "TargetAlias": 64,
        "UserName": 72,
    }
    if (
        C.sizeof(PTR) != 8
        or C.sizeof(BOOL) != 4
        or C.sizeof(FileTime) != 8
        or C.sizeof(CredentialW) != 80
        or any(getattr(CredentialW, name).offset != offset for name, offset in expected.items())
    ):
        raise _error("unsupported_platform")


def _load_api() -> _NativeAPI:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise _error("unsupported_platform")
    try:
        return _NativeAPI(
            getattr(C, "WinDLL")("advapi32.dll", use_last_error=True, winmode=0x800),
            getattr(C, "get_last_error"),
        )
    except (AttributeError, OSError):
        raise _error("credential_unavailable") from None


def _target(service: str, account: str) -> str:
    if (
        not isinstance(service, str)
        or not _SERVICE.fullmatch(service)
        or not isinstance(account, str)
        or not _ACCOUNT.fullmatch(account)
    ):
        raise _error("invalid_credential_reference")
    return service + "/" + account


def _value(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise _error("invalid_credential")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _error("invalid_credential") from None
    if len(encoded) > MAX_CREDENTIAL_BYTES:
        raise _error("invalid_credential")
    return encoded


def _wide(text: str) -> Any:
    body = text.encode("utf-16-le") + b"\x00\x00"
    return (C.c_uint16 * (len(body) // 2)).from_buffer_copy(body)


class WindowsCredentialManager:
    """Fixed product generic credentials; set is explicitly upsert, never create-only."""

    storage_kind = "windows_credential_manager"

    def __init__(self, *, _api: _NativeAPI | None = None) -> None:
        _check_layout()
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._api = _api if _api is not None else _load_api()
            definitions = (
                ("CredWriteW", [C.POINTER(CredentialW), U32], BOOL),
                ("CredReadW", [PTR, U32, U32, C.POINTER(C.POINTER(CredentialW))], BOOL),
                ("CredDeleteW", [PTR, U32, U32], BOOL),
                ("CredFree", [PTR], None),
            )
            functions = [getattr(self._api.advapi32, name) for name, _, _ in definitions]
            for function, (_, arguments, result) in zip(functions, definitions, strict=True):
                function.argtypes, function.restype = arguments, result
            self._write, self._read, self._delete, self._free = functions
        except ContextError as error:
            raise _sanitized(error) from None
        except BaseException:
            raise _error("credential_unavailable") from None

    def _check(self) -> None:
        if self._closed:
            raise _error("credential_unavailable")

    def _failure(self) -> ContextError:
        try:
            status = self._api.get_last_error()
        except BaseException:
            return _error("credential_unavailable")
        return _error(
            {1168: "credential_missing", 1312: "credential_locked", 5: "credential_denied"}.get(
                status, "credential_unavailable"
            )
        )

    def set(self, service: str, account: str, value: str) -> None:
        target, body = _target(service, account), _value(value)
        with self._lock:
            self._check()
            name, user = _wide(target), _wide(account)
            blob = (C.c_ubyte * len(body)).from_buffer_copy(body)
            credential = CredentialW(
                Type=_GENERIC,
                TargetName=C.addressof(name),
                CredentialBlobSize=len(body),
                CredentialBlob=C.addressof(blob),
                Persist=_LOCAL_MACHINE,
                UserName=C.addressof(user),
            )
            try:
                if not self._write(C.byref(credential), 0):
                    raise self._failure()
            except ContextError as error:
                raise _sanitized(error, mutation=True) from None
            except BaseException:
                raise _error("credential_outcome_unknown") from None
            finally:
                C.memset(C.addressof(blob), 0, len(body))

    @staticmethod
    def _within(pointer: int | None, size: int, base: int, *, alignment: int = 1) -> int:
        # CredRead's pointers belong to its single trusted OS allocation. The API
        # supplies no allocation length: this caps all reads and rejects null,
        # struct-overlap, misalignment and implausible spans without pretending
        # to prove an arbitrary pointer's allocation extent.
        if (
            not pointer
            or pointer % alignment
            or pointer < base + C.sizeof(CredentialW)
            or pointer + size > base + _MAX_BLOCK_SPAN
        ):
            raise _error("credential_unavailable")
        return pointer

    @classmethod
    def _text(cls, pointer: int | None, expected: str, base: int) -> None:
        encoded = expected.encode("utf-16-le") + b"\x00\x00"
        address = cls._within(pointer, len(encoded), base, alignment=2)
        if C.string_at(address, len(encoded)) != encoded:
            raise _error("credential_unavailable")

    def get(self, service: str, account: str) -> str:
        target = _target(service, account)
        with self._lock:
            self._check()
            name = _wide(target)
            output = C.POINTER(CredentialW)()
            try:
                if not self._read(C.addressof(name), _GENERIC, 0, C.byref(output)):
                    raise self._failure()
                # The native API owns this one returned block; malformed
                # structures/decoding must not bypass the finally release.
                base = C.cast(output, PTR).value
                if not base or base % C.alignment(CredentialW):
                    raise _error("credential_unavailable")
                credential = output.contents
                size = credential.CredentialBlobSize
                if (
                    credential.Flags != 0
                    or credential.Type != _GENERIC
                    or credential.Persist != _LOCAL_MACHINE
                    or credential.AttributeCount != 0
                    or credential.Attributes
                    or credential.Comment
                    or credential.TargetAlias
                    or not 1 <= size <= MAX_CREDENTIAL_BYTES
                ):
                    raise _error("credential_unavailable")
                self._text(credential.TargetName, target, base)
                self._text(credential.UserName, account, base)
                address = self._within(credential.CredentialBlob, size, base)
                try:
                    result = C.string_at(address, size).decode("utf-8")
                except UnicodeError:
                    raise _error("invalid_credential") from None
                _value(result)
                return result
            except ContextError as error:
                raise _sanitized(error) from None
            except BaseException:
                raise _error("credential_unavailable") from None
            finally:
                if output:
                    try:
                        self._free(C.cast(output, PTR))
                    except BaseException:
                        raise _error("credential_unavailable") from None

    read = get

    def delete(self, service: str, account: str) -> None:
        target = _target(service, account)
        with self._lock:
            self._check()
            name = _wide(target)
            try:
                if not self._delete(C.addressof(name), _GENERIC, 0):
                    raise self._failure()
            except ContextError as error:
                raise _sanitized(error, mutation=True) from None
            except BaseException:
                raise _error("credential_outcome_unknown") from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
