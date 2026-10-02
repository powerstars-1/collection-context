"""Injected read/seek ABI and invariant tests, NOT Windows kernel acceptance."""

from __future__ import annotations

import ctypes
import threading

import pytest
from test_context_windows_native import FakeDLLs, code

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_native as native


@pytest.fixture
def file():
    dlls = FakeDLLs()
    with dlls.native.open_root_directory("C:\\原创库") as root:
        with dlls.native.open_relative(root, "original.bin", role="read_file") as leaf:
            yield dlls, leaf


def content(dlls, leaf, body):
    dlls.files[leaf._value].update(body=body, size=len(body))


def test_read_abi_and_fixed_synchronous_seek(file):
    dlls, leaf = file
    assert dlls.native.read_file(leaf) == b"fixture-body"
    assert dlls.kernel32.SetFilePointerEx.restype is native.I32
    assert dlls.kernel32.SetFilePointerEx.argtypes == [
        native.HANDLE,
        native.I64,
        ctypes.POINTER(native.I64),
        native.U32,
    ]
    assert dlls.kernel32.ReadFile.argtypes == [
        native.HANDLE,
        native.HANDLE,
        native.U32,
        ctypes.POINTER(native.U32),
        native.HANDLE,
    ]
    assert dlls.seeks == [(leaf._value, 0, 0)]
    assert all(overlap is None for _, _, overlap in dlls.reads)
    assert dlls.native.read_file(leaf) == b"fixture-body"  # Always rewind, do not leak shared pointer state.


@pytest.mark.parametrize("body", [b"", b"\x00\xff\x80", b"a" * 131_073])
@pytest.mark.parametrize("short_read", [2, 65_536])
def test_chunked_binary_empty_and_short_reads(file, body, short_read):
    dlls, leaf = file
    content(dlls, leaf, body)
    dlls.read_limit = short_read
    assert dlls.native.read_file(leaf, max_bytes=len(body)) == body
    assert max(capacity for _, capacity, _ in dlls.reads) <= 65_536


@pytest.mark.parametrize("limit", [-1, True, 1.5, "20", None, (1 << 63) - 1])
def test_invalid_cap_never_allocates_or_reaches_io(file, limit):
    dlls, leaf = file
    assert code(lambda: dlls.native.read_file(leaf, max_bytes=limit)) == "invalid_argument"
    assert not dlls.seeks and not dlls.reads


def test_explicit_large_budget_does_not_silently_shrink_to_default(file):
    dlls, leaf = file
    assert dlls.native.read_file(leaf, max_bytes=128_000_000) == b"fixture-body"


def test_runtime_sized_body_can_exceed_default_with_explicit_budget(file):
    dlls, leaf = file
    body = b"x" * (native.MAX_NATIVE_READ + 1)
    content(dlls, leaf, body)
    assert code(lambda: dlls.native.read_file(leaf)) == "forbidden_path"
    assert dlls.native.read_file(leaf, max_bytes=128_000_000) == body
    assert max(capacity for _, capacity, _ in dlls.reads) <= 65_536


def test_oversized_metadata_rejected_before_seek_or_read(file):
    dlls, leaf = file
    assert code(lambda: dlls.native.read_file(leaf, max_bytes=11)) == "forbidden_path"
    assert not dlls.seeks and not dlls.reads


@pytest.mark.parametrize("role", ["lease_file", "lease_observer"])
def test_lease_metadata_never_uses_general_file_read_budget(file, role):
    dlls, _ = file
    with dlls.native.open_root_directory("C:\\原创库") as root:
        with dlls.native.open_relative(root, "ownership.json", role=role) as leaf:
            assert dlls.native.read_file(leaf) == b"fixture-body"
            content(dlls, leaf, b"x" * 65_537)
            reads, seeks = list(dlls.reads), list(dlls.seeks)
            assert code(lambda: dlls.native.read_file(leaf)) == "forbidden_path"
            assert dlls.reads == reads and dlls.seeks == seeks


@pytest.mark.parametrize("failure", ["seek", "position", "read", "invalid_count"])
def test_native_failures_never_return_partial_body(file, failure):
    dlls, leaf = file
    if failure == "seek":
        dlls.seek_ok = False
    elif failure == "position":
        dlls.seek_result = 1
    elif failure == "read":
        dlls.read_ok = False
    else:
        dlls.read_count_override = 65_537
    assert code(lambda: dlls.native.read_file(leaf)) == "storage_unavailable"


def test_after_partial_read_failure_does_not_expose_body(file):
    dlls, leaf = file
    dlls.read_limit = 2
    dlls.read_hook = lambda _: setattr(dlls, "read_ok", False)
    with pytest.raises(ContextError) as caught:
        dlls.native.read_file(leaf)
    assert caught.value.code == "storage_unavailable"
    assert "fixture" not in str(caught.value)


def test_reentrant_close_never_reuses_cached_handle_for_next_chunk(file):
    dlls, leaf = file
    dlls.read_limit = 2
    dlls.read_hook = lambda _: leaf.close()
    assert code(lambda: dlls.native.read_file(leaf)) == "storage_unavailable"
    assert len(dlls.reads) == 1 and leaf.closed


@pytest.mark.parametrize("empty", [True, False])
def test_explicit_eof_still_requires_full_expected_length(file, empty):
    dlls, leaf = file
    if empty:
        content(dlls, leaf, b"")
    dlls.read_ok, dlls.error = False, 38
    if empty:
        assert dlls.native.read_file(leaf, max_bytes=0) == b""
    else:
        assert code(lambda: dlls.native.read_file(leaf)) == "version_changed"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("size", 13, "version_changed"),
        ("last_write_time", 4, "version_changed"),
        ("change_time", 4, "version_changed"),
        ("creation_time", 4, "version_changed"),
        ("id", b"changed identity", "lock_changed"),
        ("links", 2, "forbidden_path"),
        ("tag", 0xA000000C, "forbidden_path"),
        ("delete", 1, "forbidden_path"),
    ],
)
def test_post_read_version_identity_or_safety_change_is_rejected(file, field, value, expected):
    dlls, leaf = file
    dlls.read_hook = lambda handle: dlls.files[handle].update({field: value})
    assert code(lambda: dlls.native.read_file(leaf)) == expected


def test_data_length_mismatch_and_hidden_growth_rejected(file):
    dlls, leaf = file
    dlls.files[leaf._value]["body"] = b"short"
    assert code(lambda: dlls.native.read_file(leaf)) == "version_changed"
    dlls.files[leaf._value]["body"] = b"a" * 13
    assert code(lambda: dlls.native.read_file(leaf, max_bytes=12)) == "version_changed"


def test_directory_closed_and_foreign_handle_never_reach_read(file):
    dlls, leaf = file
    with dlls.native.open_root_directory("C:\\目录") as root:
        assert code(lambda: dlls.native.read_file(root)) == "forbidden_path"
    assert code(lambda: FakeDLLs().native.read_file(leaf)) == "storage_unavailable"
    leaf.close()
    assert code(lambda: dlls.native.read_file(leaf)) == "storage_unavailable"
    assert not dlls.reads and not dlls.seeks


def test_owned_close_waits_for_the_synchronous_read(file):
    dlls, leaf = file
    entered, release, close_entered, closed = (threading.Event() for _ in range(4))
    outcome = []

    def wait_in_read(_):
        entered.set()
        assert release.wait(3)

    dlls.read_hook = wait_in_read
    reader = threading.Thread(target=lambda: outcome.append(dlls.native.read_file(leaf)))

    def close():
        close_entered.set()
        leaf.close()
        closed.set()

    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert close_entered.wait(2)
        assert not closed.wait(0.02)
        assert not leaf.closed
    finally:
        release.set()
        reader.join(timeout=3)
        if closer.ident is not None:
            closer.join(timeout=3)
    assert not reader.is_alive() and not closer.is_alive()
    assert closed.is_set() and outcome == [b"fixture-body"]
