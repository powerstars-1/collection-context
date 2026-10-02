"""Windows private-DACL checks on owned HANDLEs; not a selected storage backend.

Original implementation based on the native contracts, not external project code:
https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getkernelobjectsecurity
https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openthreadtoken
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-acl
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-ace_header

No account-name lookup, privilege enabling, identity impersonation, ACL modification,
keychain access, or path fallback. Native data is copied into bounded owned buffers
before inspecting offsets; token SID pointers may only address that buffer.
"""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass, field
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.windows_native import HANDLE, I32, U32, NativeHandle, WindowsNative

MAX_DESCRIPTOR = 65_536
MAX_TOKEN_USER = 4096
_SYSTEM = bytes.fromhex("010100000000000512000000")  # S-1-5-18
_ADMINISTRATORS = bytes.fromhex("01020000000000052000000020020000")  # S-1-5-32-544


def _unsafe() -> ContextError:
    return ContextError("unsafe_secret_permissions", "Windows 凭据对象的所有者或私有访问权限不安全。")


def _unavailable() -> ContextError:
    return ContextError("storage_unavailable", "Windows 权限信息无法安全核对；未读取其他账户。")


def sid_at(body: bytes, offset: int, *, end: int | None = None) -> bytes:
    """Parse a SID in already owned bytes; never dereference an arbitrary pointer."""
    if type(body) is not bytes:
        raise _unsafe()
    limit = len(body) if end is None else end
    if (
        type(offset) is not int
        or type(limit) is not int
        or not 0 <= offset <= limit <= len(body)
        or offset + 8 > limit
    ):
        raise _unsafe()
    revision, count = body[offset : offset + 2]
    end = offset + 8 + count * 4
    if revision != 1 or count > 15 or end > limit:
        raise _unsafe()
    return body[offset:end]


@dataclass(frozen=True, repr=False)
class PrivateDescriptor:
    # Do not disclose user SIDs in diagnostic reprs. Padding is not policy state.
    owner: bytes = field(repr=False)
    control: int
    revision: int
    aces: tuple[tuple[int, int, int, bytes], ...] = field(repr=False)


def private_descriptor(body: bytes, current_user: bytes) -> PrivateDescriptor:
    """Prove the bounded DACL grants only this user, SYSTEM and Administrators.

    This is a privacy predicate, not a full AccessCheck evaluator. Deny entries
    are never used to excuse an unsafe allow entry. Unsupported conditional/object
    ACEs are rejected, not treated as harmless. No absent/NULL DACL is accepted.
    Empty DACLs grant no access and are private; the kernel still checks usability.
    Inherited and inherit-only allows must obey the same trustee policy, protecting
    future child objects as well as the present one.
    """
    if type(body) is not bytes or not 20 <= len(body) <= MAX_DESCRIPTOR:
        raise _unsafe()
    if type(current_user) is not bytes or sid_at(current_user, 0) != current_user:
        raise _unsafe()
    revision, reserved, control, owner_offset, group_offset, sacl_offset, acl_offset = struct.unpack_from(
        "<BBHIIII", body
    )
    if (
        revision != 1
        or control & 0x8004 != 0x8004  # SELF_RELATIVE and DACL_PRESENT
        or control & 0xC0  # untrusted/server-security semantics are not accepted
        or reserved
        and not control & 0x4000  # resource-manager control byte
        or sacl_offset  # We request only OWNER and DACL, never SACL privilege.
        or owner_offset < 20
        or owner_offset % 4
        or acl_offset < 20
        or acl_offset % 4
        or acl_offset + 8 > len(body)
    ):
        raise _unsafe()
    owner = sid_at(body, owner_offset)
    if owner != current_user:
        raise _unsafe()
    sid_ranges = [(owner_offset, owner_offset + len(owner))]
    if group_offset:
        if group_offset < 20 or group_offset % 4:
            raise _unsafe()
        group = sid_at(body, group_offset)
        sid_ranges.append((group_offset, group_offset + len(group)))
    acl_revision, padding, acl_size, count, padding2 = struct.unpack_from("<BBHHH", body, acl_offset)
    acl_end = acl_offset + acl_size
    if (
        acl_revision not in {2, 4}
        or padding
        or padding2
        or acl_size < 8
        or acl_size % 4
        or acl_end > len(body)
        or count > (acl_size - 8) // 16
        or any(start < acl_end and end > acl_offset for start, end in sid_ranges)
        or len(sid_ranges) == 2
        and sid_ranges[0] != sid_ranges[1]
        and sid_ranges[0][0] < sid_ranges[1][1]
        and sid_ranges[1][0] < sid_ranges[0][1]
    ):
        raise _unsafe()
    position = acl_offset + 8
    entries = []
    for _ in range(count):
        if position + 16 > acl_end:
            raise _unsafe()
        kind, flags, size, mask = struct.unpack_from("<BBHI", body, position)
        if kind not in {0, 1} or flags & ~0x1F or size < 16 or size % 4 or position + size > acl_end:
            raise _unsafe()
        sid = sid_at(body, position + 8, end=position + size)
        if size != 8 + len(sid) or kind == 0 and sid not in {current_user, _SYSTEM, _ADMINISTRATORS}:
            raise _unsafe()
        entries.append((kind, flags, mask, sid))
        position += size
    return PrivateDescriptor(owner, control, acl_revision, tuple(entries))


class WindowsPrivateSecurity:
    def __init__(self, owner: WindowsNative, *, _dll: Any = None):
        self.owner, self._dll, self._bound = owner, _dll, False

    def _libraries(self):
        base = self.owner._libraries()
        if self._dll is None:
            if os.name != "nt":
                raise ContextError("unsupported_platform", "Windows 权限校验不可在其他系统上降级运行。")
            try:
                self._dll = getattr(ctypes, "WinDLL")("advapi32.dll", use_last_error=True, winmode=0x800)
            except (AttributeError, OSError):
                raise _unavailable() from None
        if not self._bound:
            signatures = [
                (base.kernel32, "GetCurrentProcess", HANDLE, []),
                (base.kernel32, "GetCurrentThread", HANDLE, []),
                (self._dll, "OpenProcessToken", I32, [HANDLE, U32, ctypes.POINTER(HANDLE)]),
                (self._dll, "OpenThreadToken", I32, [HANDLE, U32, I32, ctypes.POINTER(HANDLE)]),
                (self._dll, "GetTokenInformation", I32, [HANDLE, U32, HANDLE, U32, ctypes.POINTER(U32)]),
                (self._dll, "GetKernelObjectSecurity", I32, [HANDLE, U32, HANDLE, U32, ctypes.POINTER(U32)]),
            ]
            try:
                for dll, name, result, arguments in signatures:
                    function = getattr(dll, name)
                    function.restype, function.argtypes = result, arguments
            except AttributeError:
                raise _unavailable() from None
            self._bound = True
        return base, self._dll

    def _no_impersonation(self) -> None:
        base, api = self._libraries()
        token = HANDLE()
        opened = api.OpenThreadToken(base.kernel32.GetCurrentThread(), 8, 1, ctypes.byref(token))
        error = base.last_error()  # Capture before closing anything.
        if token.value not in {None, 0, (1 << 64) - 1}:
            if not base.kernel32.CloseHandle(token):
                raise _unavailable()
            raise _unsafe()
        if opened or error != 1008:  # ERROR_NO_TOKEN is the only safe absence.
            raise _unavailable()

    @staticmethod
    def _bounded_query(function, prefix: tuple, *, minimum: int, maximum: int, last_error) -> bytes:
        needed = U32()
        if function(*prefix, None, 0, ctypes.byref(needed)) or last_error() != 122:
            raise _unavailable()  # ERROR_INSUFFICIENT_BUFFER is expected, not guessed.
        capacity = int(needed.value)
        if not minimum <= capacity <= maximum:
            raise _unavailable()
        buffer, written = ctypes.create_string_buffer(capacity), U32()
        if not function(*prefix, buffer, capacity, ctypes.byref(written)):
            raise _unavailable()  # A changed required size is not automatically retried.
        if not minimum <= written.value <= capacity:
            raise _unavailable()
        return buffer.raw[: written.value]

    def current_user(self) -> bytes:
        self._no_impersonation()
        base, api = self._libraries()
        token = HANDLE()
        opened = api.OpenProcessToken(base.kernel32.GetCurrentProcess(), 8, ctypes.byref(token))
        try:
            if not opened or token.value in {None, 0, (1 << 64) - 1}:
                raise _unavailable()
            if not base.kernel32.SetHandleInformation(token, 1, 0):
                raise _unavailable()
            # TOKEN_USER contains a pointer into its allocated buffer. Copying the
            # buffer first must not make that pointer an unchecked dereference.
            needed = U32()
            if api.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed)) or base.last_error() != 122:
                raise _unavailable()
            capacity = int(needed.value)
            if not 24 <= capacity <= MAX_TOKEN_USER:
                raise _unavailable()
            buffer, written = ctypes.create_string_buffer(capacity), U32()
            if not api.GetTokenInformation(token, 1, buffer, capacity, ctypes.byref(written)):
                raise _unavailable()
            if not 24 <= written.value <= capacity:
                raise _unavailable()
            body = buffer.raw[: written.value]
            if struct.unpack_from("<I", body, 8)[0] != 0:  # TOKEN_USER attributes must be zero.
                raise _unavailable()
            address = struct.unpack_from("<Q", body)[0]
            offset = address - ctypes.addressof(buffer)
            if offset < 16 or offset % 4:
                raise _unavailable()
            user = sid_at(body, offset)
            self._no_impersonation()
            return user
        finally:
            if token.value not in {None, 0, (1 << 64) - 1}:
                if not base.kernel32.CloseHandle(token):
                    raise _unavailable()

    def descriptor(self, handle: NativeHandle) -> bytes:
        base, api = self._libraries()
        value = HANDLE(self.owner._value(handle))
        # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION; no SACL.
        return self._bounded_query(
            api.GetKernelObjectSecurity,
            (value, 5),
            minimum=20,
            maximum=MAX_DESCRIPTOR,
            last_error=base.last_error,
        )

    def require_private(self, handle: NativeHandle) -> None:
        self.owner._value(handle)
        with handle._io_lock:
            before = self.owner.information(handle)
            user = self.current_user()
            policy = private_descriptor(self.descriptor(handle), user)
            after_policy = private_descriptor(self.descriptor(handle), self.current_user())
            after = self.owner.information(handle)
            if before != after or policy != after_policy:
                raise ContextError("version_changed", "权限或对象版本在核对期间变化；未继续操作。")
