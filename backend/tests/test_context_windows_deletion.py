"""Owned isolated namespace/call tests, not actual Windows sharing semantics."""

from __future__ import annotations

import ctypes

import pytest
from test_context_windows_native import FakeFunction, code
from test_context_windows_publication import PublicationDLLs

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.infrastructure.windows_deletion import FileDispositionInformation
from collection_context.infrastructure.windows_files import WindowsFiles


class DeletionDLLs(PublicationDLLs):
    def __init__(self):
        super().__init__()
        self.dispositions = []
        self.delete_status = 0
        self.delete_completion = 0
        self.delete_information = 0
        self.delete_hook = None
        self.leave_entry = False
        self.delete_close_ok = True
        self.post_close_hook = None
        self.pending = set()
        self.ntdll.NtSetInformationFile = FakeFunction(self.set_information)

    def set_information(self, handle, io, buffer, length, kind):
        if kind == 10:
            return self.rename(handle, io, buffer, length, kind)
        assert kind == 13 and length == 1
        assert ctypes.string_at(buffer, length) == b"\x01"
        self.dispositions.append(handle.value)
        if self.delete_status == 0:
            self.pending.add(handle.value)
        if self.delete_hook:
            self.delete_hook(handle.value)
        block = ctypes.cast(io, ctypes.POINTER(native.IoStatusBlock)).contents
        block.Result.Status, block.Information = self.delete_completion, self.delete_information
        return self.delete_status

    def info(self, handle, kind, output, length):
        assert handle.value not in self.pending, "only close is legal after disposition"
        return super().info(handle, kind, output, length)

    def close(self, handle):
        result = super().close(handle)
        if handle.value in self.pending:
            self.pending.remove(handle.value)
            if not self.leave_entry and self.delete_close_ok:
                entry = self.files[handle.value]
                for key, value in list(self.names.items()):
                    if value is entry:
                        del self.names[key]
            if self.post_close_hook:
                self.post_close_hook(handle.value)
            return int(self.delete_close_ok)
        return result


@pytest.fixture
def deletion():
    dlls = DeletionDLLs()
    with WindowsFiles("C:\\原创库", _native=dlls.native) as files:
        files.write("附件/正文.md", "原创正文".encode())
        dlls.reads.clear()
        dlls.writes.clear()
        dlls.renames.clear()
        dlls.flushes.clear()
        yield dlls, files


@pytest.mark.parametrize("information", [0, 1])
def test_existing_file_deleted_once_by_owned_handle_without_body_or_creation(deletion, information):
    dlls, files = deletion
    dlls.delete_information = information
    creations = sum(entry["disposition"] == 2 for entry in dlls.opened)
    files.unlink("附件/正文.md")
    assert files.entry_exists("附件") and not files.entry_exists("附件/正文.md")
    assert len(dlls.dispositions) == 1
    assert dlls.closed.count(dlls.dispositions[0]) == 1
    deletion_open = [entry for entry in dlls.opened if entry["access"] == 0x00130080]
    assert len(deletion_open) == 1
    assert deletion_open[0]["shares"] == 0 and deletion_open[0]["options"] == 0x00200060
    assert deletion_open[0]["disposition"] == 1 and not deletion_open[0]["security"]
    assert not dlls.reads and not dlls.writes and not dlls.renames and not dlls.flushes
    assert creations == sum(entry["disposition"] == 2 for entry in dlls.opened)
    assert ctypes.sizeof(FileDispositionInformation) == 1
    assert dlls.ntdll.NtSetInformationFile.argtypes == [
        native.HANDLE,
        ctypes.POINTER(native.IoStatusBlock),
        native.HANDLE,
        native.U32,
        native.U32,
    ]


@pytest.mark.parametrize("path", ["missing", "missing/file", "附件/missing"])
def test_missing_target_never_creates_or_submits(deletion, path):
    dlls, files = deletion
    before = sum(entry["disposition"] == 2 for entry in dlls.opened)
    assert code(lambda: files.unlink(path)) == "not_found"
    assert not dlls.dispositions
    assert before == sum(entry["disposition"] == 2 for entry in dlls.opened)


@pytest.mark.parametrize(
    "change",
    [
        {"directory": True},
        {"links": 2},
        {"attributes": 0x400},
        {"tag": 0xA000000C},
        {"delete": 1},
    ],
)
def test_unsafe_target_never_marked_for_deletion(deletion, change):
    dlls, files = deletion
    with files._parent("附件/正文.md") as (parent, name):
        entry = dlls.names[dlls.name_key(parent._value, name)]
    entry.update(change)
    assert code(lambda: files.unlink("附件/正文.md")) == "forbidden_path"
    assert not dlls.dispositions


def test_missing_capability_fails_before_delete_handle(deletion):
    dlls, files = deletion
    del dlls.ntdll.NtSetInformationFile
    assert code(lambda: files.unlink("附件/正文.md")) == "unsupported_platform"
    assert not any(entry["access"] & 0x10000 and entry["disposition"] == 1 for entry in dlls.opened)


@pytest.mark.parametrize("where", ["target", "parent"])
def test_bad_private_acl_never_requests_disposition(deletion, where):
    dlls, files = deletion
    original = dlls.native.require_private_security

    def check(handle):
        if (where == "target" and handle.role == "delete_file") or (
            where == "parent" and handle.role == "directory"
        ):
            raise ContextError("unsafe_secret_permissions", "isolated ACL rejection")
        return original(handle)

    dlls.native.require_private_security = check
    assert code(lambda: files.unlink("附件/正文.md")) == "unsafe_secret_permissions"
    assert not dlls.dispositions


def test_version_change_before_submission_does_not_delete(deletion):
    dlls, files = deletion
    original = dlls.native.require_private_security

    def check(handle):
        original(handle)
        if handle.role == "delete_file":
            dlls.files[handle._value]["last_write_time"] = 99

    dlls.native.require_private_security = check
    assert code(lambda: files.unlink("附件/正文.md")) == "version_changed"
    assert not dlls.dispositions


def test_root_change_before_submission_does_not_delete(deletion):
    dlls, files = deletion
    original = dlls.native.require_private_security

    def check(handle):
        original(handle)
        if handle.role == "delete_file":
            dlls.device_target = "\\Device\\HarddiskVolume99"

    dlls.native.require_private_security = check
    assert code(lambda: files.unlink("附件/正文.md")) == "storage_unavailable"
    assert not dlls.dispositions


@pytest.mark.parametrize("status, completion", [(0x103, 0), (-1073741790, 0), (0, -1)])
def test_unknown_submission_is_never_retried(deletion, status, completion):
    dlls, files = deletion
    dlls.delete_status, dlls.delete_completion = status, completion
    with pytest.raises(ContextError) as caught:
        files.unlink("附件/正文.md")
    assert caught.value.code == "storage_unavailable" and not caught.value.retryable
    assert len(dlls.dispositions) == 1 and dlls.closed.count(dlls.dispositions[0]) == 1


@pytest.mark.parametrize("failure", ["left", "close", "root", "replacement"])
def test_post_submission_failure_not_reported_success_or_second_delete(deletion, failure):
    dlls, files = deletion
    if failure == "left":
        dlls.leave_entry = True
    elif failure == "close":
        dlls.delete_close_ok = False
    elif failure == "root":
        dlls.post_close_hook = lambda _: setattr(dlls, "device_target", "\\Device\\HarddiskVolume77")
    else:

        def replacement(value):
            entry = dict(dlls.files[value], id=b"n" * 16, body=b"new owner", size=9)
            key = next(entry for entry in dlls.opened if entry["access"] == 0x00130080)
            dlls.names[dlls.name_key(key["root"], key["name"])] = entry

        dlls.post_close_hook = replacement
    assert code(lambda: files.unlink("附件/正文.md")) == "storage_unavailable"
    assert len(dlls.dispositions) == 1
    if failure == "replacement":
        assert any(entry["body"] == b"new owner" for entry in dlls.names.values())


def test_keyboard_interrupt_after_unknown_submission_closes_without_retry(deletion):
    dlls, files = deletion

    def interrupt(_):
        raise KeyboardInterrupt

    dlls.delete_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        files.unlink("附件/正文.md")
    assert len(dlls.dispositions) == 1 and dlls.closed.count(dlls.dispositions[0]) == 1


def test_delete_role_cannot_read_lock_or_create(deletion):
    dlls, files = deletion
    with files._parent("附件/正文.md") as (parent, component):
        with dlls.native.open_relative(parent, component, role="delete_file") as target:
            assert code(lambda: dlls.native.read_file(target)) == "forbidden_path"
            assert code(lambda: dlls.native.try_lock(target, exclusive=True)) == "forbidden_path"
            sd = dlls.native.private_security().creation_descriptor()
            assert (
                code(lambda: dlls.native._open("new", parent, "delete_file", _creation=sd))
                == "forbidden_path"
            )
    assert not dlls.dispositions and not dlls.writes


def test_actual_file_secrets_discard_uses_facade_and_missing_is_idempotent(deletion):
    dlls, files = deletion
    secret = object.__new__(FileSecrets)
    secret.files = files
    ref = "k_" + "a" * 32
    files.write(ref, b"original synthetic credential")
    dlls.reads.clear()
    secret.discard_new(ref)
    secret.discard_new(ref)
    assert len(dlls.dispositions) == 1 and not dlls.reads
    assert not files.entry_exists(ref)


def test_actual_file_secrets_does_not_swallow_unknown_deletion(deletion):
    dlls, files = deletion
    secret = object.__new__(FileSecrets)
    secret.files = files
    ref = "k_" + "b" * 32
    files.write(ref, b"synthetic")
    dlls.leave_entry = True
    assert code(lambda: secret.discard_new(ref)) == "storage_unavailable"
    assert len(dlls.dispositions) == 1


def test_sibling_and_parent_not_deleted(deletion):
    dlls, files = deletion
    files.write("附件/保留.md", b"untouched")
    files.unlink("附件/正文.md")
    assert files.read("附件/保留.md") == b"untouched"
    assert files.entry_exists("附件") and len(dlls.dispositions) == 1


def test_closed_root_cannot_submit_delete(deletion):
    dlls, files = deletion
    files.close()
    assert code(lambda: files.unlink("附件/正文.md")) == "storage_unavailable"
    assert not dlls.dispositions


def test_inaccessible_target_not_interpreted_as_missing(deletion):
    dlls, files = deletion
    original = dlls.native.open_relative

    def open_relative(parent, component, *, role):
        if role == "delete_file":
            raise ContextError("forbidden_path", "isolated denied open")
        return original(parent, component, role=role)

    dlls.native.open_relative = open_relative
    assert code(lambda: files.unlink("附件/正文.md")) == "forbidden_path"
    assert not dlls.dispositions


@pytest.mark.parametrize("change", [{"attributes": 0x400}, {"tag": 0xA000000C}, {"directory": False}])
def test_bad_parent_does_not_open_target_with_delete_rights(deletion, change):
    dlls, files = deletion
    entry = dlls.names[dlls.name_key(files.handle._value, "附件")]
    entry.update(change)
    assert code(lambda: files.unlink("附件/正文.md")) == "forbidden_path"
    assert not dlls.dispositions
    assert not any(request["access"] == 0x00130080 for request in dlls.opened)


def test_wrong_parent_role_or_foreign_handle_no_submission(deletion):
    dlls, files = deletion
    with files._parent("附件/正文.md") as (parent, component):
        with dlls.native.open_relative(parent, component, role="metadata") as leaf:
            assert (
                code(lambda: files._deletion.unlink(leaf, "unrelated", check_attachment=files.check_root))
                == "forbidden_path"
            )
        other = DeletionDLLs()
        with other.native.open_root_directory("C:\\另一个原创库") as foreign:
            assert (
                code(lambda: files._deletion.unlink(foreign, component, check_attachment=files.check_root))
                == "storage_unavailable"
            )
    assert not dlls.dispositions


def test_file_secrets_full_existing_facade_contract(deletion):
    dlls, files = deletion
    secret = object.__new__(FileSecrets)
    secret.files = files
    ref = secret.put("original synthetic secret")
    assert secret.get(ref) == "original synthetic secret"
    secret.discard_new(ref)
    assert code(lambda: secret.get(ref)) == "credential_missing"
    assert len(dlls.dispositions) == 1
