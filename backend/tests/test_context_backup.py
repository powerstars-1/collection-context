from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from collection_context.application.contracts import ContextError, canonical_bytes, digest, item_id
from collection_context.application.service import ContextService
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.interfaces.access import ACCESS_FILE, AccessRegistry
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.backup_cli import main
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.workflows.executor import DurableExecutor, Stage, StageOutcome
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
        "schema_version": 1,
        "material_ref": item["id"],
        "content_hash": item["content_hash"],
        "kind": "video",
        "processor_version": "backup-fixture",
        "strategy_hash": "backup-fixture-v1",
        "coverage": {"has_audio": False, "complete": False},
        "originals": [{"path": blob_path, "sha256": sha, "mime_type": "video/mp4", "bytes": len(data)}],
        "audio": [],
        "frames": [
            {
                "candidate": {
                    "evidence_id": "f_000000",
                    "sample_index": 0,
                    "nominal_seconds": 0,
                    "reasons": ["fixture"],
                    "change_score": 0,
                },
                "page_index": None,
                "blob": {
                    "path": f"content-vault/80_附件/抖音/{item['id']}/输入/{sha}.png",
                    "sha256": sha,
                    "mime_type": "image/png",
                    "bytes": len(data),
                },
            }
        ],
    }
    store.files.write(payload["frames"][0]["blob"]["path"], data)
    body = canonical_bytes(payload)
    identity = "u_" + digest(payload)
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


@pytest.mark.parametrize("media_scope", ["all", "none"])
@pytest.mark.parametrize("paid", [True, False])
def test_backup_restores_confirmed_results_and_reuses_without_a_new_call(store, tmp_path, media_scope, paid):
    invocations = []
    stages = [
        Stage(
            "summary",
            "fixed-input",
            "fixed-prompt",
            lambda _: (
                invocations.append("dispatch")
                or StageOutcome({"text": "已完成的原创结果"}, usage={"total_tokens": 13})
            ),
            paid=paid,
        )
    ]
    executor = DurableExecutor(store)
    original = executor.run(executor.submit(stages, idempotency_key="original", max_calls=1)["id"], stages)
    assert original["state"] == "succeeded" and invocations == ["dispatch"]
    descriptor = original["stages"]["summary"]["result"]
    expected = JobManager(store).read_result(descriptor)
    # An orphan result file is not an authority to widen the backup scope.
    store.files.write(".context/请求结果/x_unreferenced.json", b'{"not_referenced":true}')
    archive = tmp_path / "results.zip"
    create_backup(store, archive, media_scope=media_scope)
    with zipfile.ZipFile(archive) as package:
        assert "files/" + descriptor["path"] in package.namelist()
        assert "files/.context/请求结果/x_unreferenced.json" not in package.namelist()
    destination = tmp_path / "restored-results"
    restore_backup(archive, destination)
    restored = LibraryStore(destination)
    try:
        assert JobManager(restored).read_result(descriptor) == expected
        assert JobManager(restored).get(original["id"])["calls"] == original["calls"]
        resumed = DurableExecutor(restored)
        cached = resumed.run(
            resumed.submit(stages, idempotency_key="after-restore", max_calls=0)["id"], stages
        )
        assert cached["state"] == "succeeded" and cached["calls"] == []
        assert invocations == ["dispatch"]
    finally:
        restored.close()


def test_backup_rejects_damaged_confirmed_result_instead_of_losing_the_cache(store, tmp_path):
    executor = DurableExecutor(store)
    stages = [Stage("audio", "input", "processor", lambda _: StageOutcome({"text": "原创"}), paid=True)]
    job = executor.run(executor.submit(stages, idempotency_key="confirmed", max_calls=1)["id"], stages)
    path = job["stages"]["audio"]["result"]["path"]
    store.files.write(path, b'{"changed":true}', replace=True)
    output = tmp_path / "must-not-publish.zip"
    fails("corrupt_call_result", lambda: create_backup(store, output, media_scope="all"))
    assert not output.exists()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_restore_rejects_lost_result_even_with_updated_archive_manifest(store, tmp_path, damage):
    executor = DurableExecutor(store)
    stages = [Stage("audio", "input", "processor", lambda _: StageOutcome({"text": "原创"}), paid=True)]
    job = executor.run(executor.submit(stages, idempotency_key="confirmed", max_calls=1)["id"], stages)
    source = tmp_path / "source.zip"
    create_backup(store, source, media_scope="none")
    with zipfile.ZipFile(source) as package:
        members = {name: package.read(name) for name in package.namelist()}
    result_member = FILES_PREFIX + job["stages"]["audio"]["result"]["path"]
    if damage == "missing":
        members.pop(result_member)
    else:
        members[result_member] = b'{"changed":true}'
    manifest = json.loads(members["backup-manifest.json"])
    manifest["files"] = [
        {"path": name[len(FILES_PREFIX) :], "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in members.items()
        if name.startswith(FILES_PREFIX)
    ]
    manifest["file_count"] = len(manifest["files"])
    manifest["total_bytes"] = sum(record["bytes"] for record in manifest["files"])
    damaged = tmp_path / "lost-result.zip"
    with zipfile.ZipFile(damaged, "w", compression=zipfile.ZIP_STORED) as package:
        for name, body in members.items():
            package.writestr(name, canonical_bytes(manifest) if name == "backup-manifest.json" else body)
    destination = tmp_path / "not-published"
    fails("backup_invalid", lambda: restore_backup(damaged, destination))
    assert not destination.exists()


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


def test_user_edited_readable_entry_is_preserved_and_reported(store):
    item = add(store)
    path = FileIndex.readable_path(item["id"])
    custom = "# 用户手工版\n\n这段内容不得被同步覆盖。\n".encode()
    store.files.write(path, custom, replace=True)
    store.upsert(
        {"native_id": "123", "title": "备份 UI 教程", "body": "原创合成资料", "author": "作者"},
        kind="liked",
        scope_id="s_likes",
    )
    assert store.files.read(path) == custom
    result = ContextService(store).search("备份 UI")
    assert any(gap["code"] == "readable_entry_user_modified" for gap in result["integrity_gaps"])


def test_untracked_existing_entry_is_never_claimed_or_overwritten(store):
    ref = item_id("douyin", "777")
    path = FileIndex.readable_path(ref)
    custom = "# 迁移前已有手工入口\n".encode()
    store.files.write(path, custom, replace=True)
    item = add(store, native="777")
    assert item["id"] == ref and store.files.read(path) == custom
    result = ContextService(store).search("备份 UI")
    assert any(gap["code"] == "readable_entry_untracked" for gap in result["integrity_gaps"])


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
    job = JobManager(store).submit(
        "process", {"input_id": "u_fixture"}, idempotency_key="backup", max_calls=1
    )
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
        reopened.upsert(
            {
                "native_id": "123",
                "title": "备份 UI 教程",
                "body": "原创合成资料",
                "author": "作者",
            },
            kind="liked",
            scope_id="s_likes",
        )
        assert not any(
            gap["artifact"] == "readable_entry"
            for gap in ContextService(reopened).search("备份 UI")["integrity_gaps"]
        )
        fails("not_found", lambda: reopened.files.read(ACCESS_FILE))
    finally:
        reopened.close()


def test_backup_preserves_real_user_edited_entry_and_declares_gap(store, tmp_path):
    item = add(store)
    path = FileIndex.readable_path(item["id"])
    custom = "# 用户在 Obsidian 中的修订\n\n保留我的文字。\n".encode()
    store.files.write(path, custom, replace=True)
    archive = tmp_path / "edited-entry.zip"
    result = create_backup(store, archive, media_scope="all")
    assert result["readable_entry_gaps"] == [
        {
            "material_ref": item["id"],
            "path": path,
            "code": "readable_entry_user_modified_preserved",
        }
    ]
    with zipfile.ZipFile(archive) as package:
        assert package.read(FILES_PREFIX + path) == custom
    destination = tmp_path / "edited-entry-restore"
    restored = restore_backup(archive, destination)
    assert restored["readable_entry_gaps"] == result["readable_entry_gaps"]
    reopened = LibraryStore(destination)
    try:
        assert reopened.files.read(path) == custom
    finally:
        reopened.close()


def test_text_backup_lists_omitted_media_and_blocks_unfinished_processing(store, tmp_path):
    item = add(store)
    blob_path, _ = register_media(store, item)
    job = JobManager(store).submit("process", {"input_id": "u_fixture"}, idempotency_key="text", max_calls=1)
    archive = tmp_path / "text-only.zip"
    created = create_backup(store, archive, media_scope="none")
    assert created["omitted_media"] == 2
    destination = tmp_path / "text-restore"
    restored = restore_backup(archive, destination)
    assert restored["omitted_media"] == 2
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


def test_backup_refuses_live_executor_even_with_automatic_switches_off(store, tmp_path):
    add(store)
    output = tmp_path / "blocked-by-live-execution.zip"
    with ExecutorLease(store.files.root):
        fails("executor_busy", lambda: create_backup(store, output, media_scope="all"))
    assert not output.exists()
    result = create_backup(store, output, media_scope="all")
    assert result["snapshot_protection"] == "live_executor_and_writer_leases"


def test_backup_executor_lease_is_held_through_collection_and_publication(store, tmp_path, monkeypatch):
    from collection_context.library import backup

    add(store)
    original = backup._collect_files

    def collect(*args, **kwargs):
        fails("executor_busy", lambda: ExecutorLease(store.files.root))
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "_collect_files", collect)
    create_backup(store, tmp_path / "exclusive.zip", media_scope="all")


@pytest.mark.parametrize("pointer", ["artifact", "input_manifest", "media_blob"])
def test_corrupt_library_pointers_cannot_export_credentials(store, tmp_path, pointer, monkeypatch):
    item = add(store)
    store.save_artifact(
        item["id"], "screen", "合成正文", processor_version="test", expected_content_hash=item["content_hash"]
    )
    register_media(store, item)
    secret_path = ".context/访问规则.json"
    secret = b"credential-fixture-never-read"
    store.files.write(secret_path, secret)
    secret_sha = hashlib.sha256(secret).hexdigest()

    def corrupt(state):
        if pointer == "artifact":
            state["items"][item["id"]]["artifacts"]["screen"].update(path=secret_path, sha256=secret_sha)
        else:
            identity, record = next(iter(state["prepared_inputs"].items()))
            if pointer == "input_manifest":
                record.update(path=secret_path, sha256=secret_sha)
            else:
                payload = json.loads(store.files.read(record["path"]))
                payload["originals"][0].update(path=secret_path, sha256=secret_sha, bytes=len(secret))
                body = canonical_bytes(payload)
                new_id = "u_" + digest(payload)
                new_path = f".context/输入/{new_id}.json"
                store.files.write(new_path, body)
                state["prepared_inputs"] = {
                    new_id: {
                        "path": new_path,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "material_ref": item["id"],
                    }
                }
                state["items"][item["id"]]["prepared_input"] = new_id

    store.transact(corrupt)
    original_read = store.files.read

    def read(path, **kwargs):
        assert path != secret_path, "Credential bytes must never be touched by backup"
        return original_read(path, **kwargs)

    monkeypatch.setattr(store.files, "read", read)
    output = tmp_path / "must-not-export.zip"
    fails("forbidden_path", lambda: create_backup(store, output, media_scope="all"))
    assert not output.exists()


def test_backup_checks_media_manifest_identity_and_hash(store, tmp_path):
    item = add(store)
    register_media(store, item)
    state = store.snapshot()
    record = next(iter(state["prepared_inputs"].values()))
    store.files.write(record["path"], b"{}", replace=True)
    fails("media_changed", lambda: create_backup(store, tmp_path / "changed.zip", media_scope="all"))


def test_backup_stops_collection_at_size_bound_and_releases_execution_lease(store, tmp_path, monkeypatch):
    add(store)
    monkeypatch.setattr("collection_context.library.backup.MAX_TOTAL_BYTES", 1)
    output = tmp_path / "too-large.zip"
    fails("backup_size_limit", lambda: create_backup(store, output, media_scope="none"))
    assert not output.exists()
    with ExecutorLease(store.files.root):
        pass


def test_restore_rejects_self_consistent_noncanonical_blob_filename(store, tmp_path):
    item = add(store)
    register_media(store, item)
    original = tmp_path / "original-media.zip"
    create_backup(store, original, media_scope="all")
    with zipfile.ZipFile(original) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    state = json.loads(members["library-state.json"])
    identity, record = next(iter(state["prepared_inputs"].items()))
    payload = json.loads(members[FILES_PREFIX + record["path"]])
    blob = payload["originals"][0]
    old_path = blob["path"]
    blob["path"] = f"content-vault/80_附件/抖音/{item['id']}/输入/not-the-content-hash.mp4"
    members[FILES_PREFIX + blob["path"]] = members.pop(FILES_PREFIX + old_path)
    manifest_body = canonical_bytes(payload)
    new_id = "u_" + digest(payload)
    new_path = f".context/输入/{new_id}.json"
    members.pop(FILES_PREFIX + record["path"])
    members[FILES_PREFIX + new_path] = manifest_body
    state["prepared_inputs"] = {
        new_id: {
            "path": new_path,
            "sha256": hashlib.sha256(manifest_body).hexdigest(),
            "material_ref": item["id"],
        }
    }
    state["items"][item["id"]]["prepared_input"] = new_id
    members["library-state.json"] = canonical_bytes(state)
    manifest = json.loads(members["backup-manifest.json"])
    manifest["state_sha256"] = hashlib.sha256(members["library-state.json"]).hexdigest()
    manifest["files"] = [
        {"path": name[len(FILES_PREFIX) :], "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in members.items()
        if name.startswith(FILES_PREFIX)
    ]
    manifest["file_count"] = len(manifest["files"])
    manifest["total_bytes"] = sum(record["bytes"] for record in manifest["files"])
    members["backup-manifest.json"] = canonical_bytes(manifest)
    malicious = tmp_path / "wrong-fixed-name.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    destination = tmp_path / "rejected-name"
    fails("backup_invalid", lambda: restore_backup(malicious, destination))
    assert not destination.exists()


# Kept local to this test module so archive member checks remain explicit.
FILES_PREFIX = "files/"
