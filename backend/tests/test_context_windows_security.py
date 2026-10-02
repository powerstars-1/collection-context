"""Bounded policy and injected native-buffer tests, NOT Windows OS acceptance."""

from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace

import pytest
from test_context_windows_native import FakeDLLs, FakeFunction, code

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure import windows_security as security
from collection_context.infrastructure.platform_safety import detect_platform_safety

USER = bytes.fromhex("010500000000000515000000010000000200000003000000e8030000")
OTHER = USER[:-4] + (1001).to_bytes(4, "little")
EVERYONE = bytes.fromhex("010100000000000100000000")
USERS = bytes.fromhex("01020000000000052000000021020000")


def ace(sid=USER, *, kind=0, flags=0, mask=0x1F01FF):
    return struct.pack("<BBHI", kind, flags, len(sid) + 8, mask) + sid


def descriptor(*entries, owner=USER, control=0x9004, revision=2, padding=b""):
    acl = struct.pack("<BBHHH", revision, 0, 8 + sum(map(len, entries)) + len(padding), len(entries), 0)
    return (
        struct.pack("<BBHIIII", 1, 0, control, 20, 0, 0, 20 + len(owner))
        + owner
        + acl
        + b"".join(entries)
        + padding
    )


def replace(body, offset, format, value):
    result = bytearray(body)
    struct.pack_into(format, result, offset, value)
    return bytes(result)


def test_private_owner_system_and_administrators_allow_entries():
    value = security.private_descriptor(
        descriptor(ace(USER), ace(security._SYSTEM), ace(security._ADMINISTRATORS, flags=0x13)), USER
    )
    assert value.owner == USER and len(value.aces) == 3
    assert USER.hex() not in repr(value) and "1000" not in repr(value)


@pytest.mark.parametrize("revision", [2, 4])
@pytest.mark.parametrize("control", [0x8004, 0x9004, 0x940C])
def test_private_inherited_basic_acl_empty_acl_and_ignored_unused_space(revision, control):
    a = descriptor(ace(flags=0x1B), revision=revision, control=control, padding=b"abcd")
    b = a[:-4] + b"wxyz"
    assert security.private_descriptor(a, USER) == security.private_descriptor(b, USER)
    assert security.private_descriptor(descriptor(revision=revision, control=control), USER).aces == ()


@pytest.mark.parametrize("trustee", [OTHER, EVERYONE, USERS])
@pytest.mark.parametrize("flags", [0, 0x10, 0x0B])
def test_other_user_group_or_inherit_only_allow_cannot_be_private(trustee, flags):
    assert (
        code(lambda: security.private_descriptor(descriptor(ace(trustee, flags=flags)), USER))
        == "unsafe_secret_permissions"
    )


def test_deny_everyone_is_not_a_reason_to_ignore_an_unsafe_allow():
    safe = descriptor(ace(EVERYONE, kind=1), ace(USER))
    assert len(security.private_descriptor(safe, USER).aces) == 2
    assert (
        code(lambda: security.private_descriptor(descriptor(ace(EVERYONE, kind=1), ace(EVERYONE)), USER))
        == "unsafe_secret_permissions"
    )


@pytest.mark.parametrize("kind", [2, 4, 5, 6, 9, 11, 255])
def test_unknown_object_and_conditional_ace_is_not_silently_ignored(kind):
    assert (
        code(lambda: security.private_descriptor(descriptor(ace(kind=kind)), USER))
        == "unsafe_secret_permissions"
    )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        bytes(19),
        bytes(security.MAX_DESCRIPTOR + 1),
        descriptor(ace(), owner=OTHER),
        descriptor(ace(), control=0x1004),  # not self relative
        descriptor(ace(), control=0x9000),  # absent DACL
        descriptor(ace(), control=0x90C4),  # special untrusted semantics
        replace(descriptor(ace()), 0, "B", 2),
        replace(descriptor(ace()), 1, "B", 1),
        replace(descriptor(ace()), 4, "I", 0),
        replace(descriptor(ace()), 4, "I", 21),
        replace(descriptor(ace()), 12, "I", 20),
        replace(descriptor(ace()), 16, "I", 0),  # NULL DACL
        replace(descriptor(ace()), 16, "I", 20),  # overlaps owner SID
        replace(descriptor(ace()), 16, "I", 49),
        replace(descriptor(ace()), 16, "I", 65532),
        replace(descriptor(ace()), 20, "B", 2),
        replace(descriptor(ace()), 21, "B", 16),
        descriptor(ace(), revision=1),
        replace(descriptor(ace()), 49, "B", 1),
        replace(descriptor(ace()), 50, "H", 7),
        replace(descriptor(ace()), 50, "H", 65532),
        replace(descriptor(ace()), 52, "H", 4000),
        replace(descriptor(ace()), 54, "H", 1),
        replace(descriptor(ace()), 57, "B", 0x40),
        replace(descriptor(ace()), 58, "H", 15),
        replace(descriptor(ace()), 58, "H", 65532),
        replace(descriptor(ace()), 65, "B", 16),
        descriptor(ace())[:-1],
    ],
)
def test_malformed_or_unbounded_sd_dacl_ace_offsets_never_authorize(body):
    assert code(lambda: security.private_descriptor(body, USER)) == "unsafe_secret_permissions"


@pytest.mark.parametrize("user", [None, b"", USER + b"extra", replace(USER, 0, "B", 2)])
def test_invalid_current_user_sid_never_authorizes(user):
    assert code(lambda: security.private_descriptor(descriptor(ace()), user)) == "unsafe_secret_permissions"


class SecurityDLLs(FakeDLLs):
    def __init__(self):
        super().__init__()
        self.user = USER
        self.sd = descriptor(ace())
        self.thread_error = 1008
        self.thread_token = None
        self.process_ok = True
        self.token_handle = 777
        self.token_calls, self.security_calls = [], []
        self.query_first_error = 122
        self.token_capacity = None
        self.sd_capacity = None
        self.token_written = None
        self.sd_written = None
        self.token_pointer = "inside"
        self.token_attributes = 0
        self.token_copy_ok = True
        self.sd_copy_ok = True
        self.sd_hook = None
        self.user_hook = None
        self.kernel32.GetCurrentThread = FakeFunction(lambda: -2)
        self.kernel32.GetCurrentProcess = FakeFunction(lambda: -1)
        self.advapi = SimpleNamespace(
            OpenThreadToken=FakeFunction(self.open_thread),
            OpenProcessToken=FakeFunction(self.open_process),
            GetTokenInformation=FakeFunction(self.token_info),
            GetKernelObjectSecurity=FakeFunction(self.security_info),
        )
        self.native._security = security.WindowsPrivateSecurity(self.native, _dll=self.advapi)

    def open_thread(self, handle, access, as_self, output):
        assert (handle, access, as_self) == (-2, 8, 1)
        self.error = self.thread_error
        ctypes.cast(output, ctypes.POINTER(native.HANDLE)).contents.value = self.thread_token
        return int(self.thread_token is not None)

    def open_process(self, handle, access, output):
        assert (handle, access) == (-1, 8)
        ctypes.cast(output, ctypes.POINTER(native.HANDLE)).contents.value = self.token_handle
        return int(self.process_ok)

    def token_info(self, handle, kind, buffer, capacity, needed):
        self.token_calls.append((handle.value, kind, capacity))
        assert (handle.value, kind) == (777, 1)
        required = 16 + len(self.user)
        length = self.token_capacity if not capacity and self.token_capacity is not None else required
        if capacity and self.token_written is not None:
            length = self.token_written
        ctypes.cast(needed, ctypes.POINTER(native.U32)).contents.value = length
        if not capacity:
            self.error = self.query_first_error
            return 0
        if not self.token_copy_ok or capacity < required:
            return 0
        address = ctypes.addressof(buffer) + 16
        address = {
            "before": ctypes.addressof(buffer) - 4,
            "after": ctypes.addressof(buffer) + capacity + 4,
            "header": ctypes.addressof(buffer) + 8,
            "unaligned": ctypes.addressof(buffer) + 17,
            "null": 0,
        }.get(self.token_pointer, address)
        body = struct.pack("<QII", address, self.token_attributes, 0) + self.user
        ctypes.memmove(buffer, body, len(body))
        if self.user_hook is not None:
            self.user_hook()
        return 1

    def security_info(self, handle, flags, buffer, capacity, needed):
        self.security_calls.append((handle.value, flags, capacity))
        assert flags == 5  # Only owner and DACL, no privileged SACL.
        length = self.sd_capacity if not capacity and self.sd_capacity is not None else len(self.sd)
        if capacity and self.sd_written is not None:
            length = self.sd_written
        ctypes.cast(needed, ctypes.POINTER(native.U32)).contents.value = length
        if not capacity:
            self.error = self.query_first_error
            return 0
        if not self.sd_copy_ok or capacity < len(self.sd):
            return 0
        ctypes.memmove(buffer, self.sd, len(self.sd))
        if self.sd_hook is not None:
            self.sd_hook(handle.value)
        return 1


@pytest.fixture
def private():
    dlls = SecurityDLLs()
    with dlls.native.open_root_directory("C:\\原创库") as root:
        yield dlls, root


def test_actual_handle_and_bounded_sd_and_token_query_dispatch(private):
    dlls, root = private
    dlls.native.require_private_security(root)
    assert len(dlls.security_calls) == len(dlls.token_calls) == 4
    assert all(value == root._value and flags == 5 for value, flags, _ in dlls.security_calls)
    assert dlls.closed == [777, 777]
    assert dlls.advapi.GetKernelObjectSecurity.argtypes == [
        native.HANDLE,
        native.U32,
        native.HANDLE,
        native.U32,
        ctypes.POINTER(native.U32),
    ]
    assert dlls.advapi.GetKernelObjectSecurity.restype is native.I32
    assert dlls.advapi.OpenThreadToken.argtypes == [
        native.HANDLE,
        native.U32,
        native.I32,
        ctypes.POINTER(native.HANDLE),
    ]
    assert dlls.kernel32.GetCurrentProcess.restype is native.HANDLE
    assert dlls.flags[-2:] == [(777, 1, 0), (777, 1, 0)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_error", 5),
        ("thread_token", 888),
        ("process_ok", False),
        ("token_capacity", 23),
        ("token_capacity", 4097),
        ("sd_capacity", 19),
        ("sd_capacity", 65_537),
        ("query_first_error", 5),
        ("token_written", 5000),
        ("token_written", 1),
        ("sd_written", 70_000),
        ("sd_written", 1),
        ("token_copy_ok", False),
        ("sd_copy_ok", False),
        ("token_pointer", "before"),
        ("token_pointer", "after"),
        ("token_pointer", "header"),
        ("token_pointer", "unaligned"),
        ("token_pointer", "null"),
        ("token_attributes", 1),
    ],
)
def test_security_query_failures_never_fallback_or_authorize(private, field, value):
    dlls, root = private
    setattr(dlls, field, value)
    with pytest.raises(ContextError):
        dlls.native.require_private_security(root)
    if dlls.token_calls:
        assert 777 in dlls.closed


@pytest.mark.parametrize("role", ["read_file", "lease_file", "lease_observer"])
def test_file_roles_use_same_private_policy_and_do_not_read_body(private, role):
    dlls, root = private
    with dlls.native.open_relative(root, "secret.json", role=role) as file:
        dlls.native.require_private_security(file)
    assert not dlls.reads and not dlls.writes


def test_changed_unsafe_sd_is_not_ignored(private):
    dlls, root = private
    dlls.sd_hook = lambda _: setattr(dlls, "sd", descriptor(ace(EVERYONE)))
    assert code(lambda: dlls.native.require_private_security(root)) == "unsafe_secret_permissions"


def test_changed_safe_policy_and_identity_are_still_rejected(private):
    dlls, root = private
    dlls.sd_hook = lambda _: setattr(dlls, "sd", descriptor(ace(), ace(security._SYSTEM)))
    assert code(lambda: dlls.native.require_private_security(root)) == "version_changed"
    dlls.sd_hook = lambda value: dlls.files[value].update(id=b"changed identity")
    assert code(lambda: dlls.native.require_private_security(root)) == "lock_changed"


def test_impersonation_appearing_during_native_copy_does_not_pass(private):
    dlls, root = private
    dlls.sd_hook = lambda _: setattr(dlls, "thread_token", 888)
    assert code(lambda: dlls.native.require_private_security(root)) == "unsafe_secret_permissions"
    assert 888 in dlls.closed


def test_token_cleanup_failures_are_not_success(private):
    dlls, root = private
    dlls.close_ok = False
    try:
        assert code(lambda: dlls.native.require_private_security(root)) == "storage_unavailable"
        assert dlls.closed == [777]
    finally:
        dlls.close_ok = True


@pytest.mark.parametrize(
    "symbol", ["OpenThreadToken", "OpenProcessToken", "GetTokenInformation", "GetKernelObjectSecurity"]
)
def test_missing_native_security_symbol_fails_closed(private, symbol):
    dlls, root = private
    delattr(dlls.advapi, symbol)
    assert code(lambda: dlls.native.require_private_security(root)) == "storage_unavailable"
    assert not dlls.token_calls and not dlls.security_calls


def test_foreign_closed_handles_and_public_gate_do_not_get_authorization(private):
    dlls, root = private
    assert code(lambda: FakeDLLs().native.require_private_security(root)) == "storage_unavailable"
    root.close()
    assert code(lambda: dlls.native.require_private_security(root)) == "storage_unavailable"
    assert not dlls.security_calls
    assert detect_platform_safety().safe_files_backend != "windows_native"


def test_no_security_dll_load_at_import_or_constructor(private, monkeypatch):
    calls = []
    monkeypatch.setattr(security.ctypes, "WinDLL", lambda *args, **kwargs: calls.append(args), raising=False)
    security.WindowsPrivateSecurity(private[0].native)
    assert not calls


def test_private_read_checks_acl_on_both_sides_of_body_access(private):
    dlls, root = private
    with dlls.native.open_relative(root, "secret.json", role="read_file") as leaf:
        assert dlls.native.read_file(leaf, private=True) == b"fixture-body"
    assert len(dlls.security_calls) == 8 and len(dlls.token_calls) == 8
    assert dlls.reads and not dlls.writes


def test_unsafe_private_read_never_opens_content_io(private):
    dlls, root = private
    dlls.sd = descriptor(ace(EVERYONE))
    with dlls.native.open_relative(root, "secret.json", role="read_file") as leaf:
        assert code(lambda: dlls.native.read_file(leaf, private=True)) == "unsafe_secret_permissions"
    assert not dlls.reads and not dlls.seeks


def test_acl_changed_during_private_read_never_returns_the_body(private):
    dlls, root = private
    dlls.read_hook = lambda _: setattr(dlls, "sd", descriptor(ace(EVERYONE)))
    with dlls.native.open_relative(root, "secret.json", role="read_file") as leaf:
        with pytest.raises(ContextError) as caught:
            dlls.native.read_file(leaf, private=True)
        assert caught.value.code == "unsafe_secret_permissions" and "fixture-body" not in str(caught.value)
    assert dlls.reads


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_private_flag_must_be_explicit_boolean_before_native_query(private, flag):
    dlls, root = private
    with dlls.native.open_relative(root, "secret.json", role="read_file") as leaf:
        assert code(lambda: dlls.native.read_file(leaf, private=flag)) == "invalid_argument"
    assert not dlls.security_calls and not dlls.reads
