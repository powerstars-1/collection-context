from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.application.service import ContextService
from collection_context.interfaces.access import ACCESS_FILE, AccessRegistry
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.backup_cli import main
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path / "原始资料库")
    yield value
    value.close()


def add(store, native="123"):
    return store.upsert(
        {"native_id": native, "title": "备份 UI 教程", "body": "原创合成资料", "author": "作者"},
        kind="saved",
        scope_id="s_saved",
    )["item"]


def fails(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def register_media(store, item):
    data = b"synthetic-media"
    sha = hashlib.sha256(data).hexdigest()
    blob_path = f"content-vault/80_附件/抖音/{item['id']}/输入/{sha}.mp4"
    store.files.write(blob_path, data)
    payload = {
        "material_ref": item["id"],
        "originals": [
            {"path": blob_path, "sha256": sha, "mime_type": "video/mp4", "bytes": len(data)}
        ],
        "audio": [],
        "frames": [],
    }
    body = canonical_bytes(payload)
    identity = "u_" + hashlib.sha256(body).hexdigest()
    manifest_path = f".context/输入/{identity}.json"
    store.files.write(manifest_path, body)

    def save(state):
        state.setdefault("prepared_inputs", {})[identity] = {
            "path": manifest_path,
            "sha256": hashlib.sha256(body).hexdigest(),
            "material_ref": item["id"],
        }
        state["items"][item["id"]]["prepared_input"] = identity
        state["items"][item["id"]]["media_preparation"] = {
            "input_hash": item["content_hash"],
            "kind": "video",
            "has_audio": False,
        }

    store.transact(save)
    return blob_path, data


def test_content_commits_maintain_index_and_readable_entry_without_read_writes(store):
    item = add(store)
    store.save_artifact(
        item["id"],
        "screen",
        "Tailwind 1024",
        processor_version="test",
        expected_content_hash=item["content_hash"],
    )
    pointer_before = store.files.read(".context/索引/CURRENT.json")
    generation_before = store.snapshot()["generation"]
    result = ContextService(store).search("Tailwind")
    assert result["items"][0]["material_ref"] == item["id"]
    ContextService(store).read(item["id"], artifact="screen")
    assert store.files.read(".context/索引/CURRENT.json") == pointer_before
    assert store.snapshot()["generation"] == generation_before
    readable = store.files.read(FileIndex.readable_path(item["id"])).decode()
    assert item["id"] in readable and "本页由收藏上下文后端" in readable


def test_index_failure_is_explicit_after_content_commit(store, monkeypatch):
    original = FileIndex.rebuild_committed

    def fail_index(self, state, owner):
        raise ContextError("index_limit", "fixture")

    monkeypatch.setattr(FileIndex, "rebuild_committed", fail_index)
    with pytest.raises(ContextError) as caught:
        add(store)
    assert caught.value.code == "index_maintenance_failed"
    ref = next(iter(store.snapshot()["items"]))
    assert store.get(ref)["title"] == "备份 UI 教程"
    monkeypatch.setattr(FileIndex, "rebuild_committed", original)
    fails("index_outdated", lambda: ContextService(store).search("UI"))
    FileIndex(store).rebuild()
    assert ContextService(store).search("UI")["total_matches"] == 1


def test_full_backup_restore_preserves_content_media_jobs_and_excludes_secrets(store, tmp_path):
    item = add(store)
    store.save_artifact(
        item["id"],
        "screen",
        "可恢复证据",
        processor_version="test",
        expected_content_hash=item["content_hash"],
    )
    blob_path, media = register_media(store, item)
    job = JobManager(store).submit("process", {"input_id": "u_fixture"}, idempotency_key="backup", max_calls=1)
    AccessRegistry(store).create("本地访问")
    store.files.write(".context/暂存/不应备份.txt", b"temporary")
    archive = tmp_path / "library.zip"
    result = create_backup(store, archive, media_scope="all")
    assert result["omitted_media"] == 0 and result["file_count"] >= 4
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    assert FILES_PREFIX + ACCESS_FILE not in names
    assert not any("暂存" in name or "索引" in name for name in names)
    destination = tmp_path / "新路径恢复"
    restored = restore_backup(archive, destination)
    assert restored["workspace_id"] == store.workspace_id
    reopened = LibraryStore(destination)
    try:
        state = reopened.snapshot()
        assert state["settings"]["auto_sync"] is False
        assert state["settings"]["auto_process"] is False
        assert state["jobs"][job["id"]]["state"] == "queued"
        assert reopened.files.read(blob_path) == media
        assert ContextService(reopened).search("备份 UI")["total_matches"] == 1
        fails("not_found", lambda: reopened.files.read(ACCESS_FILE))
    finally:
        reopened.close()


def test_text_backup_lists_omitted_media_and_blocks_unfinished_processing(store, tmp_path):
    item = add(store)
    blob_path, _ = register_media(store, item)
    job = JobManager(store).submit("process", {"input_id": "u_fixture"}, idempotency_key="text", max_calls=1)
    archive = tmp_path / "text-only.zip"
    created = create_backup(store, archive, media_scope="none")
    assert created["omitted_media"] == 1
    destination = tmp_path / "text-restore"
    restored = restore_backup(archive, destination)
    assert restored["omitted_media"] == 1
    reopened = LibraryStore(destination)
    try:
        state = reopened.snapshot()
        assert "prepared_inputs" not in state
        assert state["jobs"][job["id"]]["state"] == "blocked"
        with pytest.raises(ContextError) as caught:
            reopened.files.read(blob_path)
        assert caught.value.code in {"not_found", "forbidden_path"}
        assert ContextService(reopened).search("UI")["total_matches"] == 1
    finally:
        reopened.close()


def test_backup_requires_automatic_tasks_paused(store, tmp_path):
    store.transact(lambda state: state["settings"].update(auto_sync=True))
    output = tmp_path / "must-not-exist.zip"
    fails("backup_requires_pause", lambda: create_backup(store, output, media_scope="all"))
    assert not output.exists()


def test_corrupt_or_traversal_archive_leaves_empty_destination_untouched(tmp_path):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escape", b"bad")
        package.writestr("backup-manifest.json", b"{}")
        package.writestr("library-state.json", b"{}")
    destination = tmp_path / "empty"
    destination.mkdir()
    fails("backup_invalid", lambda: restore_backup(archive, destination))
    assert destination.is_dir() and not list(destination.iterdir())
    assert not (tmp_path / "escape").exists()


def test_self_consistent_archive_cannot_inject_unreferenced_secret_file(store, tmp_path):
    add(store)
    source = tmp_path / "source.zip"
    create_backup(store, source, media_scope="all")
    with zipfile.ZipFile(source) as package:
        members = {name: package.read(name) for name in package.namelist()}
    secret = b"must-not-restore"
    manifest = json.loads(members["backup-manifest.json"])
    manifest["files"].append(
        {
            "path": ".context/访问规则.json",
            "bytes": len(secret),
            "sha256": hashlib.sha256(secret).hexdigest(),
        }
    )
    manifest["file_count"] += 1
    manifest["total_bytes"] += len(secret)
    malicious = tmp_path / "injected.zip"
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_STORED) as package:
        for name, body in members.items():
            package.writestr(
                name,
                canonical_bytes(manifest) if name == "backup-manifest.json" else body,
            )
        package.writestr("files/.context/访问规则.json", secret)
    destination = tmp_path / "rejected"
    fails("backup_invalid", lambda: restore_backup(malicious, destination))
    assert not destination.exists()


def test_restore_never_overwrites_existing_library_or_file(store, tmp_path):
    archive = tmp_path / "safe.zip"
    create_backup(store, archive, media_scope="all")
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    fails("restore_destination_not_empty", lambda: restore_backup(archive, destination))
    assert marker.read_text(encoding="utf-8") == "keep"


def test_independent_backup_cli_round_trip(store, tmp_path, capsys):
    add(store)
    archive = tmp_path / "cli.zip"
    assert (
        main(
            [
                "backup",
                "--workspace",
                str(store.files.root),
                "--output",
                str(archive),
                "--media",
                "all",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"]
    destination = tmp_path / "cli-restore"
    assert main(["restore", "--archive", str(archive), "--destination", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["auto_process"] is False


# Kept local to this test module so archive member checks remain explicit.
FILES_PREFIX = "files/"
