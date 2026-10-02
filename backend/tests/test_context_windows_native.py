"""ABI/dispatch tests using injected DLLs; NOT Windows native acceptance."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.platform_safety import detect_platform_safety


class FakeFunction:
    def __init__(self, call):
        self.call = call
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self.call(*arguments)


class FakeDLLs:
    """Only emulate output buffers and dispatch, not kernel semantics."""

    def __init__(self):
        self.next_handle = 100
        self.opened = []
        self.closed = []
        self.flags = []
        self.files = {}
        self.information_classes = []
        self.locks = []
        self.unlocks = []
        self.status = 0
        self.error = 0
        self.inherit_ok = True
        self.close_ok = True
        self.file_type = 1
        self.drive_type = 3
        self.drives = []
        self.device_target = "\\Device\\HarddiskVolume4"
        self.device_queries = []
        self.device_count = None
        self.device_hook = None
        self.info_failure = None
        self.lock_ok = True
        self.unlock_ok = True
        self.bad_open_info = {}
        self.positions = {}
        self.seeks = []
        self.reads = []
        self.seek_ok = True
        self.seek_result = 0
        self.read_ok = True
        self.read_limit = 65_536
        self.read_count_override = None
        self.read_hook = None
        self.writes = []
        self.truncations = []
        self.flushes = []
        self.write_ok = True
        self.write_limit = 65_536
        self.write_count_override = None
        self.write_hook = None
        self.truncate_ok = True
        self.flush_ok = True
        self.flush_hook = None
        self.kernel32 = SimpleNamespace(
            SetHandleInformation=FakeFunction(self.set_flags),
            CloseHandle=FakeFunction(self.close),
            GetFileType=FakeFunction(lambda _: self.file_type),
            GetDriveTypeW=FakeFunction(self.drive),
            QueryDosDeviceW=FakeFunction(self.query_device),
            GetFileInformationByHandleEx=FakeFunction(self.info),
            SetFilePointerEx=FakeFunction(self.seek),
            ReadFile=FakeFunction(self.read),
            WriteFile=FakeFunction(self.write),
            SetEndOfFile=FakeFunction(self.truncate),
            FlushFileBuffers=FakeFunction(self.flush),
            LockFileEx=FakeFunction(self.lock),
            UnlockFileEx=FakeFunction(self.unlock),
        )
        self.ntdll = SimpleNamespace(NtCreateFile=FakeFunction(self.open))
        self.native = native.WindowsNative(_dlls=native._DLLs(self.kernel32, self.ntdll, lambda: self.error))

    def drive(self, pointer):
        self.drives.append(ctypes.string_at(pointer, 6).decode("utf-16-le"))
        return self.drive_type

    def query_device(self, device, target, capacity):
        self.device_queries.append((ctypes.string_at(device, 4).decode("utf-16-le"), capacity))
        body = (self.device_target + "\x00\x00").encode("utf-16-le")
        if len(body) > capacity * 2:
            return 0
        ctypes.memmove(target, body, len(body))
        if self.device_hook is not None:
            self.device_hook()
        return len(body) // 2 if self.device_count is None else self.device_count

    def open(self, output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size):
        request = ctypes.cast(attributes, ctypes.POINTER(native.ObjectAttributes)).contents
        name = request.ObjectName.contents
        decoded = ctypes.string_at(name.Buffer, name.Length).decode("utf-16-le")
        self.opened.append(
            {
                "name": decoded,
                "length": name.Length,
                "capacity": name.MaximumLength,
                "root": request.RootDirectory,
                "attributes": request.Attributes,
                "access": access,
                "shares": shares,
                "disposition": disposition,
                "options": options,
                "security": request.SecurityDescriptor,
                "allocation": allocation,
                "ea": ea,
            }
        )
        value = self.next_handle
        self.next_handle += 1
        ctypes.cast(output, ctypes.POINTER(native.HANDLE)).contents.value = value
        directory = bool(options & 1)
        self.files[value] = {
            "access": access,
            "directory": directory,
            "links": 1,
            "size": 12,
            "allocation": 4096,
            "attributes": 0x10 if directory else 0x80,
            "tag": 0,
            "delete": 0,
            "id": value.to_bytes(16, "little"),
            "body": b"fixture-body",
            **self.bad_open_info,
        }
        return self.status

    def seek(self, handle, distance, output, method):
        self.seeks.append((handle.value, distance.value, method))
        self.positions[handle.value] = self.seek_result
        ctypes.cast(output, ctypes.POINTER(native.I64)).contents.value = self.seek_result
        return int(self.seek_ok)

    def read(self, handle, buffer, capacity, count, overlap):
        self.reads.append((handle.value, capacity, overlap))
        if not self.read_ok:
            return 0
        start = self.positions[handle.value]
        body = self.files[handle.value]["body"][start : start + min(capacity, self.read_limit)]
        ctypes.memmove(buffer, body, len(body))
        self.positions[handle.value] += len(body)
        ctypes.cast(count, ctypes.POINTER(native.U32)).contents.value = (
            len(body) if self.read_count_override is None else self.read_count_override
        )
        if self.read_hook is not None:
            self.read_hook(handle.value)
        return 1

    def set_flags(self, handle, mask, value):
        self.flags.append((handle.value, mask, value))
        return int(self.inherit_ok)

    def write(self, handle, buffer, capacity, count, overlap):
        assert self.files[handle.value]["access"] & 0x40000000
        self.writes.append((handle.value, capacity, overlap))
        if not self.write_ok:
            return 0
        length = min(capacity, self.write_limit)
        body = ctypes.string_at(buffer, length)
        data, start = self.files[handle.value], self.positions[handle.value]
        data["body"] = data["body"][:start] + body + data["body"][start + length :]
        data["size"] = len(data["body"])
        self.positions[handle.value] += length
        ctypes.cast(count, ctypes.POINTER(native.U32)).contents.value = (
            length if self.write_count_override is None else self.write_count_override
        )
        if self.write_hook is not None:
            self.write_hook(handle.value)
        return 1

    def truncate(self, handle):
        assert self.files[handle.value]["access"] & 0x40000000
        self.truncations.append(handle.value)
        if not self.truncate_ok:
            return 0
        data = self.files[handle.value]
        data["body"] = data["body"][: self.positions[handle.value]]
        data["size"] = len(data["body"])
        return 1

    def flush(self, handle):
        assert self.files[handle.value]["access"] & 0x40000000
        self.flushes.append(handle.value)
        if self.flush_hook is not None:
            self.flush_hook(handle.value)
        return int(self.flush_ok)

    def close(self, handle):
        self.closed.append(handle.value)
        return int(self.close_ok)

    def info(self, handle, kind, output, length):
        self.information_classes.append((handle.value, kind, length))
        if kind == self.info_failure:
            return 0
        data = self.files[handle.value]
        if kind == 18:
            result = ctypes.cast(output, ctypes.POINTER(native.FileIdInfo)).contents
            result.VolumeSerialNumber = 0xAABBCCDDEEFF0011
            result.FileId[:] = data["id"]
        elif kind == 1:
            result = ctypes.cast(output, ctypes.POINTER(native.FileStandardInfo)).contents
            result.EndOfFile = data["size"]
            result.AllocationSize = data["allocation"]
            result.NumberOfLinks = data["links"]
            result.Directory = int(data["directory"])
            result.DeletePending = data["delete"]
        elif kind == 0:
            result = ctypes.cast(output, ctypes.POINTER(native.FileBasicInfo)).contents
            result.CreationTime = data.get("creation_time", 1)
            result.LastWriteTime = data.get("last_write_time", 2)
            result.ChangeTime = data.get("change_time", 3)
            result.FileAttributes = data["attributes"]
        elif kind == 9:
            result = ctypes.cast(output, ctypes.POINTER(native.FileAttributeTagInfo)).contents
            result.FileAttributes = data["attributes"]
            result.ReparseTag = data["tag"]
        else:
            raise AssertionError("unexpected information class")
        return 1

    def lock(self, handle, flags, reserved, low, high, pointer):
        overlap = ctypes.cast(pointer, ctypes.POINTER(native.Overlapped)).contents
        self.locks.append(
            (
                handle.value,
                flags,
                reserved,
                low,
                high,
                overlap.Position.Offsets.Offset,
                overlap.Position.Offsets.OffsetHigh,
                overlap.hEvent,
            )
        )
        return int(self.lock_ok)

    def unlock(self, handle, reserved, low, high, pointer):
        overlap = ctypes.cast(pointer, ctypes.POINTER(native.Overlapped)).contents
        self.unlocks.append((handle.value, reserved, low, high, overlap.Position.Offsets.Offset))
        return int(self.unlock_ok)


def code(action):
    with pytest.raises(ContextError) as caught:
        action()
    return caught.value.code


@pytest.fixture
def dlls():
    return FakeDLLs()


def test_explicit_windows_llp64_abi_on_real_test_host():
    native._check_layout()
    assert ctypes.sizeof(native.U16) == 2
    assert ctypes.sizeof(native.U32) == ctypes.sizeof(native.I32) == 4
    assert ctypes.sizeof(native.U64) == ctypes.sizeof(native.I64) == 8
    assert native.UnicodeString.Buffer.offset == 8
    assert native.ObjectAttributes.RootDirectory.offset == 8
    assert native.ObjectAttributes.ObjectName.offset == 16
    assert native.ObjectAttributes.Attributes.offset == 24
    assert native.ObjectAttributes.SecurityDescriptor.offset == 32
    assert native.IoStatusBlock.Information.offset == 8
    assert native.FileIdInfo.FileId.offset == 8
    assert native.FileStandardInfo.NumberOfLinks.offset == 16
    assert native.FileBasicInfo.FileAttributes.offset == 32
    assert native.Overlapped.Position.offset == 16
    assert native.Overlapped.hEvent.offset == 24


def test_import_constructor_do_not_load_windows_dll(monkeypatch):
    calls = []
    monkeypatch.setattr(native.ctypes, "WinDLL", lambda *args, **kwargs: calls.append(args), raising=False)
    monkeypatch.setattr(native, "os", SimpleNamespace(name="posix"))
    api = native.WindowsNative()
    assert calls == []
    assert code(lambda: api.open_root_directory("C:\\中文 空格")) == "unsupported_platform"
    assert calls == []


def test_arm64_is_not_silently_treated_as_windows_x64(monkeypatch):
    monkeypatch.setattr(native, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(native, "platform", SimpleNamespace(machine=lambda: "ARM64"))
    assert code(lambda: native.WindowsNative().open_root_directory("C:\\库")) == "unsupported_platform"


def test_actual_load_contract_uses_only_system_dll_search(monkeypatch, dlls):
    loaded = []

    def loader(name, **options):
        loaded.append((name, options))
        return dlls.kernel32 if name == "kernel32.dll" else dlls.ntdll

    monkeypatch.setattr(native, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(native, "platform", SimpleNamespace(machine=lambda: "AMD64"))
    monkeypatch.setattr(native.ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(native.ctypes, "get_last_error", lambda: 0, raising=False)
    with native.WindowsNative().open_root_directory("C:\\库"):
        pass
    assert loaded == [
        ("kernel32.dll", {"use_last_error": True, "winmode": 0x800}),
        ("ntdll.dll", {"use_last_error": True, "winmode": 0x800}),
    ]


@pytest.mark.parametrize(
    "component",
    [
        "",
        ".",
        "..",
        "../file",
        "foo/bar",
        "foo\\bar",
        "x:stream",
        "x\x00",
        "x\x01",
        "x\x1f",
        "foo.",
        "foo ",
        "a?",
        "a*",
        "a<",
        "a>",
        'a"',
        "a|",
        "CON",
        "nul.txt",
        "COM1.log",
        "LPT9",
        "COM¹",
        "LPT².txt",
        "CONOUT$",
        "CON .txt",
        "a" * 256,
        "😀" * 128,
        "\ud800",
        None,
        123,
    ],
)
def test_component_rejected_before_native_call(dlls, component):
    with dlls.native.open_root_directory("C:\\库") as root:
        before = len(dlls.opened)
        assert code(lambda: dlls.native.open_relative(root, component, role="read_file")) == "forbidden_path"
        assert len(dlls.opened) == before


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "C:relative",
        "\\folder",
        "\\\\server\\share",
        "\\\\?\\C:\\库",
        "\\\\.\\C:",
        "C:/库",
        "C:\\..\\库",
        "C:\\库\\",
        "C:\\库\\\\文",
        "C:\\库:stream",
        "C:\\NUL.txt",
        "C:\\foo.",
        None,
    ],
)
def test_root_rejects_nonlocal_ambiguous_or_alias_paths(dlls, path):
    assert code(lambda: dlls.native.open_root_directory(path)) == "forbidden_path"
    assert not dlls.opened


def test_unicode_backing_is_utf16_and_root_relative_no_reparse_contract(dlls):
    with dlls.native.open_root_directory("C:\\中文 空格😀") as root:
        request = dlls.opened[-1]
        assert request["name"] == "\\Device\\HarddiskVolume4\\中文 空格😀"
        assert request["root"] is None
        assert request["length"] == len(request["name"].encode("utf-16-le"))
        assert request["capacity"] == request["length"] + 2
        assert request["attributes"] == 0x1040  # DONT_REPARSE, CASE_INSENSITIVE, no INHERIT
        with dlls.native.open_relative(root, "正文😀.md", role="read_file") as leaf:
            child = dlls.opened[-1]
            assert child["name"] == "正文😀.md"
            assert child["root"] == root._value
            assert child["attributes"] == 0x1040
            assert child["disposition"] == 1  # FILE_OPEN, never CREATE/OVERWRITE
            assert child["options"] == 0x00200060
            assert child["access"] == 0x00120081
            assert child["shares"] == 1  # deny competing write/delete access
            assert child["security"] is None
            assert dlls.flags[-1] == (leaf._value, 1, 0)
            info = dlls.native.information(leaf)
            assert info.identity == leaf.identity
            assert info.identity.volume_serial == 0xAABBCCDDEEFF0011
            assert len(info.identity.file_id) == 16
            assert (info.size, info.links, info.change_time, info.last_write_time) == (12, 1, 3, 2)
            assert {kind for _, kind, _ in dlls.information_classes} == {0, 1, 9, 18}


@pytest.mark.parametrize(
    "role,access,shares,options",
    [
        ("directory", 0x001200A0, 3, 0x00200021),
        ("read_file", 0x00120081, 1, 0x00200060),
        ("lease_file", 0x40120081, 3, 0x00200060),
        ("lease_observer", 0x80120081, 3, 0x00200060),
    ],
)
def test_roles_cannot_supply_arbitrary_masks_or_creation_flags(dlls, role, access, shares, options):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "entry", role=role):
            request = dlls.opened[-1]
            assert (request["access"], request["shares"], request["options"]) == (access, shares, options)
        assert code(lambda: dlls.native.open_relative(root, "entry", role="execute")) == "forbidden_path"


@pytest.mark.parametrize(
    "status,expected",
    [
        (0xC0000034, "not_found"),
        (0xC000003A, "not_found"),
        (0xC0000022, "forbidden_path"),
        (0xC000050B, "forbidden_path"),
        (0xC0000279, "forbidden_path"),
        (0x8000002D, "forbidden_path"),
        (0xC0000043, "version_changed"),
        (0xC0000008, "storage_unavailable"),
        (0x103, "storage_unavailable"),
    ],
)
def test_nt_status_converts_without_paths_and_closes_partial_handles(dlls, status, expected):
    dlls.status = ctypes.c_int32(status).value
    with pytest.raises(ContextError) as caught:
        dlls.native.open_root_directory("C:\\不该泄露的名字")
    assert caught.value.code == expected
    assert "不该泄露" not in str(caught.value)
    assert dlls.closed == [100]


@pytest.mark.parametrize(
    "bad",
    [
        {"attributes": 0x400},
        {"tag": 0xA0000003},
        {"directory": False},
        {"delete": 1},
        {"size": -1},
        {"allocation": -1},
        {"id": bytes(16)},
    ],
)
def test_open_info_rejection_closes_handle(dlls, bad):
    dlls.bad_open_info = bad
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "forbidden_path"
    assert dlls.closed == [100]


def test_leaf_hardlink_and_type_rejected(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        for bad in ({"links": 2}, {"directory": True}, {"tag": 0xA000000C}):
            dlls.bad_open_info = bad
            assert code(lambda: dlls.native.open_relative(root, "x", role="read_file")) == "forbidden_path"
        assert dlls.closed == [101, 102, 103]


@pytest.mark.parametrize("failure", ["inherit", "type", "info"])
def test_native_security_query_failures_do_not_return_unchecked_handle(dlls, failure):
    if failure == "inherit":
        dlls.inherit_ok = False
    elif failure == "type":
        dlls.file_type = 3
    else:
        dlls.info_failure = 18
    expected = "forbidden_path" if failure == "type" else "storage_unavailable"
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == expected
    assert dlls.closed == [100]


def test_context_close_is_exactly_once_and_closed_or_foreign_handles_rejected(dlls):
    root = dlls.native.open_root_directory("C:\\库")
    assert code(lambda: FakeDLLs().native.information(root)) == "storage_unavailable"
    with root:
        pass
    root.close()
    assert dlls.closed == [100]
    assert code(lambda: dlls.native.information(root)) == "storage_unavailable"
    assert code(lambda: root.identity) == "storage_unavailable"


def test_acl_never_reports_fake_private_security_success(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        assert code(lambda: dlls.native.require_private_security(root)) == "unsupported_platform"


@pytest.mark.parametrize("symbol", ["GetFileInformationByHandleEx", "ReadFile", "SetFilePointerEx"])
def test_missing_native_symbol_fails_closed_before_any_open(dlls, symbol):
    delattr(dlls.kernel32, symbol)
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "unsupported_platform"
    assert not dlls.opened


@pytest.mark.parametrize("drive_type", [0, 1, 4, 5, 6])
def test_unknown_unavailable_and_mapped_network_drives_do_not_open(dlls, drive_type):
    dlls.drive_type = drive_type
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "storage_unavailable"
    assert dlls.drives == ["C:\\"]
    assert not dlls.opened


@pytest.mark.parametrize("invalid", [None, "arbitrary handle", 100])
def test_foreign_values_never_reach_native_lock_calls(dlls, invalid):
    assert code(lambda: dlls.native.try_lock(invalid, exclusive=True)) == "storage_unavailable"
    assert not dlls.locks


def test_handle_role_is_readonly(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_observer") as observer:
            with pytest.raises(AttributeError):
                observer.role = "lease_file"


def test_lock_release_failure_remains_owned_until_retry_or_handle_close(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_file") as leaf:
            lock = dlls.native.try_lock(leaf, exclusive=True)
            dlls.unlock_ok = False
            assert code(lock.close) == "storage_unavailable"
            assert not lock.closed
            assert leaf._locks == 1
            assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == "forbidden_path"
            dlls.unlock_ok = True
            lock.close()
            assert lock.closed
            assert leaf._locks == 0


def test_file_close_failure_is_reported_and_never_reuses_handle(dlls):
    root = dlls.native.open_root_directory("C:\\库")
    dlls.close_ok = False
    assert code(root.close) == "storage_unavailable"
    assert root.closed
    root.close()
    assert dlls.closed == [100]


def test_shared_observer_and_exclusive_owner_fixed_lock_range(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_file") as owner:
            with dlls.native.try_lock(owner, exclusive=True) as lock:
                assert lock.identity == owner.identity
                assert dlls.locks[-1][1:] == (3, 0, 1, 0, 1 << 20, 0, None)
                assert code(lambda: dlls.native.try_lock(owner, exclusive=True)) == "forbidden_path"
            assert len(dlls.unlocks) == 1
            lock.close()
            assert len(dlls.unlocks) == 1
        with dlls.native.open_relative(root, "lease", role="lease_observer") as observer:
            assert code(lambda: dlls.native.try_lock(observer, exclusive=True)) == "forbidden_path"
            with dlls.native.try_lock(observer, exclusive=False):
                assert dlls.locks[-1][1] == 1


@pytest.mark.parametrize("error,expected", [(33, "lock_busy"), (32, "lock_busy"), (5, "storage_unavailable")])
def test_nonblocking_lock_failure_does_not_claim_ownership(dlls, error, expected):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_file") as leaf:
            dlls.lock_ok, dlls.error = False, error
            assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == expected
            assert leaf._locks == 0
            assert dlls.unlocks == []


def test_lock_checks_identity_and_metadata_bound_before_native_call(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_file") as leaf:
            dlls.files[leaf._value]["size"] = 65537
            assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == "forbidden_path"
            assert not dlls.locks
            dlls.files[leaf._value]["size"] = 12
            dlls.files[leaf._value]["id"] = b"changed identity"
            assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == "lock_changed"
            assert not dlls.locks


def test_failed_post_lock_check_releases_kernel_lock(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        with dlls.native.open_relative(root, "lease", role="lease_file") as leaf:
            original = dlls.kernel32.LockFileEx.call

            def change_after_lock(*args):
                result = original(*args)
                dlls.files[leaf._value]["id"] = b"changed identity"
                return result

            dlls.kernel32.LockFileEx.call = change_after_lock
            assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == "lock_changed"
            assert leaf._locks == 0
            assert len(dlls.unlocks) == 1


def test_closed_file_does_not_unlock_reused_native_handle(dlls):
    with dlls.native.open_root_directory("C:\\库") as root:
        leaf = dlls.native.open_relative(root, "lease", role="lease_file")
        lock = dlls.native.try_lock(leaf, exclusive=True)
        leaf.close()
        lock.close()
        assert not dlls.unlocks


def test_draft_does_not_activate_public_platform_backend():
    report = detect_platform_safety()
    assert report.safe_files_backend != "windows_native"
    assert report.ownership_backend != "windows_native"
