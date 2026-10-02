"""Injected bounded lease-write contracts only; no Windows kernel or disk claims."""

from __future__ import annotations

import ctypes
import threading

import pytest
from test_context_windows_native import FakeDLLs, code

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native


@pytest.fixture
def lease():
    dlls = FakeDLLs()
    with dlls.native.open_root_directory("C:\\原创库") as root:
        with dlls.native.open_relative(root, "ownership.json", role="lease_file") as leaf:
            with dlls.native.try_lock(leaf, exclusive=True) as lock:
                yield dlls, leaf, lock


@pytest.mark.parametrize("body", [b"", b"\x00\xff", b"a" * 65_536])
@pytest.mark.parametrize("short_write", [3, 65_536])
def test_exact_binary_empty_and_short_writes_are_truncated_flushed_and_verified(lease, body, short_write):
    dlls, leaf, lock = lease
    dlls.write_limit = short_write
    dlls.native.write_lease_metadata(lock, body)
    assert dlls.files[leaf._value]["body"] == body
    assert dlls.truncations == dlls.flushes == [leaf._value]
    assert lock.exclusive and not lock.closed and leaf._active_lock is lock
    assert all(size <= 65_536 and overlap is None for _, size, overlap in dlls.writes)
    assert dlls.native.read_file(leaf) == body
    if not body:
        assert not dlls.writes  # WriteFile(..., 0) cannot truncate.


def test_signatures_do_not_use_host_long_or_overlapped_io(lease):
    dlls, _, _ = lease
    assert dlls.kernel32.WriteFile.restype is native.I32
    assert dlls.kernel32.WriteFile.argtypes == [
        native.HANDLE,
        native.HANDLE,
        native.U32,
        ctypes.POINTER(native.U32),
        native.HANDLE,
    ]
    for name in ("SetEndOfFile", "FlushFileBuffers"):
        assert getattr(dlls.kernel32, name).argtypes == [native.HANDLE]
        assert getattr(dlls.kernel32, name).restype is native.I32


@pytest.mark.parametrize("body", [b"a" * 65_537, bytearray(b"a"), "secret", None, 5])
def test_invalid_or_oversized_input_never_seeks_or_writes(lease, body):
    dlls, _, lock = lease
    assert code(lambda: dlls.native.write_lease_metadata(lock, body)) == "invalid_argument"
    assert not dlls.seeks and not dlls.writes and not dlls.truncations and not dlls.flushes


@pytest.mark.parametrize("role", ["directory", "read_file", "lease_observer", "lease_file"])
def test_handles_and_shared_locks_never_become_write_authority(lease, role):
    dlls, _, _ = lease
    with dlls.native.open_root_directory("C:\\原创库") as root:
        with dlls.native.open_relative(root, "x", role=role) as handle:
            assert code(lambda: dlls.native.write_lease_metadata(handle, b"a")) == "forbidden_path"
            if role in {"lease_file", "lease_observer"}:
                with dlls.native.try_lock(handle, exclusive=False) as shared:
                    assert code(lambda: dlls.native.write_lease_metadata(shared, b"a")) == "lock_changed"
    assert not dlls.writes and not dlls.truncations


def test_closed_foreign_and_superseded_locks_do_not_write(lease):
    dlls, leaf, lock = lease
    assert code(lambda: FakeDLLs().native.write_lease_metadata(lock, b"a")) == "storage_unavailable"
    lock.close()
    with dlls.native.try_lock(leaf, exclusive=True) as newer:
        assert code(lambda: dlls.native.write_lease_metadata(lock, b"a")) == "lock_changed"
        dlls.native.write_lease_metadata(newer, b"new")
    assert len(dlls.writes) == 1


@pytest.mark.parametrize("failure", ["seek", "position", "oversized", "identity", "hardlink"])
def test_preflight_rejects_without_modifying_metadata(lease, failure):
    dlls, leaf, lock = lease
    if failure == "seek":
        dlls.seek_ok = False
    elif failure == "position":
        dlls.seek_result = 1
    elif failure == "oversized":
        dlls.files[leaf._value]["size"] = 65_537
    elif failure == "identity":
        dlls.files[leaf._value]["id"] = b"changed identity"
    else:
        dlls.files[leaf._value]["links"] = 2
    with pytest.raises(ContextError):
        dlls.native.write_lease_metadata(lock, b"new")
    assert not dlls.writes and not dlls.truncations and not dlls.flushes
    assert leaf._active_lock is lock


@pytest.mark.parametrize(
    "failure", ["write", "zero", "count", "truncate", "flush", "verify", "length", "identity"]
)
def test_unknown_write_invalidates_authority_retains_kernel_lock_and_never_retries(lease, failure):
    dlls, leaf, lock = lease
    if failure == "write":
        dlls.write_ok = False
    elif failure == "zero":
        dlls.write_count_override = 0
    elif failure == "count":
        dlls.write_count_override = 10_000
    elif failure == "truncate":
        dlls.truncate_ok = False
    elif failure == "flush":
        dlls.flush_ok = False
    elif failure == "verify":
        dlls.flush_hook = lambda value: dlls.files[value].update(body=b"bad")
    elif failure == "length":
        dlls.flush_hook = lambda value: dlls.files[value].update(size=123)
    else:
        dlls.flush_hook = lambda value: dlls.files[value].update(id=b"changed identity")
    with pytest.raises(ContextError) as caught:
        dlls.native.write_lease_metadata(lock, b"new")
    assert caught.value.code == "storage_unavailable"
    assert not caught.value.retryable and not caught.value.possibly_charged
    assert "new" not in str(caught.value)
    assert leaf._active_lock is None and leaf._locks == 1 and not lock.closed
    assert not dlls.unlocks
    events = (len(dlls.writes), len(dlls.truncations), len(dlls.flushes))
    assert code(lambda: dlls.native.write_lease_metadata(lock, b"new")) == "lock_changed"
    assert events == (len(dlls.writes), len(dlls.truncations), len(dlls.flushes))


def test_partial_write_failure_is_not_truncated_or_retried(lease):
    dlls, leaf, lock = lease
    dlls.write_limit = 2
    dlls.write_hook = lambda _: setattr(dlls, "write_ok", False)
    assert code(lambda: dlls.native.write_lease_metadata(lock, b"abcdef")) == "storage_unavailable"
    assert len(dlls.writes) == 2 and not dlls.truncations and not dlls.flushes
    assert dlls.files[leaf._value]["body"].startswith(b"ab")
    assert leaf._active_lock is None


@pytest.mark.parametrize("action", ["lock", "handle"])
def test_reentrant_close_prevents_using_cached_authority_in_next_native_call(lease, action):
    dlls, leaf, lock = lease
    dlls.write_limit = 2
    dlls.write_hook = lambda _: lock.close() if action == "lock" else leaf.close()
    assert code(lambda: dlls.native.write_lease_metadata(lock, b"abcdef")) == "storage_unavailable"
    assert len(dlls.writes) == 1 and not dlls.truncations and not dlls.flushes


def test_unlock_failure_keeps_owned_range_but_disables_further_writes(lease):
    dlls, leaf, lock = lease
    dlls.unlock_ok = False
    assert code(lock.close) == "storage_unavailable"
    assert not lock.closed and leaf._locks == 1 and leaf._active_lock is None
    assert code(lambda: dlls.native.write_lease_metadata(lock, b"x")) == "lock_changed"
    assert code(lambda: dlls.native.try_lock(leaf, exclusive=True)) == "forbidden_path"
    dlls.unlock_ok = True
    lock.close()
    assert lock.closed and leaf._locks == 0


def test_interrupt_poisoning_preserves_control_flow_and_owned_lock(lease):
    dlls, leaf, lock = lease

    def interrupt(_):
        raise KeyboardInterrupt

    dlls.write_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        dlls.native.write_lease_metadata(lock, b"new")
    assert leaf._active_lock is None and leaf._locks == 1 and not lock.closed


def test_unlock_waits_for_write_flush_and_verification(lease):
    dlls, leaf, lock = lease
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    failures = []

    def wait_in_write(_):
        entered.set()
        assert release.wait(3)

    def write():
        try:
            dlls.native.write_lease_metadata(lock, b"new")
        except BaseException as error:
            failures.append(type(error).__name__)

    def close():
        closing.set()
        lock.close()
        closed.set()

    dlls.write_hook = wait_in_write
    writer, closer = threading.Thread(target=write), threading.Thread(target=close)
    writer.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert closing.wait(2) and not closed.wait(0.02)
        assert leaf._active_lock is lock
    finally:
        release.set()
        writer.join(3)
        if closer.ident is not None:
            closer.join(3)
    assert not failures and not writer.is_alive() and not closer.is_alive()
    assert closed.is_set() and dlls.flushes and dlls.reads


@pytest.mark.parametrize("symbol", ["WriteFile", "SetEndOfFile", "FlushFileBuffers"])
def test_missing_write_symbol_is_not_a_silent_path_fallback(symbol):
    dlls = FakeDLLs()
    delattr(dlls.kernel32, symbol)
    assert code(lambda: dlls.native.open_root_directory("C:\\原创库")) == "unsupported_platform"
    assert not dlls.opened
