"""Draft directory facade through injected DLLs, not Windows OS acceptance."""

from __future__ import annotations

import ctypes
import threading
from types import SimpleNamespace

import pytest
from test_context_windows_native import FakeDLLs, FakeFunction, code
from test_context_windows_publication import PublicationDLLs
from test_context_windows_security import USER, ace, descriptor

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.windows_files import WindowsFiles


@pytest.fixture
def files():
    dlls = PublicationDLLs()
    tree = WindowsFiles("C:\\原创库", _native=dlls.native)
    try:
        yield dlls, tree
    finally:
        tree.close()


def test_directory_exclusive_create_protected_acl_and_fixed_rights(files):
    dlls, tree = files
    with dlls.native.create_directory(tree.handle, "附件") as child:
        request = [entry for entry in dlls.opened if entry["disposition"] == 2][-1]
        assert request["name"] == "附件" and request["root"] == tree.handle._value
        assert request["shares"] == 3 and request["access"] == 0x001200A0
        assert request["options"] == 0x00200021
        sd = dlls.descriptors[child._value]
        from collection_context.infrastructure.windows_security import private_descriptor

        value = private_descriptor(sd, USER)
        assert value.control == 0x9004 and len(value.aces) == 3
        assert all(flags == 3 for _, flags, _, _ in value.aces)
        assert dlls.native.information(child).directory
    assert code(lambda: dlls.native.create_directory(tree.handle, "附件")) == "write_conflict"


@pytest.mark.parametrize("body", [b"", b"original\x00\xff", "正文🙂".encode()])
def test_nested_write_read_and_reopen_preserve_library_and_content(files, body):
    dlls, tree = files
    tree.write("资料/作品/正文.md", body)
    assert tree.read("资料/作品/正文.md", private=True) == body
    tree.check_root()
    with WindowsFiles(tree.root, _native=dlls.native) as reopened:
        assert reopened.identity == tree.identity
        assert reopened.read("资料/作品/正文.md", private=True) == body
    assert len([entry for entry in dlls.opened if entry["disposition"] == 2 and entry["options"] & 1]) == 2
    assert not any(entry["name"].startswith("\\??\\") for entry in dlls.opened)


def test_existing_mkdir_does_not_recreate_repair_or_replace(files):
    dlls, tree = files
    tree.mkdir("附件/视频")
    created = len([entry for entry in dlls.opened if entry["disposition"] == 2])
    tree.mkdir("附件/视频")
    assert len([entry for entry in dlls.opened if entry["disposition"] == 2]) == created == 2


def test_missing_read_does_not_create_directories_or_stage(files):
    dlls, tree = files
    assert code(lambda: tree.read("missing/正文.md")) == "not_found"
    assert not any(entry["disposition"] == 2 for entry in dlls.opened)
    assert not dlls.writes and not dlls.renames


@pytest.mark.parametrize(
    "relative", ["", "/a", "a/", "a//b", "a/../b", "a/CON", "a\\b", "x:stream", "a/xx.", None]
)
@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "entry_exists", "file_size"])
def test_invalid_paths_have_no_native_side_effects(files, relative, operation):
    dlls, tree = files
    before = len(dlls.opened)

    def action():
        if operation == "write":
            return tree.write(relative, b"body")
        return getattr(tree, operation)(relative)

    assert code(action) == "forbidden_path"
    assert len(dlls.opened) == before and not dlls.writes


@pytest.mark.parametrize("limit, private", [(-1, False), (True, False), (0, 1), (None, False)])
def test_read_flags_rejected_without_root_probe(files, limit, private):
    dlls, tree = files
    before = len(dlls.opened)
    assert code(lambda: tree.read("safe", max_bytes=limit, private=private)) == "invalid_argument"
    assert len(dlls.opened) == before


def test_root_path_replacement_blocks_reads_mkdir_and_writes(files):
    dlls, tree = files
    tree.write("original", b"value")
    key = (None, "\\device\\harddiskvolume4\\原创库")
    dlls.names[key] = dict(dlls.names[key], id=b"z" * 16)
    before = len(dlls.writes)
    for action in (
        tree.check_root,
        lambda: tree.read("original"),
        lambda: tree.mkdir("new"),
        lambda: tree.write("new", b"no"),
    ):
        assert code(action) == "storage_unavailable"
    assert len(dlls.writes) == before


def test_drive_reassignment_blocks_without_reopening_old_mapping(files):
    dlls, tree = files
    dlls.device_target = "\\Device\\HarddiskVolume99"
    assert code(tree.check_root) == "storage_unavailable"
    assert dlls.opened[-1]["name"].startswith("\\Device\\HarddiskVolume99\\")
    assert not any(entry["name"].startswith("\\??\\") for entry in dlls.opened)


def test_root_goes_away_during_read_no_body_returned(files):
    dlls, tree = files
    tree.write("original", b"value")
    dlls.read_hook = lambda _: setattr(dlls, "drive_type", 1)
    assert code(lambda: tree.read("original")) == "storage_unavailable"


def test_attachment_recheck_before_rename_never_publishes(files):
    dlls, tree = files
    dlls.flush_hook = lambda _: setattr(dlls, "device_target", "\\Device\\HarddiskVolume98")
    assert code(lambda: tree.write("new", b"value")) == "storage_unavailable"
    assert not dlls.renames


def test_attachment_change_after_native_rename_is_unknown_not_success(files):
    dlls, tree = files
    dlls.rename_hook = lambda *_: setattr(dlls, "device_target", "\\Device\\HarddiskVolume97")
    with pytest.raises(ContextError) as caught:
        tree.write("new", b"value")
    assert caught.value.code == "storage_unavailable" and not caught.value.retryable
    assert len(dlls.renames) == 1


def test_parent_traversal_keeps_all_ancestors_open_until_exit(files):
    dlls, tree = files
    tree.mkdir("a/b/c")
    with tree._parent("a/b/c/value") as (parent, component):
        assert component == "value" and not parent.closed
        # Three unique opened directory handles after the root probes. None of
        # these borrowed-chain handles can be closed while their child is used.
        ancestors = [entry for entry in dlls.opened if entry["name"] in {"a", "b", "c"}][-3:]
        assert len(ancestors) == 3
        parent_value = parent._value
        assert parent_value not in dlls.closed
        assert ancestors[2]["root"] not in dlls.closed
        assert ancestors[1]["root"] not in dlls.closed
    assert parent_value in dlls.closed and not tree.handle.closed


def test_close_is_idempotent_and_future_operations_cannot_reuse_root(files):
    dlls, tree = files
    root_value = tree.handle._value
    tree.close()
    tree.close()
    assert dlls.closed.count(root_value) == 1
    assert code(lambda: tree.read("old")) == "storage_unavailable"
    assert code(lambda: tree.mkdir("new")) == "storage_unavailable"


def test_directory_identity_change_or_wrong_type_after_create_closes_own_handle(files):
    dlls, tree = files

    def changed(value, _, disposition):
        if disposition == 2:
            dlls.files[value]["directory"] = False

    dlls.open_hook = changed
    assert code(lambda: tree.mkdir("new")) == "forbidden_path"
    assert not tree.handle.closed


def test_existing_unsafe_directory_not_repaired_or_used_for_writes(files):
    dlls, tree = files
    tree.mkdir("existing")
    original_descriptor = descriptor(ace(bytes.fromhex("010100000000000100000000")))

    def changed(value, name, disposition):
        if disposition == 1 and name == "existing":
            dlls.descriptors[value] = original_descriptor

    dlls.open_hook = changed
    created = len([entry for entry in dlls.opened if entry["disposition"] == 2])
    assert code(lambda: tree.write("existing/value", b"no")) == "unsafe_secret_permissions"
    assert len([entry for entry in dlls.opened if entry["disposition"] == 2]) == created


def test_confirmed_competing_directory_creation_is_opened_once_and_checked(files):
    dlls, tree = files
    original = dlls.native.create_directory

    def competing(parent, component):
        original(parent, component).close()
        raise ContextError("write_conflict", "fixture collision")

    dlls.native.create_directory = competing
    tree.mkdir("new")
    assert len([entry for entry in dlls.opened if entry["disposition"] == 2]) == 1


@pytest.mark.parametrize(
    "target",
    [
        "\\??\\C:\\elsewhere",
        "\\Device\\Mup\\server",
        "\\Device\\HarddiskVolume1\\extra",
        "",
        "C:",
        "\\Device\\HarddiskVolume" + "1" * 21,
    ],
)
def test_drive_alias_network_or_invalid_mapping_rejected_before_open(target):
    dlls = FakeDLLs()
    dlls.device_target = target
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) in {
        "forbidden_path",
        "storage_unavailable",
    }
    assert not dlls.opened


@pytest.mark.parametrize("count", [0, 1, 1025, 3])
def test_drive_mapping_count_and_terminators_not_guessed(count):
    dlls = FakeDLLs()
    dlls.device_count = count
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "storage_unavailable"
    assert not dlls.opened


def test_drive_mapping_changed_during_open_closes_and_refuses_result():
    dlls = FakeDLLs()
    calls = []

    def changed():
        calls.append(1)
        dlls.device_target = "\\Device\\HarddiskVolume5"

    dlls.device_hook = changed
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "storage_unavailable"
    assert dlls.closed == [100]
    assert dlls.device_queries == [("C:", 1024), ("C:", 1024)]
    assert dlls.kernel32.QueryDosDeviceW.argtypes == [
        ctypes.POINTER(native.U16),
        ctypes.POINTER(native.U16),
        native.U32,
    ]


def test_previous_drive_mappings_are_not_used_as_fallback():
    dlls = FakeDLLs()
    dlls.device_target = "\\Device\\HarddiskVolume4\x00\\Device\\HarddiskVolume9"
    with dlls.native.open_root_directory("C:\\库"):
        assert dlls.opened[-1]["name"] == "\\Device\\HarddiskVolume4\\库"
    dlls.device_target = "\\Device\\Mup\\server\x00\\Device\\HarddiskVolume4"
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "forbidden_path"


def test_missing_query_symbol_fails_without_normal_path_fallback():
    dlls = FakeDLLs()
    del dlls.kernel32.QueryDosDeviceW
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "unsupported_platform"
    assert not dlls.opened


def test_facade_constructor_does_not_reinterpret_posix_or_relative_roots():
    dlls = FakeDLLs()
    for path in ("/tmp/library", "relative", "C:relative"):
        assert code(lambda: WindowsFiles(path, _native=dlls.native)) == "forbidden_path"
    assert not dlls.opened


def test_private_root_probe_never_mutates_or_creates(files):
    dlls, tree = files
    tree.require_private_root()
    assert not dlls.construction and not dlls.writes
    dlls.sd = descriptor(ace(bytes.fromhex("010100000000000100000000")))
    assert code(tree.require_private_root) == "unsafe_secret_permissions"


def test_constructor_does_not_load_other_platform_dlls(monkeypatch):
    calls = []
    monkeypatch.setattr(native, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(native.ctypes, "WinDLL", lambda *args, **kwargs: calls.append(args), raising=False)
    assert code(lambda: WindowsFiles("C:\\库")) == "unsupported_platform"
    assert not calls


def test_root_volume_serial_change_is_not_hidden_by_same_file_id(files):
    dlls, tree = files
    original = dlls.kernel32.GetFileInformationByHandleEx

    def changed(handle, kind, output, length):
        result = original(handle, kind, output, length)
        if kind == 18:
            ctypes.cast(output, ctypes.POINTER(native.FileIdInfo)).contents.VolumeSerialNumber += 1
        return result

    dlls.kernel32.GetFileInformationByHandleEx = FakeFunction(changed)
    assert code(tree.check_root) == "storage_unavailable"
    assert not dlls.writes


def test_directory_creation_postcheck_failure_keeps_other_handles_and_stops(files):
    dlls, tree = files

    def changed(value, name, disposition):
        if disposition == 2 and name == "b":
            dlls.files[value]["tag"] = 1

    dlls.open_hook = changed
    assert code(lambda: tree.mkdir("a/b/c")) == "forbidden_path"
    created = [entry for entry in dlls.opened if entry["disposition"] == 2]
    assert [entry["name"] for entry in created] == ["a", "b"]
    assert not tree.handle.closed and not dlls.writes and not dlls.renames


def test_existing_reparse_directory_cannot_be_traversed_for_read_or_write(files):
    dlls, tree = files
    tree.mkdir("a")
    dlls.names[(tree.handle._value, "a")]["tag"] = 1
    assert code(lambda: tree.read("a/value")) == "forbidden_path"
    assert code(lambda: tree.write("a/value", b"no")) == "forbidden_path"
    assert not dlls.writes


def test_close_serialized_with_inflight_write_and_root_checks(files):
    dlls, tree = files
    root_value = tree.handle._value
    entered, release, closing, done = (threading.Event() for _ in range(4))
    failures = []

    def hold(_):
        entered.set()
        if not release.wait(3):
            raise AssertionError("fixture writer was not released")

    def write():
        try:
            tree.write("value", b"body")
        except BaseException as error:
            failures.append(error)

    def close():
        closing.set()
        tree.close()
        done.set()

    dlls.write_hook = hold
    worker = threading.Thread(target=write)
    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(3)
        closer.start()
        assert closing.wait(3)
        assert not done.is_set() and root_value not in dlls.closed
    finally:
        release.set()
        worker.join(3)
        if closer.ident is not None:
            closer.join(3)
    assert not worker.is_alive() and not closer.is_alive() and not failures
    assert done.is_set() and dlls.closed.count(root_value) == 1
    assert len(dlls.renames) == 1


def test_malformed_utf16_device_mapping_never_reaches_ntcreatefile():
    dlls = FakeDLLs()

    def malformed(device, target, capacity):
        ctypes.memmove(target, b"\x00\xd8\x00\x00\x00\x00", 6)
        return 3

    dlls.kernel32.QueryDosDeviceW = FakeFunction(malformed)
    assert code(lambda: dlls.native.open_root_directory("C:\\库")) == "storage_unavailable"
    assert not dlls.opened


def test_metadata_presence_and_size_do_not_read_body_or_request_body_rights(files):
    dlls, tree = files
    tree.write("资料/正文", b"original bytes")
    reads = len(dlls.reads)
    assert tree.entry_exists("资料") is True
    assert tree.entry_exists("资料/正文") is True
    assert tree.file_size("资料/正文") == len(b"original bytes")
    assert len(dlls.reads) == reads
    probes = [entry for entry in dlls.opened if entry["access"] == 0x00120080]
    assert probes and all(entry["shares"] == 3 and entry["options"] == 0x00200020 for entry in probes)
    with tree._parent("资料/正文") as (parent, component):
        with dlls.native.open_relative(parent, component, role="metadata") as handle:
            assert code(lambda: dlls.native.read_file(handle)) == "forbidden_path"
            assert code(lambda: dlls.native.try_lock(handle, exclusive=False)) == "forbidden_path"


def test_metadata_missing_does_not_create_or_request_write(files):
    dlls, tree = files
    assert tree.entry_exists("missing") is False
    assert tree.entry_exists("missing/child") is False
    assert code(lambda: tree.file_size("missing")) == "not_found"
    assert not dlls.reads and not dlls.writes and not dlls.renames
    assert not any(entry["disposition"] == 2 for entry in dlls.opened)


@pytest.mark.parametrize("change", ["directory", "reparse", "hardlink"])
def test_metadata_size_rejects_nonregular_or_unsafe_entries(files, change):
    dlls, tree = files
    tree.write("entry", b"original")
    key = dlls.name_key(tree.handle._value, "entry")
    if change == "directory":
        dlls.names[key]["directory"] = True
        assert tree.entry_exists("entry") is True
    elif change == "reparse":
        dlls.names[key]["tag"] = 0xA000000C
    else:
        dlls.names[key]["links"] = 2
    assert code(lambda: tree.file_size("entry")) == "forbidden_path"
    if change != "directory":
        assert code(lambda: tree.entry_exists("entry")) == "forbidden_path"


@pytest.mark.parametrize("method", ["entry_exists", "file_size"])
def test_metadata_root_change_is_not_false_or_stale_size(files, method):
    dlls, tree = files
    tree.write("entry", b"original")

    def detach(value, name, disposition):
        if name == "entry" and disposition == 1:
            dlls.device_target = "\\Device\\HarddiskVolume87"

    dlls.open_hook = detach
    assert code(lambda: getattr(tree, method)("entry")) == "storage_unavailable"


def test_metadata_existing_windows_facade_supports_credential_consumer_without_fd(files):
    from collection_context.infrastructure.secrets import SYSTEM_SECRET_MANIFEST, FileSecrets

    _, tree = files
    backend = object.__new__(FileSecrets)
    backend.files = tree
    backend._check()
    tree.mkdir(SYSTEM_SECRET_MANIFEST)
    assert code(backend._check) == "credential_backend_mismatch"


def test_metadata_windows_facade_supports_library_size_consumer_without_fd(files):
    from collection_context.application.library_management import LibraryManagement

    _, tree = files
    tree.write("媒体/原创", b"original fixture")
    service = LibraryManagement(SimpleNamespace(files=tree), authorize=lambda: None)
    assert service._size("媒体/原创") == len(b"original fixture")


@pytest.mark.parametrize("change", ["size", "timestamp", "directory", "hardlink"])
def test_metadata_windows_size_change_never_returns_old_size(files, monkeypatch, change):
    dlls, tree = files
    tree.write("entry", b"original")
    original = dlls.native.information
    calls = 0

    def changing(handle):
        nonlocal calls
        result = original(handle)
        if handle.role == "metadata":
            calls += 1
            if calls == 2:  # Open validation first, then the facade's size snapshot.
                data = dlls.files[handle._value]
                if change == "size":
                    data["size"] += 1
                elif change == "timestamp":
                    data["last_write_time"] = 99
                elif change == "directory":
                    data["directory"] = True
                else:
                    data["links"] = 2
        return result

    monkeypatch.setattr(dlls.native, "information", changing)
    assert code(lambda: tree.file_size("entry")) == (
        "forbidden_path" if change == "hardlink" else "version_changed"
    )
