"""Original HANDLE/DACL contract fixtures, NOT Windows kernel or device acceptance."""

from __future__ import annotations

import ctypes
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
from test_context_windows_native import code
from test_context_windows_publication import PublicationDLLs
from test_context_windows_security import EVERYONE, USER, ace, descriptor

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.windows_initialization import initialize_windows_root
from collection_context.infrastructure.windows_security import private_descriptor


def seed(dlls, *components):
    with ExitStack() as stack:
        parent = stack.enter_context(dlls.native.open_root_directory("C:\\"))
        for component in components:
            parent = stack.enter_context(dlls.native.create_directory(parent, component))
        return dlls.files[parent._value]


def created_requests(dlls):
    return [request for request in dlls.opened if request["disposition"] == 2]


def assert_closed(dlls):
    assert set(dlls.files) <= set(dlls.closed)


def test_each_level_is_anchored_private_and_owned_until_close(monkeypatch):
    dlls = PublicationDLLs()

    def no_path_mutation(*args, **kwargs):
        pytest.fail("No pathname mkdir/chmod/deletion fallback")

    with monkeypatch.context() as patch:
        for name in ("mkdir", "makedirs", "chmod", "rmdir", "unlink"):
            patch.setattr(os, name, no_path_mutation)
        patch.setattr(Path, "mkdir", no_path_mutation)
        result = initialize_windows_root("C:\\新父目录\\原创 目录\\资料库", _native=dlls.native)
        result.check()
        assert result.root == "C:\\新父目录\\原创 目录\\资料库"
        assert result.native is dlls.native and result.identity == result.handle.identity
        assert result.created_count == 3 and not result.closed
        creations = created_requests(dlls)
        assert [request["name"] for request in creations] == ["新父目录", "原创 目录", "资料库"]
        assert all(request["shares"] == 3 and request["security"] for request in creations)
        assert all(request["attributes"] == 0x1040 for request in creations)
        assert all(request["options"] == 0x00200021 for request in creations)
        assert all(not request["access"] & 0x000C0000 for request in dlls.opened)
        live = {value for value in dlls.files if value not in dlls.closed}
        assert len(live) == 4 and result.handle._value in live
        assert all(request["root"] in live for request in creations)
        for value in live - {creations[0]["root"]}:
            policy = private_descriptor(dlls.descriptors[value], USER)
            assert policy.control == 0x9004
            assert len(policy.aces) == 3 and all(entry[1] == 3 for entry in policy.aces)
        assert not dlls.writes and not dlls.renames and not dlls.truncations
        result.close()
        assert result.closed
        close_count = len(dlls.closed)
        result.close()
        assert len(dlls.closed) == close_count
    assert_closed(dlls)


def test_existing_parent_can_be_shared_and_is_never_permission_repaired():
    dlls = PublicationDLLs()
    parent = seed(dlls, "existing", "shared")
    previous_nodes = {key: dict(value) for key, value in dlls.names.items()}
    previous_descriptors = dict(dlls.descriptors)
    dlls.sd = descriptor(ace(EVERYONE))  # Existing ancestors intentionally non-private.
    dlls.opened.clear()
    dlls.security_calls.clear()
    with initialize_windows_root("C:\\existing\\shared\\new", _native=dlls.native) as result:
        assert result.created_count == 1
        assert [request["name"] for request in created_requests(dlls)] == ["new"]
        assert all(key in dlls.names and dlls.names[key] == value for key, value in previous_nodes.items())
        assert all(dlls.descriptors[key] == value for key, value in previous_descriptors.items())
        assert {value for value, _, _ in dlls.security_calls} == {result.handle._value}
        assert parent["id"] != result.identity.file_id
    assert_closed(dlls)


@pytest.mark.parametrize("kind", ["directory", "file", "reparse", "unsafe_acl"])
def test_existing_final_target_always_refused_without_adoption(kind):
    dlls = PublicationDLLs()
    target = seed(dlls, "existing")
    if kind == "file":
        target["directory"] = False
    elif kind == "reparse":
        target.update(attributes=0x410, tag=1)
    elif kind == "unsafe_acl":
        dlls.sd = descriptor(ace(EVERYONE))
    before = {key: dict(value) for key, value in dlls.names.items()}
    before_acl = dict(dlls.descriptors)
    dlls.opened.clear()
    assert code(lambda: initialize_windows_root("C:\\existing", _native=dlls.native)) == "workspace_not_empty"
    assert dlls.names == before and dlls.descriptors == before_acl
    assert not created_requests(dlls)
    assert not any(request["name"] == "existing" and request["disposition"] == 1 for request in dlls.opened)
    assert_closed(dlls)


@pytest.mark.parametrize(
    "path",
    [
        "",
        None,
        "relative",
        "C:relative",
        "C:\\",
        "C:/new",
        "\\server\\new",
        "\\\\server\\share\\new",
        "\\\\?\\C:\\new",
        "C:\\existing\\..\\new",
        "C:\\existing\\",
        "C:\\\\new",
        "C:\\CON\\new",
        "C:\\new.",
        "C:\\new ",
        "C:\\new:stream",
        "C:\\bad\x00name",
        "C:\\" + "a" * 1024,
        "C:\\" + "\\".join(["a"] * 65),
    ],
)
def test_invalid_selection_has_no_native_activity(path):
    dlls = PublicationDLLs()
    assert code(lambda: initialize_windows_root(path, _native=dlls.native)) == "forbidden_path"
    assert not dlls.opened and not dlls.device_queries and not dlls.construction


@pytest.mark.parametrize("change", ["reparse", "tag", "links", "pending", "file"])
def test_unsafe_existing_ancestor_blocks_creation(change):
    dlls = PublicationDLLs()
    ancestor = seed(dlls, "parent")
    ancestor.update(
        {
            "reparse": {"attributes": 0x410},
            "tag": {"tag": 1},
            "links": {"links": 2},
            "pending": {"delete": 1},
            "file": {"directory": False},
        }[change]
    )
    dlls.opened.clear()
    assert code(lambda: initialize_windows_root("C:\\parent\\new", _native=dlls.native)) == "forbidden_path"
    assert not created_requests(dlls)
    assert_closed(dlls)


def test_intermediate_exclusive_creation_collision_is_not_reopened_or_adopted(monkeypatch):
    dlls = PublicationDLLs()
    original = dlls.native.create_directory

    def competing(parent, component):
        original(parent, component).close()
        raise ContextError("write_conflict", "Original synthetic competing creator")

    monkeypatch.setattr(dlls.native, "create_directory", competing)
    assert (
        code(lambda: initialize_windows_root("C:\\new-parent\\root", _native=dlls.native)) == "write_conflict"
    )
    assert [entry["name"] for entry in created_requests(dlls)] == ["new-parent"]
    assert not any(
        entry["name"] in {"new-parent", "root"} and entry["disposition"] == 1 for entry in dlls.opened
    )
    assert any(key[1] == "new-parent" for key in dlls.names)
    assert_closed(dlls)


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_every_retained_path_binding_is_rechecked(level):
    dlls = PublicationDLLs()
    result = initialize_windows_root("C:\\a\\b\\root", _native=dlls.native)
    try:
        anchor = result._anchors[level]
        key = next(key for key, node in dlls.names.items() if node["id"] == anchor.identity.file_id)
        dlls.names[key] = dict(dlls.names[key], id=b"z" * 16)
        assert code(result.check) == "storage_unavailable"
    finally:
        result.close()
    assert_closed(dlls)


def test_drive_remapping_after_creation_cannot_reuse_old_handle():
    dlls = PublicationDLLs()
    with initialize_windows_root("C:\\root", _native=dlls.native) as result:
        dlls.device_target = "\\Device\\HarddiskVolume98"
        assert code(result.check) == "storage_unavailable"
    assert_closed(dlls)


def test_context_entry_failure_closes_previously_created_chain():
    dlls = PublicationDLLs()
    result = initialize_windows_root("C:\\first\\root", _native=dlls.native)
    dlls.device_target = "\\Device\\HarddiskVolume97"
    assert code(result.__enter__) == "storage_unavailable"
    assert result.closed
    assert_closed(dlls)


def test_parent_replacement_detected_before_final_creation():
    dlls = PublicationDLLs()
    ancestor = seed(dlls, "parent")
    key = next(key for key, value in dlls.names.items() if value is ancestor)
    count = 0

    def replace_after_reopen(value, name, disposition):
        nonlocal count
        if disposition == 1 and name == "parent":
            count += 1
            if count == 2:
                dlls.names[key] = dict(ancestor, id=b"y" * 16)

    dlls.open_hook = replace_after_reopen
    dlls.opened.clear()
    assert (
        code(lambda: initialize_windows_root("C:\\parent\\new", _native=dlls.native)) == "storage_unavailable"
    )
    assert not created_requests(dlls)
    assert_closed(dlls)


@pytest.mark.parametrize("change", ["ancestor_alias", "different_volume"])
def test_new_child_cannot_alias_an_ancestor_or_change_volume(change):
    dlls = PublicationDLLs()
    if change == "ancestor_alias":

        def alias(value, name, disposition):
            if disposition == 2:
                parent = created_requests(dlls)[-1]["root"]
                dlls.files[value]["id"] = dlls.files[parent]["id"]

        dlls.open_hook = alias
    else:
        original = dlls.kernel32.GetFileInformationByHandleEx.call

        def foreign(handle, kind, output, length):
            result = original(handle, kind, output, length)
            if kind == 18 and handle.value in dlls.descriptors:
                record = ctypes.cast(output, ctypes.POINTER(native.FileIdInfo)).contents
                record.VolumeSerialNumber += 1
            return result

        dlls.kernel32.GetFileInformationByHandleEx.call = foreign
    assert code(lambda: initialize_windows_root("C:\\root", _native=dlls.native)) == "forbidden_path"
    assert len(created_requests(dlls)) == 1
    assert_closed(dlls)


@pytest.mark.parametrize("phase", ["descriptor", "created_acl", "retained_acl"])
def test_permission_failure_stops_without_repair_or_deletion(phase):
    dlls = PublicationDLLs()
    if phase == "descriptor":
        dlls.creation_fail = "set_control"
    elif phase == "created_acl":

        def unsafe(value, name, disposition):
            if disposition == 2:
                dlls.descriptors[value] = descriptor(ace(EVERYONE))

        dlls.open_hook = unsafe
    else:
        result = initialize_windows_root("C:\\first\\root", _native=dlls.native)
        before = dict(dlls.names)
        dlls.descriptors[result._anchors[1].handle._value] = descriptor(ace(EVERYONE))
        try:
            assert code(result.check) == "unsafe_secret_permissions"
        finally:
            result.close()
        assert dlls.names == before
        assert_closed(dlls)
        return
    assert code(lambda: initialize_windows_root("C:\\first\\root", _native=dlls.native)) in {
        "storage_unavailable",
        "unsafe_secret_permissions",
    }
    assert len(created_requests(dlls)) == (0 if phase == "descriptor" else 1)
    assert not dlls.writes and not dlls.renames
    assert_closed(dlls)


def test_failure_after_partial_creation_retains_new_directories_and_original_error(monkeypatch):
    dlls = PublicationDLLs()
    original = dlls.native.create_directory

    def fail_final(parent, component):
        if component == "root":
            raise ContextError("storage_unavailable", "Original synthetic disk failure")
        return original(parent, component)

    monkeypatch.setattr(dlls.native, "create_directory", fail_final)
    assert (
        code(lambda: initialize_windows_root("C:\\first\\root", _native=dlls.native)) == "storage_unavailable"
    )
    assert [entry["name"] for entry in created_requests(dlls)] == ["first"]
    assert any(key[1] == "first" for key in dlls.names)
    assert not any(key[1] == "root" for key in dlls.names)
    assert_closed(dlls)


def test_close_failure_attempts_every_owned_handle_once_and_leaves_adapter_usable():
    dlls = PublicationDLLs()
    result = initialize_windows_root("C:\\first\\root", _native=dlls.native)
    owned = [anchor.handle._value for anchor in result._anchors]
    dlls.close_ok = False
    assert code(result.close) == "storage_unavailable"
    assert result.closed and all(anchor.handle.closed for anchor in result._anchors)
    assert dlls.closed[-len(owned) :] == list(reversed(owned))
    before = len(dlls.closed)
    result.close()
    assert len(dlls.closed) == before
    assert code(result.check) == "storage_unavailable"
    dlls.close_ok = True
    with dlls.native.open_root_directory("D:\\"):
        pass
    assert_closed(dlls)


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows rejection contract, not native acceptance")
def test_uninjected_execution_on_mac_or_linux_remains_closed():
    assert code(lambda: initialize_windows_root("C:\\new")) == "unsupported_platform"
