"""Owner-confirmed external text snapshots; no model calls and no original-file overwrite."""

from __future__ import annotations

import hashlib
import json

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.library_management import LibraryManagement
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.index import is_current
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "人工编辑隔离库")
    item = store.upsert(
        {"native_id": "911", "title": "原创教程", "body": "来源正文"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_bundle(
        item["id"],
        {
            kind: {"text": "原始" + kind, "processor_version": "test"}
            for kind in ("original", "screen", "audio", "image", "summary", "readable", "user_note")
        },
        expected_content_hash=item["content_hash"],
    )
    try:
        yield store, store.get(item["id"])
    finally:
        store.close()


def manager(store):
    return LibraryManagement(store, authorize=lambda: None)


def edit(store, item, kind="screen", body="修正关键词：蓝莓线框模板"):
    path = item["artifacts"][kind]["path"]
    # Explicitly emulate the user's external editor, not an application write command.
    (store.files.root / path).write_bytes(body.encode() if isinstance(body, str) else body)
    return path


def accept(store, ref, kind="screen"):
    service = manager(store)
    preview = service.preview_edit(ref, artifact=kind)
    return service.accept_edit(ref, artifact=kind, preview_token=preview["preview_token"], confirmed=True)


def fails(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


@pytest.mark.parametrize(
    "kind,stale",
    [
        ("original", ["readable", "summary"]),
        ("audio", ["readable", "summary"]),
        ("screen", ["readable", "summary"]),
        ("image", ["readable", "summary"]),
        ("summary", ["readable"]),
        ("readable", []),
        ("user_note", []),
    ],
)
def test_confirmed_snapshot_reindexes_invalidates_and_preserves_original(library, kind, stale):
    store, item = library
    path = edit(store, item, kind)
    previous = store.snapshot()
    files = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in store.files.root.rglob("*") if p.is_file()
    }
    service = manager(store)
    preview = service.preview_edit(item["id"], artifact=kind)
    assert preview["changed"] and preview["model_requests"] == 0
    assert preview["invalidated_artifacts"] == stale
    assert not preview["previous_text_available"]
    assert store.snapshot() == previous
    assert files == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in store.files.root.rglob("*") if p.is_file()
    }
    result = service.accept_edit(
        item["id"], artifact=kind, preview_token=preview["preview_token"], confirmed=True
    )
    updated = store.get(item["id"])
    assert result["edited_file_preserved"] and result["model_requests"] == 0
    assert updated["body"] == item["body"] and updated["content_hash"] == item["content_hash"]
    assert updated["artifacts"][kind]["path"] != path
    assert store.files.read(path) == store.files.read(updated["artifacts"][kind]["path"])
    assert updated["artifacts"][kind]["owner_edit"]["previous_sha256"] == item["artifacts"][kind]["sha256"]
    assert all(updated["artifacts"][key]["state"] == "stale" for key in stale)
    assert updated["artifacts"]["user_note"]["state"] == "ready"
    assert is_current(updated, updated["artifacts"][kind])
    assert ContextService(store).read(item["id"], artifact=kind)["text"] == "修正关键词：蓝莓线框模板"
    if kind in {"screen", "audio", "summary", "readable", "image"}:
        assert ContextService(store).search("蓝莓")["total_matches"] == 1
    fails(
        "owner_edit_conflict",
        lambda: store.save_artifact(
            item["id"], kind, "程序覆盖", processor_version="test", expected_content_hash=item["content_hash"]
        ),
    )


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_confirmation_is_strict(library, confirmed):
    store, item = library
    edit(store, item)
    preview = manager(store).preview_edit(item["id"], artifact="screen")
    fails(
        "confirmation_required",
        lambda: manager(store).accept_edit(
            item["id"], artifact="screen", preview_token=preview["preview_token"], confirmed=confirmed
        ),
    )


@pytest.mark.parametrize("body", [b"\xff", b"", b"  \n", b"x" * 500001])
def test_invalid_text_rejected(library, body):
    store, item = library
    edit(store, item, body=body)
    fails("invalid_artifact", lambda: manager(store).preview_edit(item["id"], artifact="screen"))


@pytest.mark.parametrize("change", ["file", "source", "job", "excluded"])
def test_preview_change_rejects_without_committed_mutation(library, change):
    store, item = library
    edit(store, item)
    preview = manager(store).preview_edit(item["id"], artifact="screen")
    if change == "file":
        edit(store, item, body="再次修改")
    elif change == "source":
        store.upsert({"native_id": "911", "title": "新版本"}, kind="saved", scope_id="s_saved")
    elif change == "job":
        JobManager(store).submit("sync", {}, idempotency_key="pending")
    else:
        store.exclude(item["id"])
    before = store.snapshot()
    fails(
        "not_found" if change == "excluded" else "version_changed",
        lambda: manager(store).accept_edit(
            item["id"], artifact="screen", preview_token=preview["preview_token"], confirmed=True
        ),
    )
    assert store.snapshot() == before


def test_unchanged_busy_and_replay_rejected(library):
    store, item = library
    fails("edit_unchanged", lambda: accept(store, item["id"]))
    edit(store, item)
    job = JobManager(store).submit("sync", {}, idempotency_key="pending")
    fails("library_busy", lambda: accept(store, item["id"]))
    JobManager(store).cancel(job["id"])
    preview = manager(store).preview_edit(item["id"], artifact="screen")
    accept(store, item["id"])
    fails(
        "version_changed",
        lambda: manager(store).accept_edit(
            item["id"], artifact="screen", preview_token=preview["preview_token"], confirmed=True
        ),
    )


def test_stale_correction_never_claims_current_input(library):
    store, item = library
    store.upsert({"native_id": "911", "title": "来源已变"}, kind="saved", scope_id="s_saved")
    edit(store, item)
    accept(store, item["id"])
    updated = store.get(item["id"])
    assert updated["artifacts"]["screen"]["input_hash"] == item["content_hash"]
    assert updated["artifacts"]["screen"]["state"] == "stale"
    assert not is_current(updated, updated["artifacts"]["screen"])
    assert ContextService(store).search("蓝莓")["total_matches"] == 0


def test_missing_unknown_path_and_denied_owner_do_not_read_arbitrary_files(library):
    store, item = library
    fails("invalid_artifact", lambda: manager(store).preview_edit(item["id"], artifact="../../secret"))
    store.transact(
        lambda state: state["items"][item["id"]]["artifacts"]["screen"].update(
            path=".context/提交/CURRENT.json"
        )
    )
    fails("forbidden_path", lambda: manager(store).preview_edit(item["id"], artifact="screen"))

    def deny():
        raise ContextError("permission_denied", "拒绝")

    fails(
        "permission_denied",
        lambda: LibraryManagement(store, authorize=deny).preview_edit(item["id"], artifact="screen"),
    )


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "directory", "oversize"])
def test_unsafe_external_file_is_not_accepted(library, tmp_path, unsafe):
    store, item = library
    path = store.files.root / item["artifacts"]["screen"]["path"]
    if unsafe == "oversize":
        path.write_bytes(b"x" * 2_000_001)
    else:
        path.unlink()
        if unsafe == "directory":
            path.mkdir()
        else:
            external = tmp_path / "不可接纳文件"
            external.write_text("外部内容", encoding="utf-8")
            if unsafe == "symlink":
                path.symlink_to(external)
            else:
                import os

                os.link(external, path)
    before = store.snapshot()
    with pytest.raises(ContextError):
        manager(store).preview_edit(item["id"], artifact="screen")
    assert store.snapshot() == before


@pytest.mark.parametrize("fault", ["revoked", "changed", "publish_failed"])
def test_commit_boundary_rechecks_owner_text_and_leaves_old_pointer(library, monkeypatch, fault):
    store, item = library
    path = edit(store, item)
    preview = manager(store).preview_edit(item["id"], artifact="screen")
    before = store.snapshot()
    active = True
    original = store.files.write

    def write(name, body, **kwargs):
        nonlocal active
        if name.startswith(".context/提交/") and not name.endswith("CURRENT.json"):
            if fault == "changed":
                edit(store, item, body="提交前再次修改")
            elif fault == "revoked":
                active = False
        if fault == "publish_failed" and name == ".context/提交/CURRENT.json":
            raise ContextError("write_conflict", "测试失败")
        return original(name, body, **kwargs)

    def authorize():
        if not active:
            raise ContextError("permission_denied", "已撤销")

    monkeypatch.setattr(store.files, "write", write)
    fails(
        {"revoked": "permission_denied", "changed": "version_changed", "publish_failed": "write_conflict"}[
            fault
        ],
        lambda: LibraryManagement(store, authorize=authorize).accept_edit(
            item["id"], artifact="screen", preview_token=preview["preview_token"], confirmed=True
        ),
    )
    assert store.snapshot() == before
    assert store.files.read(path).decode() == (
        "提交前再次修改" if fault == "changed" else "修正关键词：蓝莓线框模板"
    )


def test_backup_and_cli_preserve_manual_correction(library, tmp_path, capsys):
    store, item = library
    edit(store, item)
    argv = ["--workspace", str(store.files.root), "accept-edit", "--ref", item["id"], "--artifact", "screen"]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)["data"]
    assert main([*argv, "--preview-token", preview["preview_token"], "--confirm-edit"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["model_requests"] == 0
    archive = tmp_path / "backup.zip"
    create_backup(store, archive, media_scope="none")
    destination = tmp_path / "恢复库"
    restore_backup(archive, destination)
    with_store = LibraryStore(destination)
    try:
        updated = with_store.get(item["id"])
        assert updated["artifacts"]["screen"]["owner_edit"]
        assert updated["artifacts"]["summary"]["state"] == "stale"
        assert (
            ContextService(with_store).read(item["id"], artifact="screen")["text"]
            == "修正关键词：蓝莓线框模板"
        )
        fails(
            "owner_edit_conflict",
            lambda: with_store.save_artifact(
                item["id"],
                "screen",
                "覆盖",
                processor_version="test",
                expected_content_hash=item["content_hash"],
            ),
        )
    finally:
        with_store.close()
