"""Injected rooted lease composition, not real Windows locks/process death."""

from __future__ import annotations

import ctypes
import json
import threading

import pytest
from test_context_windows_native import code
from test_context_windows_publication import PublicationDLLs

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.ownership import worker_capabilities
from collection_context.infrastructure.windows_files import WindowsFiles
from collection_context.infrastructure.windows_ownership import (
    WindowsExecutorLease,
    WindowsWorkerLease,
    WindowsWriterLease,
)


class LeaseDLLs(PublicationDLLs):
    """Track shared/exclusive ranges by fixture file identity, not OS behavior."""

    def __init__(self):
        super().__init__()
        self.held = {}
        self.lock_hook = None

    def lock(self, handle, flags, reserved, low, high, pointer):
        result = super().lock(handle, flags, reserved, low, high, pointer)
        assert self.files[handle.value]["access"] & 0xC0000000
        assert (low, high, reserved) == (1, 0, 0)
        overlap = ctypes.cast(pointer, ctypes.POINTER(native.Overlapped)).contents
        assert overlap.Position.Offsets.Offset == native.LEASE_LOCK_OFFSET
        if not result:
            return result
        identity = self.files[handle.value]["id"]
        owners = self.held.setdefault(identity, {})
        if owners and (flags & 2 or any(value & 2 for value in owners.values())):
            self.error = 33
            result = 0
        else:
            owners[handle.value] = flags
        if self.lock_hook is not None:
            self.lock_hook(handle.value, flags, result)
        return result

    def unlock(self, handle, reserved, low, high, pointer):
        result = super().unlock(handle, reserved, low, high, pointer)
        if result:
            for owners in self.held.values():
                owners.pop(handle.value, None)
        return result

    def close(self, handle):
        result = super().close(handle)
        if result:
            for owners in self.held.values():
                owners.pop(handle.value, None)
        return result


@pytest.fixture
def dlls():
    return LeaseDLLs()


@pytest.mark.parametrize("kind", [WindowsExecutorLease, WindowsWriterLease, WindowsWorkerLease])
def test_bootstrap_private_file_lock_before_metadata_and_recheck(dlls, kind):
    with kind("C:\\原创库", _native=dlls.native) as lease:
        lease.check()
        body = json.loads(lease.body)
        assert set(body) == {"nonce", "pid"} and body["nonce"] == lease.nonce and body["pid"] > 0
        created = [entry for entry in dlls.opened if entry["disposition"] == 2 and not entry["options"] & 1]
        assert len(created) == 1
        assert created[0]["access"] == 0x40120081 and created[0]["shares"] == 3
        assert not created[0]["access"] & 0x10000
        assert dlls.locks[0][1] == 3 and dlls.truncations == [lease.handle._value]
        assert dlls.files[lease.handle._value]["body"] == lease.body
        assert not dlls.renames
    assert not any(dlls.held.values())
    assert code(lease.check) == lease.not_owned


@pytest.mark.parametrize("kind", [WindowsExecutorLease, WindowsWriterLease, WindowsWorkerLease])
def test_competing_owner_never_writes_or_replaces_live_metadata(dlls, kind):
    with kind("C:\\原创库", _native=dlls.native) as first:
        writes = len(dlls.writes)
        assert code(lambda: kind("C:\\原创库", _native=dlls.native)) == first.busy
        assert len(dlls.writes) == writes
        first.check()
        assert dlls.files[first.handle._value]["body"] == first.body
    with kind("C:\\原创库", _native=dlls.native) as second:
        assert second.nonce != first.nonce
        second.check()
    assert not any(dlls.held.values())


def test_three_different_lease_roles_do_not_contend_on_one_metadata_file(dlls):
    with (
        WindowsWriterLease("C:\\原创库", _native=dlls.native) as writer,
        WindowsExecutorLease("C:\\原创库", _native=dlls.native) as executor,
        WindowsWorkerLease("C:\\原创库", _native=dlls.native) as worker,
    ):
        assert len({item.identity for item in (writer, executor, worker)}) == 3
        for item in (writer, executor, worker):
            item.check()


def test_bootstrap_collision_opens_checked_existing_once(dlls, monkeypatch):
    create = dlls.native.create_lease_file
    calls = []

    def competing(parent, component):
        calls.append(component)
        create(parent, component).close()
        return create(parent, component)

    monkeypatch.setattr(dlls.native, "create_lease_file", competing)
    with WindowsWriterLease("C:\\原创库", _native=dlls.native) as lease:
        lease.check()
    assert calls == ["写入所有权.lock"] and len(dlls.writes) == 1


def test_ancestor_handles_remain_owned_until_lease_close(dlls):
    lease = WindowsWriterLease("C:\\原创库", _native=dlls.native)
    parent, root, leaf = lease.parent._value, lease.files.handle._value, lease.handle._value
    assert parent not in dlls.closed and root not in dlls.closed and leaf not in dlls.closed
    lease.close()
    lease.close()
    assert all(dlls.closed.count(value) == 1 for value in (parent, root, leaf))
    assert dlls.closed.index(leaf) < dlls.closed.index(parent) < dlls.closed.index(root)


@pytest.mark.parametrize("failure", ["body", "file", "directory", "root", "mapping", "missing", "acl"])
def test_check_rejects_replaced_attachment_body_and_permissions(dlls, failure):
    with WindowsWriterLease("C:\\原创库", _native=dlls.native) as lease:
        writes = len(dlls.writes)
        key = dlls.name_key(lease.parent._value, lease.component)
        if failure == "body":
            dlls.files[lease.handle._value].update(body=b"other", size=5)
        elif failure == "file":
            dlls.names[key] = dict(dlls.names[key], id=b"x" * 16)
        elif failure == "directory":
            directory = dlls.name_key(lease.files.handle._value, ".context")
            dlls.names[directory] = dict(dlls.names[directory], id=b"z" * 16)
        elif failure == "root":
            root = (None, "\\device\\harddiskvolume4\\原创库")
            dlls.names[root] = dict(dlls.names[root], id=b"r" * 16)
        elif failure == "mapping":
            dlls.device_target = "\\Device\\HarddiskVolume99"
        elif failure == "missing":
            del dlls.names[key]
        else:
            dlls.descriptors[lease.handle._value] = bytes(20)
        with pytest.raises(ContextError):
            lease.check()
        assert len(dlls.writes) == writes


@pytest.mark.parametrize("failure", ["flush", "partial", "lock", "unlock"])
def test_failed_construction_or_close_attempts_owned_cleanup_no_retry(dlls, failure):
    if failure == "flush":
        dlls.flush_ok = False
    elif failure == "partial":
        dlls.write_limit = 2
        dlls.write_hook = lambda _: setattr(dlls, "write_ok", False)
    elif failure == "lock":
        dlls.lock_ok, dlls.error = False, 5
    if failure == "unlock":
        lease = WindowsWriterLease("C:\\原创库", _native=dlls.native)
        dlls.unlock_ok = False
        assert code(lease.close) == "writer_unavailable"
        assert code(lease.check) == "writer_not_owned"
    else:
        with pytest.raises(ContextError):
            WindowsWriterLease("C:\\原创库", _native=dlls.native)
    assert not any(dlls.held.values())
    assert not dlls.renames
    assert len(dlls.writes) <= (2 if failure == "partial" else 1)


def test_external_lock_close_stops_further_authority(dlls):
    with WindowsWriterLease("C:\\原创库", _native=dlls.native) as lease:
        lease.lock.close()
        assert code(lease.check) == "lock_changed"


def test_check_and_close_from_other_thread_do_not_retain_constructor_thread_lock(dlls):
    lease = WindowsWriterLease("C:\\原创库", _native=dlls.native)
    errors = []

    def run():
        try:
            lease.check()
            lease.close()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(3)
    assert not thread.is_alive() and not errors
    assert code(lease.check) == "writer_not_owned"


@pytest.mark.parametrize("model,source", [(True, False), (False, True), (False, False), (True, True)])
def test_worker_observe_busy_permissions_without_model_or_source_calls(dlls, model, source):
    with WindowsWorkerLease(
        "C:\\原创库", allow_model_calls=model, allow_source_sync=source, _native=dlls.native
    ) as worker:
        writes = len(dlls.writes)
        result = worker.observe(worker.files)
        assert result["online"] is True and result["progress_verified"] is False
        assert result["evidence"] == "os_exclusive_lease"
        assert result["capabilities"] == {"model_calls": model, "source_sync": source}
        assert len(dlls.writes) == writes
        worker.check()


def test_observe_missing_never_creates_and_stale_file_is_offline(dlls):
    with WindowsFiles("C:\\原创库", _native=dlls.native) as files:
        before = len(dlls.writes)
        assert WindowsWorkerLease.observe(files)["online"] is False
        assert not any(entry["disposition"] == 2 for entry in dlls.opened)
        worker = WindowsWorkerLease("C:\\原创库", _native=dlls.native)
        worker.close()
        result = WindowsWorkerLease.observe(files)
        assert result["online"] is False and result["capabilities"]["model_calls"] is None
        assert len(dlls.writes) == before + 1


def test_observe_legacy_worker_has_unknown_capabilities_not_enabled(dlls):
    with WindowsWorkerLease("C:\\原创库", _native=dlls.native) as worker:
        result = worker.observe(worker.files)
        assert result["online"] is True
        assert result["capabilities"] == {"model_calls": None, "source_sync": None}


@pytest.mark.parametrize("mode", ["body", "file", "directory", "root", "release", "close"])
def test_observe_midflight_changes_never_claim_progress_or_keep_permissions(dlls, mode):
    with WindowsWorkerLease(
        "C:\\原创库", allow_model_calls=True, allow_source_sync=True, _native=dlls.native
    ) as worker:
        changed = False

        def change(value):
            nonlocal changed
            if changed:
                return
            changed = True
            if mode == "body":
                dlls.files[value].update(body=b"bad", size=3)
            elif mode == "file":
                key = dlls.name_key(worker.parent._value, worker.component)
                dlls.names[key] = dict(dlls.names[key], id=b"f" * 16)
            elif mode == "directory":
                key = dlls.name_key(worker.files.handle._value, ".context")
                dlls.names[key] = dict(dlls.names[key], id=b"d" * 16)
            elif mode == "root":
                dlls.device_target = "\\Device\\HarddiskVolume77"
            elif mode == "release":
                worker.lock.close()
            else:
                worker.files.close()

        dlls.read_hook = change
        result = worker.observe(worker.files)
        assert result["online"] is (False if mode == "release" else None)
        assert result["capabilities"] == {"model_calls": None, "source_sync": None}
        assert result["progress_verified"] is False
        dlls.read_hook = None


@pytest.mark.parametrize("model,source", [(1, False), (False, "yes"), (True, None), (None, True)])
def test_invalid_permissions_fail_before_any_native_open(dlls, model, source):
    assert (
        code(
            lambda: WindowsWorkerLease(
                "C:\\原创库", allow_model_calls=model, allow_source_sync=source, _native=dlls.native
            )
        )
        == "invalid_argument"
    )
    assert not dlls.opened and not dlls.writes


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"nonce": "e_" + "a" * 32, "pid": True},
        {"nonce": "e_" + "a" * 32, "pid": 1, "capabilities": None},
        {"nonce": "e_" + "a" * 32, "pid": 1, "capabilities": {"model_calls": 1, "source_sync": False}},
    ],
)
def test_shared_worker_metadata_rejects_malformed_without_pid_liveness(value):
    with pytest.raises(ValueError):
        worker_capabilities(canonical_bytes(value))


def test_shared_worker_metadata_legacy_and_permissions_strict_canonical():
    value = {"nonce": "e_" + "a" * 32, "pid": 987654321}
    assert worker_capabilities(canonical_bytes(value)) is None
    value["capabilities"] = {"model_calls": False, "source_sync": True}
    assert worker_capabilities(canonical_bytes(value)) == value["capabilities"]
    with pytest.raises(ValueError):
        worker_capabilities(json.dumps(value).encode())


def test_close_interruption_attempts_all_owned_handles_then_preserves_interrupt(dlls):
    lease = WindowsWriterLease("C:\\原创库", _native=dlls.native)
    values = (lease.handle._value, lease.parent._value, lease.files.handle._value)

    def interrupt(*_):
        raise KeyboardInterrupt

    dlls.kernel32.UnlockFileEx.call = interrupt
    with pytest.raises(KeyboardInterrupt):
        lease.close()
    assert all(value in dlls.closed for value in values)
    assert not any(dlls.held.values())
    assert code(lease.check) == "writer_not_owned"


def test_constructor_write_interruption_closes_root_parents_and_file(dlls):
    def interrupt(_):
        raise KeyboardInterrupt

    dlls.write_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        WindowsWriterLease("C:\\原创库", _native=dlls.native)
    assert not any(dlls.held.values())
    assert len(dlls.writes) == 1 and not dlls.truncations and not dlls.flushes
    assert set(dlls.files).issubset(set(dlls.closed))


@pytest.mark.parametrize("failure", ["large", "hardlink", "reparse", "acl"])
def test_existing_unsafe_lease_is_not_locked_or_repaired(dlls, failure):
    lease = WindowsWriterLease("C:\\原创库", _native=dlls.native)
    entry = dlls.files[lease.handle._value]
    lease.close()
    if failure == "large":
        entry.update(size=65_537)
    elif failure == "hardlink":
        entry.update(links=2)
    elif failure == "reparse":
        entry.update(tag=0xA000000C)
    else:
        dlls.sd = bytes(20)
    before = len(dlls.locks), len(dlls.writes), len(dlls.truncations)
    with pytest.raises(ContextError):
        WindowsWriterLease("C:\\原创库", _native=dlls.native)
    assert before == (len(dlls.locks), len(dlls.writes), len(dlls.truncations))


def test_native_lease_bootstrap_role_and_creation_status_fail_closed(dlls):
    with WindowsFiles("C:\\原创库", _native=dlls.native) as files:
        files.mkdir(".context")
        with files._parent(".context/lease") as (parent, component):
            dlls.created_information = 1
            assert code(lambda: dlls.native.create_lease_file(parent, component)) == "storage_unavailable"
            assert code(lambda: dlls.native.create_lease_file(parent, "../other")) == "forbidden_path"
        with files._parent(".context/lease") as (parent, component):
            with dlls.native.open_relative(parent, component, role="lease_observer") as observer:
                assert code(lambda: dlls.native.create_lease_file(observer, "other")) == "forbidden_path"
    assert not dlls.writes and not dlls.locks


@pytest.mark.parametrize("kind", [WindowsExecutorLease, WindowsWriterLease, WindowsWorkerLease])
def test_lease_paths_and_errors_match_common_application_contract(kind):
    from collection_context.infrastructure import ownership

    posix = getattr(ownership, kind.__name__.removeprefix("Windows"))
    for name in ("path", "busy", "not_owned", "changed", "unavailable", "label"):
        assert getattr(kind, name) == getattr(posix, name)
