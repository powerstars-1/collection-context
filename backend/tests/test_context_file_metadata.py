"""Metadata-only contracts and application consumers in isolated POSIX fixtures."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.library_management import LibraryManagement
from collection_context.infrastructure import files as module
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import FileSecrets


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "原创 metadata 库"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as safe:
        yield safe


def test_presence_and_size_do_not_open_or_read_body(files, monkeypatch):
    path = files.root / "large.bin"
    with path.open("wb") as stream:
        stream.truncate(40_000_000)  # Sparse original fixture, not a downloaded video.
    opened = []
    original = module.os.open

    def open_directory_only(name, flags, *args, **kwargs):
        opened.append((name, flags))
        assert flags & os.O_DIRECTORY
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_directory_only)
    monkeypatch.setattr(files, "read", lambda *_args, **_kwargs: pytest.fail("body read during metadata"))
    assert files.entry_exists("large.bin") is True
    assert files.file_size("large.bin") == 40_000_000
    assert not opened  # Direct entries need only the already-owned root descriptor.


def test_missing_leaf_and_parent_are_confirmed_absence_without_creation(files):
    assert files.entry_exists("missing") is False
    assert files.entry_exists("missing/child") is False
    assert not list(files.root.iterdir())
    for path in ("missing", "missing/child"):
        with pytest.raises(ContextError) as caught:
            files.file_size(path)
        assert caught.value.code == "not_found"


@pytest.mark.parametrize("kind", ["directory", "broken-link", "link", "hardlink", "fifo"])
def test_presence_is_not_readable_file_authority_and_size_rejects_nodes(files, tmp_path, kind):
    path = files.root / "entry"
    if kind == "directory":
        path.mkdir()
    elif kind == "broken-link":
        path.symlink_to(tmp_path / "not-found")
    elif kind == "link":
        outside = tmp_path / "outside"
        outside.write_bytes(b"original fixture")
        path.symlink_to(outside)
    elif kind == "hardlink":
        path.write_bytes(b"original fixture")
        os.link(path, files.root / "second")
    else:
        os.mkfifo(path)
    assert files.entry_exists("entry") is True
    with pytest.raises(ContextError) as caught:
        files.file_size("entry")
    assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("method", ["entry_exists", "file_size"])
@pytest.mark.parametrize("path", ["", "../outside", "/absolute", "a//b", "a\\b", "a:stream"])
def test_metadata_rejects_paths_before_leaf_probe(files, monkeypatch, method, path):
    original = module.os.stat

    def no_stat(*args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            pytest.fail("invalid relative path reached leaf stat")
        return original(*args, **kwargs)

    monkeypatch.setattr(module.os, "stat", no_stat)
    with pytest.raises(ContextError) as caught:
        getattr(files, method)(path)
    assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("method", ["entry_exists", "file_size"])
def test_inaccessible_stat_is_unknown_not_absence(files, monkeypatch, method):
    original = module.os.stat

    def inaccessible(*args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise PermissionError("fixture failure must not leak")
        return original(*args, **kwargs)

    monkeypatch.setattr(module.os, "stat", inaccessible)
    with pytest.raises(ContextError) as caught:
        getattr(files, method)("entry")
    assert caught.value.code == "storage_unavailable"
    assert "fixture failure" not in str(caught.value)


@pytest.mark.parametrize("method", ["entry_exists", "file_size"])
def test_root_changed_during_stat_never_returns_stale_result(files, tmp_path, monkeypatch, method):
    (files.root / "entry").write_bytes(b"original")
    original = module.os.stat
    changed = False

    def replacing(name, *args, **kwargs):
        nonlocal changed
        result = original(name, *args, **kwargs)
        if name == "entry" and not changed:
            changed = True
            files.root.rename(tmp_path / "preserved")
            files.root.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(module.os, "stat", replacing)
    with pytest.raises(ContextError) as caught:
        getattr(files, method)("entry")
    assert caught.value.code == "storage_unavailable"


@pytest.mark.parametrize("change", ["body", "replacement", "hardlink"])
def test_size_version_change_rejected_without_reading_content(files, monkeypatch, change):
    path = files.root / "entry"
    path.write_bytes(b"original")
    original = module.os.stat
    changed = False

    def changing(name, *args, **kwargs):
        nonlocal changed
        result = original(name, *args, **kwargs)
        if name == "entry" and not changed:
            changed = True
            if change == "body":
                path.write_bytes(b"changed size")
            elif change == "replacement":
                path.rename(files.root / "preserved")
                path.write_bytes(b"original")
            else:
                os.link(path, files.root / "extra")
        return result

    monkeypatch.setattr(module.os, "stat", changing)
    with pytest.raises(ContextError) as caught:
        files.file_size("entry")
    assert caught.value.code == "version_changed"


def test_closed_descriptor_metadata_is_controlled_error(files):
    files.close()
    for method in (files.entry_exists, files.file_size):
        with pytest.raises(ContextError) as caught:
            method("entry")
        assert caught.value.code == "storage_unavailable"


@pytest.mark.parametrize("present", [False, True])
def test_file_credentials_use_presence_contract_without_posix_descriptor(present):
    calls = []
    facade = SimpleNamespace(
        require_private_root=lambda: calls.append("private"),
        entry_exists=lambda relative: calls.append(relative) or present,
    )
    backend = object.__new__(FileSecrets)
    backend.files = facade
    if present:
        with pytest.raises(ContextError) as caught:
            backend._check()
        assert caught.value.code == "credential_backend_mismatch"
    else:
        backend._check()
        assert calls[-1] == "private"
    assert calls[:2] == ["private", "collection-system-secrets.json"]


def test_file_credentials_presence_failure_does_not_read_any_key():
    def unknown(_):
        raise ContextError("forbidden_path", "original fixture")

    backend = object.__new__(FileSecrets)
    backend.files = SimpleNamespace(require_private_root=lambda: None, entry_exists=unknown)
    with pytest.raises(ContextError) as caught:
        backend._check()
    assert caught.value.code == "credential_backend_mismatch"


def test_library_size_consumer_uses_facade_not_os_stat():
    calls = []
    facade = SimpleNamespace(file_size=lambda relative: calls.append(relative) or 123)
    management = LibraryManagement(SimpleNamespace(files=facade), authorize=lambda: None)
    assert management._size("original/fixture") == 123
    assert calls == ["original/fixture"]
