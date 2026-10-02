"""Real POSIX private files plus injected Windows IO; no Windows OS claim."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_context_windows_publication import PublicationDLLs

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import files as posix
from collection_context.infrastructure.file_stream import STREAM_CHUNK_BYTES, verified_chunks
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.windows_files import WindowsFiles


def digest(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture(params=["posix", "windows_injected"])
def scope(request, tmp_path):
    if request.param == "posix":
        root = tmp_path / "private"
        root.mkdir(mode=0o700)
        with SafeFiles(root) as tree:
            yield tree, None
    else:
        dlls = PublicationDLLs()
        with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:
            yield tree, dlls


@pytest.mark.parametrize("body", [b"", b"original\x00\xff", b"x" * (STREAM_CHUNK_BYTES * 3 + 17)])
def test_shared_bounded_publication_readback_and_metadata(scope, body):
    tree, dlls = scope
    source = (body[start : start + STREAM_CHUNK_BYTES] for start in range(0, len(body), STREAM_CHUNK_BYTES))
    tree.write_chunks("archive/input", source, expected_size=len(body), expected_sha256=digest(body))
    assert tree.read("archive/input", max_bytes=len(body), private=True) == body
    assert tree.file_size("archive/input") == len(body)
    if dlls:
        assert max(entry[1] for entry in dlls.reads) <= STREAM_CHUNK_BYTES
        assert all(entry[1] <= STREAM_CHUNK_BYTES for entry in dlls.writes)
        assert len(dlls.renames) == 1


@pytest.mark.parametrize("replace", [False, True])
def test_existing_result_is_preserved_unless_explicit_replace(scope, replace):
    tree, _ = scope
    tree.write("target", b"retained")
    if replace:
        tree.write_chunks("target", [b"new"], expected_size=3, expected_sha256=digest(b"new"), replace=True)
        assert tree.read("target", private=True) == b"new"
    else:
        with pytest.raises(ContextError) as caught:
            tree.write_chunks("target", [b"new"], expected_size=3, expected_sha256=digest(b"new"))
        assert caught.value.code == "write_conflict"
        assert tree.read("target", private=True) == b"retained"


@pytest.mark.parametrize(
    "chunks,size,sha",
    [
        ([b"short"], 8, digest(b"original")),
        ([b"original!"], 8, digest(b"original")),
        ([b"original"], 8, "0" * 64),
        ([b""], 0, digest(b"")),
        ([bytearray(b"original")], 8, digest(b"original")),
        ([b"x" * (STREAM_CHUNK_BYTES + 1)], STREAM_CHUNK_BYTES + 1, digest(b"x" * (STREAM_CHUNK_BYTES + 1))),
    ],
)
def test_invalid_partial_or_unverified_stream_never_publishes(scope, chunks, size, sha):
    tree, dlls = scope
    with pytest.raises(ContextError) as caught:
        tree.write_chunks("target", chunks, expected_size=size, expected_sha256=sha)
    assert caught.value.code == "file_stream_integrity"
    assert not tree.entry_exists("target")
    if dlls:
        assert not dlls.renames
    else:
        assert not list(tree.root.iterdir())


@pytest.mark.parametrize(
    "size,sha,replace,cancel,chunks",
    [
        (True, "0" * 64, False, lambda: None, []),
        (-1, "0" * 64, False, lambda: None, []),
        (2_147_483_649, "0" * 64, False, lambda: None, []),
        (0, "bad", False, lambda: None, []),
        (0, "F" * 64, False, lambda: None, []),
        (0, "0" * 64, 1, lambda: None, []),
        (0, "0" * 64, False, None, []),
        (0, "0" * 64, False, lambda: None, None),
    ],
)
def test_invalid_contract_before_directory_creation_or_read(scope, size, sha, replace, cancel, chunks):
    tree, dlls = scope
    opens = len(dlls.opened) if dlls else 0
    with pytest.raises(ContextError) as caught:
        tree.write_chunks(
            "new/sub/target",
            chunks,
            expected_size=size,
            expected_sha256=sha,
            replace=replace,
            check_cancel=cancel,
        )
    assert caught.value.code == "invalid_argument"
    if dlls:
        assert len(dlls.opened) == opens and not dlls.writes
    else:
        assert not list(tree.root.iterdir())


@pytest.mark.parametrize("after_chunk", [False, True])
def test_cancel_stops_source_and_never_renames(scope, after_chunk):
    tree, dlls = scope
    seen = []
    stopped = not after_chunk

    def check():
        if stopped:
            raise ContextError("runtime_install_cancelled", "cancelled")

    def source():
        nonlocal stopped
        seen.append(1)
        yield b"first"
        stopped = True
        yield b"second"
        pytest.fail("consumed after cancellation")

    with pytest.raises(ContextError) as caught:
        tree.write_chunks(
            "target", source(), expected_size=11, expected_sha256=digest(b"firstsecond"), check_cancel=check
        )
    assert caught.value.code == "runtime_install_cancelled"
    assert seen == ([1] if after_chunk else [])
    assert not tree.entry_exists("target")
    if dlls:
        assert not dlls.renames


def test_bounded_windows_hash_readback_does_not_use_buffered_read(monkeypatch):
    dlls = PublicationDLLs()
    monkeypatch.setattr(dlls.native, "read_file", lambda *a, **kw: pytest.fail("whole-body read"))
    dlls.write_limit = 13
    body = b"abc" * 30_000
    with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:
        tree.write_chunks(
            "target",
            [body[:STREAM_CHUNK_BYTES], body[STREAM_CHUNK_BYTES:]],
            expected_size=len(body),
            expected_sha256=digest(body),
        )
        assert len(dlls.writes) > 2 and all(entry[1] <= STREAM_CHUNK_BYTES for entry in dlls.writes)
        assert tree.file_size("target") == len(body)


def test_posix_collision_does_not_delete_existing_staging_name(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    prior = tmp_path / (".tmp-" + "a" * 32)
    prior.write_bytes(b"retained")
    prior.chmod(0o600)
    monkeypatch.setattr(posix.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    with SafeFiles(tmp_path) as tree:
        with pytest.raises(ContextError) as caught:
            tree.write_chunks("target", [b"new"], expected_size=3, expected_sha256=digest(b"new"))
        assert caught.value.code == "write_conflict"
    assert prior.read_bytes() == b"retained" and not (tmp_path / "target").exists()


@pytest.mark.parametrize("change", ["mode", "hardlink", "body", "spelling", "root", "parent"])
def test_posix_tampering_never_publishes_or_deletes_replacement(tmp_path, change, monkeypatch):
    tmp_path.chmod(0o700)
    target_parent = tmp_path / "nested"
    target_parent.mkdir(mode=0o700)

    def source():
        yield b"first"
        temp = next(target_parent.glob(".tmp-*"))
        if change == "mode":
            temp.chmod(0o644)
        elif change == "hardlink":
            os.link(temp, target_parent / "other")
        elif change == "body":
            original = os.fsync

            def corrupt_after_flush(fd):
                original(fd)
                os.pwrite(fd, b"evil!", 0)

            monkeypatch.setattr(posix.os, "fsync", corrupt_after_flush)
        elif change == "spelling":
            temp.rename(target_parent / "old-stage")
            temp.write_bytes(b"foreign replacement")
        elif change == "root":
            tmp_path.chmod(0o755)
        else:
            target_parent.chmod(0o755)
        yield b"second"

    with SafeFiles(tmp_path) as tree:
        with pytest.raises(ContextError):
            tree.write_chunks(
                "nested/target", source(), expected_size=11, expected_sha256=digest(b"firstsecond")
            )
    assert not (target_parent / "target").exists()
    if change == "spelling":
        assert next(target_parent.glob(".tmp-*")).read_bytes() == b"foreign replacement"


def test_verified_generator_checks_cancel_before_touching_next_chunk():
    calls = []

    def source():
        calls.append(True)
        yield b"x"

    def cancelled():
        raise ContextError("runtime_install_cancelled", "cancelled")

    stream = verified_chunks(source(), expected_size=1, expected_sha256=digest(b"x"), check_cancel=cancelled)
    with pytest.raises(ContextError):
        next(stream)
    assert not calls


@pytest.mark.parametrize("change", ["root", "acl", "read_failure", "short", "body", "version"])
def test_windows_stage_readback_failure_never_publishes(change):
    dlls = PublicationDLLs()
    with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:

        def changed(value):
            if change == "root":
                dlls.device_target = "\\Device\\HarddiskVolume99"
            elif change == "acl":
                from test_context_windows_security import ace, descriptor

                dlls.descriptors[value] = descriptor(ace(bytes.fromhex("010100000000000100000000")))
            elif change == "read_failure":
                dlls.read_ok = False
            elif change == "short":
                dlls.files[value].update(body=b"first", size=5)
            elif change == "body":
                dlls.files[value]["body"] = b"evil!second"
            else:
                dlls.read_hook = lambda current: dlls.files[current].update(change_time=999)

        dlls.flush_hook = changed
        with pytest.raises(ContextError):
            tree.write_chunks(
                "target", [b"first", b"second"], expected_size=11, expected_sha256=digest(b"firstsecond")
            )
        assert not dlls.renames


@pytest.mark.parametrize("expected", [0, 1, -1, True, 2_147_483_649])
def test_windows_digest_exact_length_limits_and_roles(expected):
    dlls = PublicationDLLs()
    with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:
        tree.write("target", b"ab")
        with dlls.native.open_relative(tree.handle, "target", role="read_file") as handle:
            before = len(dlls.reads)
            with pytest.raises(ContextError):
                dlls.native.file_digest(handle, expected_size=expected)
            assert len(dlls.reads) == before
        with pytest.raises(ContextError) as caught:
            dlls.native.file_digest(tree.handle, expected_size=2)
        assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("blocks", [16, 256])
def test_real_posix_stream_python_allocations_do_not_scale_with_archive(tmp_path, blocks):
    # Fresh interpreter: do not reset or inspect the parent's allocator trace.
    # This proves Python buffering for synthetic 1/16MiB files, NOT total RSS
    # or real network/native-Windows resource use for multi-GiB components.
    program = """
import hashlib, json, pathlib, sys, tracemalloc
from collection_context.infrastructure.files import SafeFiles
root, blocks = pathlib.Path(sys.argv[1]), int(sys.argv[2])
root.chmod(0o700)
chunk = b'x' * 65536
sha = hashlib.sha256()
for _ in range(blocks):
    sha.update(chunk)
with SafeFiles(root) as files:
    tracemalloc.start()
    files.write_chunks('artifact', (chunk for _ in range(blocks)),
                       expected_size=blocks*65536, expected_sha256=sha.hexdigest())
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({'peak_bytes': peak, 'size': files.file_size('artifact')}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(blocks)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(posix.__file__).resolve().parents[2]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        },
    )
    measured = json.loads(result.stdout)
    assert measured["size"] == blocks * STREAM_CHUNK_BYTES
    assert measured["peak_bytes"] < 2_000_000


def test_windows_cancel_after_rename_is_unknown_not_safe_to_repeat():
    dlls = PublicationDLLs()
    stopped = False

    def check():
        if stopped:
            raise ContextError("runtime_install_cancelled", "cancelled")

    def submitted(*_):
        nonlocal stopped
        stopped = True

    dlls.rename_hook = submitted
    with WindowsFiles("C:\\原创库", _native=dlls.native) as tree:
        with pytest.raises(ContextError) as caught:
            tree.write_chunks(
                "target", [b"new"], expected_size=3, expected_sha256=digest(b"new"), check_cancel=check
            )
        assert caught.value.code == "storage_unavailable" and not caught.value.retryable
        assert len(dlls.renames) == 1 and tree.read("target", private=True) == b"new"


def test_posix_post_publication_flush_failure_is_unknown_no_rewrite(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    original = os.fsync
    calls = 0

    def fail_directory(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic directory fsync failure")
        original(fd)

    monkeypatch.setattr(posix.os, "fsync", fail_directory)
    with SafeFiles(tmp_path) as tree:
        with pytest.raises(ContextError) as caught:
            tree.write_chunks("target", [b"new"], expected_size=3, expected_sha256=digest(b"new"))
        assert caught.value.code == "storage_unavailable" and not caught.value.retryable
        assert tree.read("target", private=True) == b"new" and calls == 2


def test_posix_parent_spelling_replacement_stops_before_publication(tmp_path):
    tmp_path.chmod(0o700)
    parent = tmp_path / "nested"
    parent.mkdir(mode=0o700)

    def source():
        yield b"first"
        parent.rename(tmp_path / "detached")
        parent.mkdir(mode=0o700)
        yield b"second"

    with SafeFiles(tmp_path) as tree:
        with pytest.raises(ContextError) as caught:
            tree.write_chunks(
                "nested/target", source(), expected_size=11, expected_sha256=digest(b"firstsecond")
            )
        assert caught.value.code == "version_changed"
    assert not (parent / "target").exists() and not (tmp_path / "detached" / "target").exists()
