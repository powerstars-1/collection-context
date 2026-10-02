"""Injected native dispatch/namespace tests; NOT Windows kernel acceptance."""

from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace

import pytest
from test_context_windows_native import FakeFunction, code
from test_context_windows_security import USER, SecurityDLLs, ace, descriptor

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import platform_safety
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure import windows_security as security
from collection_context.infrastructure.windows_publication import FileRenameInformation, WindowsPublication


class PublicationDLLs(SecurityDLLs):
    """Emulate the named outputs we assert, not kernel sharing or durability."""

    def __init__(self):
        super().__init__()
        self.names = {}
        self.descriptors = {}
        self.construction = []
        self.creation_fail = None
        self.created_information = 2
        self.created_status = 0
        self.rename_status = 0
        self.rename_io_status = 0
        self.rename_hook = None
        self.renames = []
        self.open_hook = None
        self.close_hook = None
        self.advapi.InitializeSecurityDescriptor = FakeFunction(self.initialize_sd)
        self.advapi.InitializeAcl = FakeFunction(self.initialize_acl)
        self.advapi.AddAccessAllowedAceEx = FakeFunction(self.add_ace)
        self.advapi.SetSecurityDescriptorOwner = FakeFunction(self.set_owner)
        self.advapi.SetSecurityDescriptorDacl = FakeFunction(self.set_dacl)
        self.advapi.SetSecurityDescriptorControl = FakeFunction(self.set_control)
        self.advapi.IsValidSecurityDescriptor = FakeFunction(lambda _: self.construct("valid"))
        self.ntdll.NtSetInformationFile = FakeFunction(self.rename)

    def construct(self, name):
        self.construction.append(name)
        return int(self.creation_fail != name)

    def initialize_sd(self, pointer, revision):
        assert revision == 1
        ctypes.cast(pointer, ctypes.POINTER(security.AbsoluteDescriptor)).contents.Revision = revision
        return self.construct("initialize_sd")

    def initialize_acl(self, buffer, size, revision):
        assert revision == 2
        ctypes.memmove(buffer, struct.pack("<BBHHH", revision, 0, size, 0, 0), 8)
        return self.construct("initialize_acl")

    def add_ace(self, buffer, revision, flags, mask, sid):
        assert revision == 2 and mask == 0x001F01FF and flags in {0, 3}
        user = sid.raw
        _, _, size, count, _ = struct.unpack_from("<BBHHH", buffer.raw)
        offset = 8
        for _ in range(count):
            offset += struct.unpack_from("<H", buffer.raw, offset + 2)[0]
        body = ace(user, flags=flags, mask=mask)
        assert offset + len(body) <= size
        ctypes.memmove(ctypes.addressof(buffer) + offset, body, len(body))
        ctypes.memmove(ctypes.addressof(buffer) + 4, struct.pack("<H", count + 1), 2)
        return self.construct("add_ace")

    def set_owner(self, pointer, sid, defaulted):
        assert defaulted == 0
        ctypes.cast(pointer, ctypes.POINTER(security.AbsoluteDescriptor)).contents.Owner = ctypes.addressof(
            sid
        )
        return self.construct("set_owner")

    def set_dacl(self, pointer, present, acl, defaulted):
        assert present == 1 and defaulted == 0
        sd = ctypes.cast(pointer, ctypes.POINTER(security.AbsoluteDescriptor)).contents
        sd.Dacl, sd.Control = ctypes.addressof(acl), sd.Control | 4
        return self.construct("set_dacl")

    def set_control(self, pointer, interest, bits):
        assert interest == bits == 0x1000
        sd = ctypes.cast(pointer, ctypes.POINTER(security.AbsoluteDescriptor)).contents
        sd.Control = (sd.Control & ~interest) | bits
        return self.construct("set_control")

    def open(self, output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size):
        request = ctypes.cast(attributes, ctypes.POINTER(native.ObjectAttributes)).contents
        string = request.ObjectName.contents
        name = ctypes.string_at(string.Buffer, string.Length).decode("utf-16-le")
        key = self.name_key(request.RootDirectory, name)
        existing = self.names.get(key)
        if disposition == 2 and existing is not None:
            return -1073741771  # C0000035 name collision
        if disposition == 1 and request.RootDirectory is not None and existing is None:
            return -1073741772  # C0000034 not found
        result = super().open(
            output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size
        )
        value = ctypes.cast(output, ctypes.POINTER(native.HANDLE)).contents.value
        if disposition == 2:
            assert request.SecurityDescriptor
            if options & 1:
                assert shares == 3 and not access & 0x40010000
            else:
                assert shares == 0 and access & 0x10000 and access & 0x40000000
            sd = ctypes.cast(request.SecurityDescriptor, ctypes.POINTER(security.AbsoluteDescriptor)).contents
            assert sd.Revision == 1 and sd.Control == 0x1004 and not sd.Group and not sd.Sacl
            # Only pointers produced by the owned construction buffers above.
            acl_size = struct.unpack("<H", ctypes.string_at(sd.Dacl + 2, 2))[0]
            owner = ctypes.string_at(sd.Owner, len(USER))
            body = struct.pack("<BBHIIII", 1, 0, sd.Control | 0x8000, 20, 0, 0, 20 + len(owner))
            self.descriptors[value] = body + owner + ctypes.string_at(sd.Dacl, acl_size)
            self.files[value].update(size=0, allocation=0, body=b"")
            self.positions[value] = 0  # New synchronous FILE_CREATE starts at EOF 0.
            self.names[key] = self.files[value]
            block = ctypes.cast(io, ctypes.POINTER(native.IoStatusBlock)).contents
            block.Information, block.Result.Status = self.created_information, self.created_status
        elif existing is not None:
            self.files[value] = existing
        else:
            self.names[key] = self.files[value]
        if self.open_hook is not None:
            self.open_hook(value, name, disposition)
        return result

    def name_key(self, parent, name):
        identity = None if parent is None else int.from_bytes(self.files[parent]["id"], "little")
        return identity, name.casefold()

    def security_info(self, handle, flags, buffer, capacity, needed):
        original = self.sd
        self.sd = self.descriptors.get(handle.value, self.sd)
        try:
            return super().security_info(handle, flags, buffer, capacity, needed)
        finally:
            self.sd = original

    def rename(self, handle, io, buffer, length, kind):
        header = ctypes.cast(buffer, ctypes.POINTER(FileRenameInformation)).contents
        assert kind == 10 and length >= ctypes.sizeof(FileRenameInformation) + header.FileNameLength
        raw = ctypes.string_at(ctypes.addressof(buffer), length)
        assert raw[1:8] == bytes(7) and header.ReplaceIfExists in {0, 1}
        assert raw[20 + header.FileNameLength :] == bytes(length - 20 - header.FileNameLength)
        name = raw[20 : 20 + header.FileNameLength].decode("utf-16-le")
        key = self.name_key(header.RootDirectory, name)
        self.renames.append((handle.value, key, bool(header.ReplaceIfExists)))
        if not header.ReplaceIfExists and key in self.names:
            return -1073741771
        if self.rename_hook is not None:
            self.rename_hook(handle.value, key)
        if self.rename_status:
            return self.rename_status
        entry = self.files[handle.value]
        for old_key, old_entry in list(self.names.items()):
            if old_entry is entry:
                del self.names[old_key]
        self.names[key] = entry
        block = ctypes.cast(io, ctypes.POINTER(native.IoStatusBlock)).contents
        block.Result.Status = self.rename_io_status
        return 0

    def close(self, handle):
        if self.close_hook is not None:
            self.close_hook(handle.value)
        return super().close(handle)


@pytest.fixture
def publication():
    dlls = PublicationDLLs()
    with dlls.native.open_root_directory("C:\\原创库") as root:
        yield dlls, root, WindowsPublication(dlls.native)


def test_fixed_native_descriptor_and_rename_layout_and_signatures(publication):
    dlls, root, writer = publication
    result = writer.write(root, "中文 😀.md", b"complete")
    assert result.file_id == (101).to_bytes(16, "little")
    assert ctypes.sizeof(security.AbsoluteDescriptor) == 40
    assert security.AbsoluteDescriptor.Dacl.offset == 32
    assert ctypes.sizeof(FileRenameInformation) == 24
    assert FileRenameInformation.RootDirectory.offset == 8
    assert FileRenameInformation.FileName.offset == 20
    assert dlls.ntdll.NtSetInformationFile.argtypes == [
        native.HANDLE,
        ctypes.POINTER(native.IoStatusBlock),
        native.HANDLE,
        native.U32,
        native.U32,
    ]
    assert dlls.advapi.SetSecurityDescriptorControl.argtypes == [native.HANDLE, native.U16, native.U16]
    assert dlls.advapi.AddAccessAllowedAceEx.argtypes == [
        native.HANDLE,
        native.U32,
        native.U32,
        native.U32,
        native.HANDLE,
    ]
    created = [request for request in dlls.opened if request["disposition"] == 2]
    assert len(created) == 1 and created[0]["root"] == root._value
    assert created[0]["security"] and created[0]["shares"] == 0
    assert not created[0]["access"] & 0xC0000  # no WRITE_DAC / WRITE_OWNER
    assert created[0]["options"] & 0x00200000
    policy = security.private_descriptor(dlls.descriptors[101], USER)
    assert policy.control == 0x9004 and len(policy.aces) == 3
    assert {entry[3] for entry in policy.aces} == {USER, security._SYSTEM, security._ADMINISTRATORS}
    assert all(entry[1] == 0 and entry[2] == 0x001F01FF for entry in policy.aces)


@pytest.mark.parametrize(
    "body",
    [b"", b"hello", "完整正文😀".encode(), b"x" * 131_073],
    ids=["empty", "short", "unicode", "multi-chunk"],
)
@pytest.mark.parametrize("write_limit", [7, 65_536])
def test_complete_flush_readback_then_single_publication(publication, body, write_limit):
    dlls, root, writer = publication
    dlls.write_limit = write_limit
    writer.write(root, "document.md", body)
    assert dlls.names[(root._value, "document.md")]["body"] == body
    assert len(dlls.renames) == 1 and dlls.renames[0][2] is False
    assert dlls.flushes == [101] and not dlls.truncations
    assert all(capacity <= 65_536 for _, capacity, _ in dlls.writes)
    assert not any(key[1].startswith(".context-") for key in dlls.names)
    assert 101 in dlls.closed and 102 in dlls.closed


def test_target_not_observed_until_complete_verified_body(publication):
    dlls, root, writer = publication
    observations = []
    dlls.write_hook = lambda _: observations.append((root._value, "target") in dlls.names)
    dlls.flush_hook = lambda _: observations.append((root._value, "target") in dlls.names)
    dlls.rename_hook = lambda value, _: observations.append(dlls.files[value]["body"] == b"final")
    writer.write(root, "target", b"final")
    assert observations == [False, False, True]


@pytest.mark.parametrize("replace", [False, True])
def test_collision_or_explicit_replace_native_decision(publication, replace):
    dlls, root, writer = publication
    writer.write(root, "target.md", b"old")
    dlls.renames.clear()
    if replace:
        writer.write(root, "target.md", b"new", replace=True)
        assert dlls.names[(root._value, "target.md")]["body"] == b"new"
    else:
        assert code(lambda: writer.write(root, "TARGET.md", b"new")) == "write_conflict"
        assert dlls.names[(root._value, "target.md")]["body"] == b"old"
    assert len(dlls.renames) == 1 and dlls.renames[0][2] is replace


@pytest.mark.parametrize(
    "component", ["", "..", "a/b", "a\\b", "x:stream", "CON", "x.", "x ", "\ud800", "a" * 256]
)
def test_bad_target_rejected_before_any_creation(publication, component):
    dlls, root, writer = publication
    assert code(lambda: writer.write(root, component, b"no")) == "forbidden_path"
    assert len(dlls.opened) == 1 and not dlls.construction and not dlls.writes


@pytest.mark.parametrize(
    "body, replace", [("text", False), (bytearray(b"x"), False), (b"x", 1), (b"x", None)]
)
def test_strict_body_and_replace_before_creation(publication, body, replace):
    dlls, root, writer = publication
    assert code(lambda: writer.write(root, "safe", body, replace=replace)) == "invalid_argument"
    assert len(dlls.opened) == 1


@pytest.mark.parametrize(
    "failure", ["initialize_sd", "initialize_acl", "add_ace", "set_owner", "set_dacl", "set_control", "valid"]
)
def test_security_construction_failure_never_creates(publication, failure):
    dlls, root, writer = publication
    dlls.creation_fail = failure
    assert code(lambda: writer.write(root, "safe", b"no")) == "storage_unavailable"
    assert len(dlls.opened) == 1 and not dlls.writes and not dlls.renames


def test_unavailable_rename_symbol_fails_before_creation(publication):
    dlls, root, writer = publication
    del dlls.ntdll.NtSetInformationFile
    assert code(lambda: writer.write(root, "safe", b"no")) == "unsupported_platform"
    assert len(dlls.opened) == 1 and not dlls.construction


@pytest.mark.parametrize("field, value", [("created_information", 1), ("created_status", -1)])
def test_exclusive_create_completion_must_confirm_created(publication, field, value):
    dlls, root, writer = publication
    setattr(dlls, field, value)
    assert code(lambda: writer.write(root, "safe", b"no")) == "storage_unavailable"
    assert not dlls.writes and not dlls.renames and 101 in dlls.closed


@pytest.mark.parametrize(
    "field, value",
    [
        ("write_ok", False),
        ("write_count_override", 0),
        ("write_count_override", 1000),
        ("flush_ok", False),
        ("read_ok", False),
    ],
)
def test_failed_write_or_verification_never_renames_or_retries(publication, field, value):
    dlls, root, writer = publication
    setattr(dlls, field, value)
    assert code(lambda: writer.write(root, "safe", b"complete")) == "storage_unavailable"
    assert not dlls.renames and (root._value, "safe") not in dlls.names
    assert len([entry for entry in dlls.opened if entry["disposition"] == 2]) == 1
    assert 101 in dlls.closed


def test_wrong_readback_never_publishes(publication):
    dlls, root, writer = publication
    dlls.flush_hook = lambda value: dlls.files[value].update(body=b"badbad", size=6)
    assert code(lambda: writer.write(root, "safe", b"proper")) == "storage_unavailable"
    assert not dlls.renames


@pytest.mark.parametrize(
    "field, value", [("rename_status", -1), ("rename_status", 259), ("rename_io_status", -1)]
)
def test_unknown_rename_status_does_not_report_success_retry_or_cleanup(publication, field, value):
    dlls, root, writer = publication
    setattr(dlls, field, value)
    assert code(lambda: writer.write(root, "safe", b"body")) == "storage_unavailable"
    assert len(dlls.renames) == 1 and 101 in dlls.closed


def test_post_rename_spelling_replacement_is_not_success(publication):
    dlls, root, writer = publication

    def change(value, name, disposition):
        if disposition == 1 and name == "safe":
            dlls.files[value] = dict(dlls.files[value], id=b"z" * 16)

    dlls.open_hook = change
    assert code(lambda: writer.write(root, "safe", b"body")) == "storage_unavailable"
    assert len(dlls.renames) == 1


def test_post_rename_body_change_is_not_success(publication):
    dlls, root, writer = publication

    def change(value, name, disposition):
        if disposition == 1 and name == "safe":
            dlls.files[value]["body"] = b"evil"

    dlls.open_hook = change
    assert code(lambda: writer.write(root, "safe", b"body")) == "storage_unavailable"


def test_closed_foreign_or_nondirectory_parent_cannot_write(publication):
    dlls, root, writer = publication
    other = PublicationDLLs()
    with other.native.open_root_directory("C:\\other") as foreign:
        assert code(lambda: writer.write(foreign, "safe", b"no")) == "storage_unavailable"
    leaf = native.NativeHandle(dlls.native, root._value, "read_file")
    assert code(lambda: writer.write(leaf, "safe", b"no")) == "forbidden_path"
    root.close()
    assert code(lambda: writer.write(root, "safe", b"no")) == "storage_unavailable"
    assert len(dlls.opened) == 1


def test_existing_arbitrary_file_cannot_be_opened_as_publication_handle(publication):
    dlls, root, _ = publication
    assert code(lambda: dlls.native.open_relative(root, "safe", role="publication_file")) == "forbidden_path"
    assert len(dlls.opened) == 1


def test_constructor_does_not_load_dll_or_change_public_platform_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(native.ctypes, "WinDLL", lambda *args, **kw: calls.append(args), raising=False)
    WindowsPublication(native.WindowsNative())
    assert calls == []
    monkeypatch.setattr(platform_safety, "os", SimpleNamespace(**{**vars(platform_safety.os), "name": "nt"}))
    report = platform_safety.detect_platform_safety()
    assert report.safe_files_backend is None and report.ownership_backend is None


def test_directory_descriptor_has_only_private_inheritable_trustees(publication):
    dlls, _, _ = publication
    result = dlls.native.private_security().creation_descriptor(directory=True)
    assert result.descriptor.Control == 0x1004
    assert result.descriptor.Owner == ctypes.addressof(result.sids[0])
    assert len(result.acl.raw) == 8 + sum(8 + len(value.raw) for value in result.sids)
    # The allocation owner remains reachable as long as the descriptor is used.
    assert result.descriptor.Dacl == ctypes.addressof(result.acl)
    assert result.user == USER and "1000" not in repr(result)
    position = 8
    for _ in range(3):
        kind, flags, size, mask = struct.unpack_from("<BBHI", result.acl.raw, position)
        assert (kind, flags, mask) == (0, 3, 0x001F01FF)
        position += size


def test_unsafe_parent_acl_does_not_create_or_repair(publication):
    dlls, root, writer = publication
    dlls.sd = descriptor(ace(bytes.fromhex("010100000000000100000000")))
    assert code(lambda: writer.write(root, "safe", b"no")) == "unsafe_secret_permissions"
    assert len(dlls.opened) == 1 and not dlls.construction


def test_staging_name_collision_is_never_opened_or_retried(publication, monkeypatch):
    dlls, root, writer = publication
    import collection_context.infrastructure.windows_publication as module

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    temp = ".context-" + "a" * 32 + ".tmp"
    original = {"body": b"untouched"}
    dlls.names[(root._value, temp)] = original
    assert code(lambda: writer.write(root, "target", b"new")) == "write_conflict"
    assert dlls.names[(root._value, temp)] is original
    assert not dlls.writes and not dlls.renames


@pytest.mark.parametrize("change", ["unsafe_acl", "nonempty", "hardlink", "reparse"])
def test_created_object_must_really_be_empty_private_ordinary_file(publication, change):
    dlls, root, writer = publication

    def changed(value, name, disposition):
        if disposition != 2:
            return
        if change == "unsafe_acl":
            dlls.descriptors[value] = descriptor(ace(bytes.fromhex("010100000000000100000000")))
        elif change == "nonempty":
            dlls.files[value].update(body=b"preexisting", size=11)
        elif change == "hardlink":
            dlls.files[value]["links"] = 2
        else:
            dlls.files[value]["tag"] = 1

    dlls.open_hook = changed
    assert code(lambda: writer.write(root, "target", b"new")) in {"storage_unavailable", "forbidden_path"}
    assert not dlls.writes and not dlls.renames and 101 in dlls.closed


def test_close_failure_after_rename_does_not_report_success_or_close_reused_handle(publication):
    dlls, root, writer = publication

    def close(value):
        dlls.closed.append(value.value)
        return int(value.value != 101)

    dlls.kernel32.CloseHandle = FakeFunction(close)
    assert code(lambda: writer.write(root, "safe", b"body")) == "storage_unavailable"
    assert len(dlls.renames) == 1 and dlls.closed.count(101) == 1
    assert not any(entry["name"] == "safe" for entry in dlls.opened)


def test_interruption_closes_only_owned_stage_and_does_not_publish(publication):
    dlls, root, writer = publication

    def interrupt(_):
        raise KeyboardInterrupt

    dlls.write_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        writer.write(root, "safe", b"body")
    assert not dlls.renames and dlls.closed.count(101) == 1 and root._value not in dlls.closed


def test_successful_native_rename_followed_by_verification_failure_is_unknown(publication):
    dlls, root, writer = publication

    def move_but_fail(value, key):
        dlls.names[key] = dlls.files[value]

    dlls.rename_hook, dlls.rename_status = move_but_fail, -1
    with pytest.raises(ContextError) as caught:
        writer.write(root, "safe", b"body")
    assert caught.value.code == "storage_unavailable" and not caught.value.retryable
    assert (root._value, "safe") in dlls.names and len(dlls.renames) == 1
    assert caught.value.next_action and "body" not in caught.value.message


@pytest.mark.parametrize("value", [None, 1, "directory"])
def test_invalid_security_creation_kind_rejected_before_native_calls(publication, value):
    dlls, _, _ = publication
    assert (
        code(lambda: dlls.native.private_security().creation_descriptor(directory=value))
        == "invalid_argument"
    )
    assert not dlls.construction
