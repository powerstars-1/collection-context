"""Durable derived indexes without republishing unchanged or user-owned notes."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from collection_context.application.service import ContextService
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


@pytest.fixture
def library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "原创隔离库")
    try:
        yield store
    finally:
        store.close()


def add(store, native, **changes):
    return store.upsert(
        {"native_id": native, "title": "合成教程 " + native, "body": "原创资料", **changes},
        kind="saved",
        scope_id="s_saved",
    )["item"]


def observe_entries(store, monkeypatch):
    published = []
    actual = store.files.write

    def observe(relative, body, **kwargs):
        if relative.startswith("content-vault/00_素材收件箱/抖音/"):
            published.append(relative)
        return actual(relative, body, **kwargs)

    monkeypatch.setattr(store.files, "write", observe)
    return published


def index_body(store):
    pointer = json.loads(store.files.read(".context/索引/CURRENT.json"))
    body = store.files.read(".context/索引/" + pointer["version"] + ".json")
    assert hashlib.sha256(body).hexdigest() == pointer["sha256"]
    return json.loads(body)


def test_explicit_rebuild_keeps_generated_note_inode_and_mtime(library, monkeypatch):
    item = add(library, "1")
    relative = FileIndex.readable_path(item["id"])
    path = library.files.root / relative
    before = path.stat()
    content = path.read_bytes()
    calls = observe_entries(library, monkeypatch)
    previous_pointer = library.files.read(".context/索引/CURRENT.json")
    result = FileIndex(library).rebuild()
    after = path.stat()
    assert calls == []
    assert (before.st_dev, before.st_ino, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
    )
    assert path.read_bytes() == content and result["gaps"] == []
    assert index_body(library)["generated_entries"][relative] == hashlib.sha256(content).hexdigest()
    # The explicit rebuild still publishes its fresh verified derived index.
    assert library.files.read(".context/索引/CURRENT.json") != previous_pointer


def test_one_metadata_update_only_republishes_that_note(library, monkeypatch):
    items = [add(library, str(number)) for number in range(3)]
    originals = {
        item["id"]: (library.files.root / FileIndex.readable_path(item["id"])).stat() for item in items
    }
    calls = observe_entries(library, monkeypatch)
    changed = add(library, "1", title="新关键词 火箭")
    assert calls == [FileIndex.readable_path(changed["id"])]
    for item in (items[0], items[2]):
        after = (library.files.root / FileIndex.readable_path(item["id"])).stat()
        before = originals[item["id"]]
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    result = ContextService(library).search("火箭")
    assert [value["material_ref"] for value in result["items"]] == [changed["id"]]


def test_same_source_refresh_keeps_note_but_updates_index_version(library, monkeypatch):
    item = add(library, "1")
    calls = observe_entries(library, monkeypatch)
    before = library.snapshot()["generation"]
    add(library, "1")
    assert calls == [] and library.snapshot()["generation"] > before
    assert ContextService(library).search("合成教程")["items"][0]["material_ref"] == item["id"]


@pytest.mark.parametrize("untracked", [False, True])
def test_identical_bytes_do_not_erase_untracked_or_user_modified_gap(library, monkeypatch, untracked):
    item = add(library, "1")
    relative = FileIndex.readable_path(item["id"])
    if untracked:
        state = library.snapshot()
        entries = {}  # Missing history cannot claim an existing file as generated.
        expected = "readable_entry_untracked"
    else:
        library.files.write(relative, b"# Hand edited\n", replace=True)
        state = library.snapshot()
        entries = FileIndex(library).generated_entries()
        expected = "readable_entry_user_modified"
    retained = library.files.read(relative)
    calls = observe_entries(library, monkeypatch)
    with library.writer() as owner:
        result = FileIndex(library).rebuild_committed(state, owner, generated_entries=entries)
    assert calls == [] and library.files.read(relative) == retained
    assert {gap["code"] for gap in result["gaps"]} == {expected}


def test_missing_generated_note_is_recreated_and_indexed(library, monkeypatch):
    item = add(library, "1")
    relative = FileIndex.readable_path(item["id"])
    (library.files.root / relative).unlink()
    calls = observe_entries(library, monkeypatch)
    result = FileIndex(library).rebuild()
    assert calls == [relative] and result["gaps"] == []
    assert library.files.read(relative) == FileIndex._readable_markdown(library.get(item["id"]))


def test_model_artifact_update_still_republishes_changed_readable_links(library, monkeypatch):
    item = add(library, "1")
    calls = observe_entries(library, monkeypatch)
    library.save_artifact(
        item["id"],
        "screen",
        "原创画面文字",
        processor_version="fixture",
        expected_content_hash=item["content_hash"],
    )
    assert calls == [FileIndex.readable_path(item["id"])]
    assert "画面文字.md" in library.files.read(calls[0]).decode()
    assert ContextService(library).search("原创画面文字")["total_matches"] == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversized", "directory"])
def test_existing_unreadable_note_is_not_replaced(library, tmp_path, monkeypatch, kind):
    item = add(library, "1")
    relative = FileIndex.readable_path(item["id"])
    path = library.files.root / relative
    path.unlink()
    external = tmp_path / "保留我的文件"
    external.write_bytes(b"external-user-content")
    if kind == "symlink":
        path.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, path)
    elif kind == "oversized":
        with path.open("wb") as stream:
            stream.truncate(2_000_001)
    else:
        path.mkdir()
    before = path.lstat()
    calls = observe_entries(library, monkeypatch)
    result = FileIndex(library).rebuild()
    after = path.lstat()
    assert calls == [] and (before.st_ino, before.st_size, before.st_mode) == (
        after.st_ino,
        after.st_size,
        after.st_mode,
    )
    assert external.read_bytes() == b"external-user-content"
    assert {gap["code"] for gap in result["gaps"]} == {"readable_entry_unreadable"}
    assert ContextService(library).search("合成教程")["total_matches"] == 1


def test_file_appearing_after_absence_is_not_overwritten_or_claimed(library, monkeypatch):
    item = add(library, "1")
    relative = FileIndex.readable_path(item["id"])
    path = library.files.root / relative
    path.unlink()
    actual = library.files.write
    calls = []

    def appears(name, body, **kwargs):
        if name == relative:
            calls.append(kwargs)
            actual(name, b"user-file-arrived")
        return actual(name, body, **kwargs)

    monkeypatch.setattr(library.files, "write", appears)
    result = FileIndex(library).rebuild()
    assert calls == [{}] and path.read_bytes() == b"user-file-arrived"
    assert {gap["code"] for gap in result["gaps"]} == {"readable_entry_write_conflict"}
    assert FileIndex(library).generated_entries()[relative] != hashlib.sha256(path.read_bytes()).hexdigest()


def test_job_only_transaction_does_not_rebuild_or_write_notes(library, monkeypatch):
    add(library, "1")
    pointer = library.files.read(".context/索引/CURRENT.json")
    calls = observe_entries(library, monkeypatch)
    library.transact(lambda state: state["settings"].update({"auto_process": True}))
    assert calls == [] and library.files.read(".context/索引/CURRENT.json") == pointer
