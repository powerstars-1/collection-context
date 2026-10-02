"""Real POSIX streams and injected Windows ABI, not Windows OS acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from test_context_file_stream import scope as scope
from test_context_runtime_installation import plan_for

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import files as posix
from collection_context.infrastructure.file_stream import STREAM_CHUNK_BYTES
from collection_context.infrastructure.runtime_installation import RuntimeInstaller


def failed(operation, code):
    with pytest.raises(ContextError) as caught:
        operation()
    assert caught.value.code == code


@pytest.mark.parametrize("body", [b"", b"original\x00\xff", b"x" * (STREAM_CHUNK_BYTES * 3 + 7)])
def test_shared_eof_checks_bounded_chunks_binary_and_empty(scope, body):
    tree, dlls = scope
    tree.write("source/input", body)
    with tree.read_chunks("source/input", max_bytes=len(body), private=True) as source:
        chunks = list(source)
        assert b"".join(chunks) == body
        assert all(type(chunk) is bytes and 0 < len(chunk) <= STREAM_CHUNK_BYTES for chunk in chunks)
    assert list(source) == []
    if dlls:
        assert all(size <= STREAM_CHUNK_BYTES for _, size, _ in dlls.reads)


@pytest.mark.parametrize(
    "limit,private,cancel",
    [
        (-1, False, lambda: None),
        (True, False, lambda: None),
        (2_147_483_649, False, lambda: None),
        (1.0, False, lambda: None),
        (0, 1, lambda: None),
        (0, False, None),
    ],
)
def test_invalid_contract_does_not_create_read_or_open_source(scope, limit, private, cancel):
    tree, dlls = scope
    opens = len(dlls.opened) if dlls else 0
    with pytest.raises(ContextError) as caught:
        with tree.read_chunks("new/sub/source", max_bytes=limit, private=private, check_cancel=cancel):
            pytest.fail("invalid stream was opened")
    assert caught.value.code == "invalid_argument"
    if dlls:
        assert len(dlls.opened) == opens
        assert not dlls.reads
    assert not tree.entry_exists("new")


def test_limit_rejects_before_body_read(scope):
    tree, dlls = scope
    tree.write("source", b"original")
    reads = list(dlls.reads) if dlls else []
    with pytest.raises(ContextError) as caught:
        with tree.read_chunks("source", max_bytes=7):
            pytest.fail("oversized source entered")
    assert caught.value.code == "forbidden_path"
    if dlls:
        assert dlls.reads == reads


@pytest.mark.parametrize("failure", [None, ValueError, OSError, KeyboardInterrupt])
def test_early_exit_or_consumer_failure_closes_owned_iterator_and_handles(scope, failure):
    tree, dlls = scope
    body = b"x" * (STREAM_CHUNK_BYTES * 2)
    tree.write("source", body)
    if dlls:
        dlls.closed.clear()
    source = None

    def consume():
        nonlocal source
        with tree.read_chunks("source", private=True) as source:
            assert next(source) == body[:STREAM_CHUNK_BYTES]
            if failure:
                raise failure("controlled consumer fixture")

    if failure:
        with pytest.raises(failure, match="controlled consumer fixture"):
            consume()
    else:
        consume()
    assert next(source, None) is None
    if dlls:
        assert dlls.reads[-1][0] in dlls.closed
    assert tree.read("source", private=True) == body


def test_cancellation_between_chunks_releases_scope_without_reading_more(scope):
    tree, dlls = scope
    tree.write("source", b"x" * (STREAM_CHUNK_BYTES * 2))
    stop = threading.Event()

    def cancel():
        if stop.is_set():
            raise ContextError("runtime_install_cancelled", "controlled cancellation")

    with tree.read_chunks("source", check_cancel=cancel) as source:
        assert len(next(source)) == STREAM_CHUNK_BYTES
        reads = len(dlls.reads) if dlls else 0
        stop.set()
        failed(lambda: next(source), "runtime_install_cancelled")
        if dlls:
            assert len(dlls.reads) == reads
    assert next(source, None) is None


def test_iterator_cannot_be_transferred_to_a_different_thread(scope):
    tree, _ = scope
    tree.write("source", b"original")
    outcomes = []
    with tree.read_chunks("source") as source:

        def consume():
            try:
                next(source)
            except ContextError as error:
                outcomes.append(error.code)

        thread = threading.Thread(target=consume)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and outcomes == ["invalid_argument"]
    assert next(source, None) is None


def test_same_length_source_change_after_prefix_fails_before_eof_signoff(scope):
    tree, dlls = scope
    body = b"x" * (STREAM_CHUNK_BYTES * 2)
    tree.write("source", body)
    with tree.read_chunks("source", private=True) as source:
        next(source)
        if dlls:
            dlls.files[dlls.reads[-1][0]]["change_time"] = 42
        else:
            info = (tree.root / "source").stat()
            os.utime(tree.root / "source", ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        failed(lambda: list(source), "version_changed")


@pytest.mark.parametrize("relative", ["../outside", "a/../b", "/absolute", "a\\b", "C:source"])
def test_stream_paths_are_bounded_by_the_same_facade(scope, relative):
    tree, _ = scope
    with pytest.raises(ContextError):
        with tree.read_chunks(relative):
            pytest.fail("unsafe source entered")


def test_real_installer_copy_consumer_uses_only_facade_contract(scope):
    tree, _ = scope
    body = b"original archive bytes\x00" * 8000
    tree.write("source", body)
    plan = plan_for(body)

    class FacadeOnly:
        def read_chunks(self, *args, **kwargs):
            return tree.read_chunks(*args, **kwargs)

        def write_chunks(self, *args, **kwargs):
            return tree.write_chunks(*args, **kwargs)

        @property
        def fd(self):
            pytest.fail("installer copied through a POSIX descriptor")

    files = FacadeOnly()
    RuntimeInstaller._copy_snapshot(files, files, "source", plan, None)
    assert tree.read(".source.archive", max_bytes=len(body), private=True) == body


@pytest.mark.parametrize("change", [{"sha256": "0" * 64}, {"bytes": 1}, {"bytes": 20}])
def test_installer_never_publishes_a_mismatched_snapshot(scope, change):
    tree, _ = scope
    tree.write("source", b"original")
    plan = replace(plan_for(b"original"), **change)
    failed(
        lambda: RuntimeInstaller._copy_snapshot(tree, tree, "source", plan, None), "runtime_install_integrity"
    )
    assert not tree.entry_exists(".source.archive")


def test_snapshot_conflict_keeps_existing_result(scope):
    tree, _ = scope
    tree.write("source", b"original")
    tree.write(".source.archive", b"retained")
    failed(
        lambda: RuntimeInstaller._copy_snapshot(tree, tree, "source", plan_for(b"original"), None),
        "write_conflict",
    )
    assert tree.read(".source.archive") == b"retained"


def test_digest_from_stream_does_not_require_materializing_the_whole_file(scope):
    tree, _ = scope
    body = b"x" * (STREAM_CHUNK_BYTES * 5 + 1)
    tree.write("source", body)
    digest = hashlib.sha256()
    with tree.read_chunks("source", private=True) as source:
        for chunk in source:
            digest.update(chunk)
    assert digest.hexdigest() == hashlib.sha256(body).hexdigest()


def test_snapshot_source_version_change_is_rejected_before_publication(scope):
    tree, dlls = scope
    body = b"x" * (STREAM_CHUNK_BYTES * 2)
    tree.write("source", body)

    class ChangingReader:
        @contextmanager
        def read_chunks(self, *args, **kwargs):
            with tree.read_chunks(*args, **kwargs) as original:

                def changed():
                    yield next(original)
                    if dlls:
                        dlls.files[dlls.reads[-1][0]]["change_time"] = 42
                    else:
                        info = (tree.root / "source").stat()
                        os.utime(tree.root / "source", ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
                    yield from original

                chunks = changed()
                try:
                    yield chunks
                finally:
                    chunks.close()

    failed(
        lambda: RuntimeInstaller._copy_snapshot(tree, ChangingReader(), "source", plan_for(body), None),
        "runtime_install_integrity",
    )
    assert not tree.entry_exists(".source.archive")


def test_snapshot_cancellation_after_source_io_does_not_publish(scope, monkeypatch):
    tree, dlls = scope
    body = b"x" * (STREAM_CHUNK_BYTES * 2)
    tree.write("source", body)
    stop = threading.Event()
    if dlls:
        dlls.read_hook = lambda _: stop.set()
    else:
        original = posix.os.read

        def stopped(*args):
            result = original(*args)
            stop.set()
            return result

        monkeypatch.setattr(posix.os, "read", stopped)
    failed(
        lambda: RuntimeInstaller._copy_snapshot(tree, tree, "source", plan_for(body), stop),
        "runtime_install_cancelled",
    )
    assert not tree.entry_exists(".source.archive")


def test_posix_parent_path_replacement_is_not_a_valid_eof_proof(tmp_path):
    from collection_context.infrastructure.files import SafeFiles

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as tree:
        tree.write("folder/source", b"original")
        with tree.read_chunks("folder/source") as chunks:
            assert next(chunks) == b"original"
            (root / "folder").rename(root / "retained")
            (root / "folder").mkdir(mode=0o700)
            failed(lambda: list(chunks), "version_changed")


def test_windows_private_acl_change_is_not_a_valid_eof_proof():
    from test_context_windows_publication import PublicationDLLs
    from test_context_windows_security import EVERYONE, ace, descriptor

    from collection_context.infrastructure.windows_files import WindowsFiles

    dlls = PublicationDLLs()
    with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:
        tree.write("source", b"original")
        with tree.read_chunks("source", private=True) as chunks:
            assert next(chunks) == b"original"
            value = dlls.reads[-1][0]
            dlls.descriptors[value] = descriptor(ace(EVERYONE))
            failed(lambda: list(chunks), "unsafe_secret_permissions")


@pytest.mark.parametrize("blocks", [16, 256])
def test_real_posix_snapshot_copy_python_buffering_does_not_scale_with_archive(tmp_path, blocks):
    script = """
import hashlib, json, pathlib, sys, tracemalloc
from types import SimpleNamespace
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.runtime_installation import RuntimeInstaller
root, blocks = pathlib.Path(sys.argv[1]), int(sys.argv[2])
root.chmod(0o700)
chunk = b'x' * 65536
digest = hashlib.sha256()
for _ in range(blocks):
    digest.update(chunk)
plan = SimpleNamespace(bytes=blocks*65536, sha256=digest.hexdigest())
with SafeFiles(root) as files:
    files.write_chunks('source', (chunk for _ in range(blocks)),
                       expected_size=plan.bytes, expected_sha256=plan.sha256)
    tracemalloc.start()
    RuntimeInstaller._copy_snapshot(files, files, 'source', plan, None)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({'peak_bytes': peak, 'size': files.file_size('.source.archive')}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), str(blocks)],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(Path(posix.__file__).resolve().parents[2]),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    measured = json.loads(result.stdout)
    assert measured["size"] == blocks * STREAM_CHUNK_BYTES
    assert measured["peak_bytes"] < 2_000_000  # Python trace only, not native Windows/RSS.
