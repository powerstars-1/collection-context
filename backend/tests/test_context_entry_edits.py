"""Owner entry-page edits are notes, not parsed platform metadata or executable paths."""

import json
import os

import pytest
from test_context_owner_edits import fails, manager
from test_context_owner_edits import library as library

from collection_context.application.contracts import ContextError
from collection_context.application.library_management import LibraryManagement
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


def change_entry(store, item, *, text=None):
    path = FileIndex.readable_path(item["id"])
    body = (store.files.read(path).decode() + "\n入口笔记：蓝莓独角兽参数390。\n") if text is None else text
    (store.files.root / path).write_bytes(body.encode() if isinstance(body, str) else body)
    return path


def confirm(store, ref):
    owner = manager(store)
    preview = owner.preview_edit(ref, artifact="entry")
    return owner.accept_edit(ref, artifact="entry", preview_token=preview["preview_token"], confirmed=True)


def test_entry_is_readonly_until_confirmed_then_notes_are_searchable_and_originals_unchanged(library):
    store, item = library
    path = change_entry(store, item)
    body = store.files.read(path)
    before = store.snapshot()
    preview = manager(store).preview_edit(item["id"], artifact="entry")
    assert preview["changed"] and preview["effect"] == "append_entry_snapshot_to_user_note"
    assert preview["invalidated_artifacts"] == [] and preview["model_requests"] == 0
    assert store.snapshot() == before
    assert not ContextService(store).search("蓝莓独角兽")["total_matches"]
    fails(
        "confirmation_required",
        lambda: manager(store).accept_edit(
            item["id"], artifact="entry", preview_token=preview["preview_token"], confirmed=False
        ),
    )
    result = confirm(store, item["id"])
    updated = store.get(item["id"])
    assert result["existing_notes_preserved"] and not result["source_metadata_changed"]
    for key in ("title", "body", "source_url", "content_hash", "relations"):
        assert updated[key] == item[key]
    for kind in item["artifacts"].keys() - {"user_note"}:
        assert updated["artifacts"][kind] == item["artifacts"][kind]
    note = ContextService(store).read(item["id"], artifact="user_note")["text"]
    assert note.startswith("原始user_note") and "蓝莓独角兽" in note
    assert (
        updated["artifacts"]["user_note"]["owner_edit"]["previous_version"]
        == item["artifacts"]["user_note"]["version"]
    )
    for key, value in item["artifacts"]["user_note"]["coverage"].items():
        if key not in {"accuracy", "owner_modified", "entry_card_snapshot_sha256"}:
            assert updated["artifacts"]["user_note"]["coverage"][key] == value
    assert "用户备注，不是平台原文" in note
    assert store.files.read(path) == body
    result = ContextService(store).search("蓝莓独角兽")
    assert result["total_matches"] == 1
    assert result["items"][0]["matched_artifacts"] == ["user_note"]
    assert not any(
        gap.get("artifact") == "readable_entry" for gap in FileIndex(store).load(store.snapshot())["gaps"]
    )
    fails("edit_unchanged", lambda: confirm(store, item["id"]))


def test_rebuild_and_backup_restore_preserve_confirmed_entry_without_generated_ownership(library, tmp_path):
    store, item = library
    path = change_entry(store, item)
    original = store.files.read(path)
    confirm(store, item["id"])
    FileIndex(store).rebuild(generated_entries={})
    assert store.files.read(path) == original and path not in FileIndex(store).generated_entries()
    archive = tmp_path / "entry-backup.zip"
    create_backup(store, archive, media_scope="none")
    root = tmp_path / "restored"
    restore_backup(archive, root)
    reopened = LibraryStore(root)
    try:
        assert reopened.files.read(path) == original
        assert ContextService(reopened).search("蓝莓独角兽")["total_matches"] == 1
        FileIndex(reopened).rebuild()
        assert reopened.files.read(path) == original
    finally:
        reopened.close()


def test_subsequent_edit_requires_another_confirmation_and_appends_without_destroying_notes(library):
    store, item = library
    path = change_entry(store, item)
    confirm(store, item["id"])
    prior = ContextService(store).read(item["id"], artifact="user_note")["text"]
    change_entry(store, item, text="第二次用户补充：柠檬插件。")
    FileIndex(store).rebuild()
    assert not ContextService(store).search("柠檬插件")["total_matches"]
    assert any(
        gap.get("code") in {"readable_entry_untracked", "readable_entry_user_modified"}
        for gap in FileIndex(store).load(store.snapshot())["gaps"]
    )
    assert store.files.read(path).decode() == "第二次用户补充：柠檬插件。"
    confirm(store, item["id"])
    assert ContextService(store).read(item["id"], artifact="user_note")["text"].startswith(prior)
    assert ContextService(store).search("柠檬插件")["total_matches"] == 1


def test_reverting_to_an_old_generated_page_does_not_restore_machine_ownership(library):
    store, item = library
    path = FileIndex.readable_path(item["id"])
    old_generated = store.files.read(path)
    change_entry(store, item)
    confirm(store, item["id"])
    assert path not in FileIndex(store).generated_entries()
    (store.files.root / path).write_bytes(old_generated)
    # Source changes may update the application view but cannot overwrite any
    # subsequently edited owner entry, even if it equals an old machine version.
    store.upsert(
        {"native_id": "911", "title": "更新的平台标题", "body": "新的平台正文"},
        kind="saved",
        scope_id="s_saved",
    )
    assert store.files.read(path) == old_generated


@pytest.mark.parametrize(
    "body", [b"\xff", b"", b"\n ", b"x" * 500001], ids=["non_utf8", "empty", "blank", "oversized"]
)
def test_invalid_entry_text_never_creates_a_note(library, body):
    store, item = library
    change_entry(store, item, text=body)
    before = store.snapshot()
    fails("invalid_artifact", lambda: manager(store).preview_edit(item["id"], artifact="entry"))
    assert store.snapshot() == before


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_entry_links_cannot_read_external_content(library, tmp_path, mode):
    store, item = library
    path = store.files.root / FileIndex.readable_path(item["id"])
    external = tmp_path / "synthetic-outside.md"
    external.write_text("never-read-external-synthetic", encoding="utf-8")
    path.unlink()
    if mode == "symlink":
        path.symlink_to(external)
    else:
        os.link(external, path)
    fails("forbidden_path", lambda: manager(store).preview_edit(item["id"], artifact="entry"))


def test_changed_preview_and_pending_jobs_reject(library):
    store, item = library
    change_entry(store, item)
    preview = manager(store).preview_edit(item["id"], artifact="entry")
    change_entry(store, item, text="预览之后再次变化")
    fails(
        "version_changed",
        lambda: manager(store).accept_edit(
            item["id"], artifact="entry", preview_token=preview["preview_token"], confirmed=True
        ),
    )
    JobManager(store).submit("sync", {"scope": "test"}, idempotency_key="pending")
    fails("library_busy", lambda: confirm(store, item["id"]))


@pytest.mark.parametrize("fault", ["changed", "revoked", "publish_failed"])
def test_entry_commit_boundary_never_uses_stale_preview_or_revoked_owner(library, monkeypatch, fault):
    store, item = library
    path = change_entry(store, item)
    preview = manager(store).preview_edit(item["id"], artifact="entry")
    before = store.snapshot()
    active = True
    original = store.files.write

    def write(name, body, **kwargs):
        nonlocal active
        if name.startswith(".context/提交/") and not name.endswith("CURRENT.json"):
            if fault == "changed":
                change_entry(store, item, text="提交前变化")
            elif fault == "revoked":
                active = False
        if fault == "publish_failed" and name == ".context/提交/CURRENT.json":
            raise ContextError("write_conflict", "isolated publication failure")
        return original(name, body, **kwargs)

    def authorize():
        if not active:
            raise ContextError("permission_denied", "isolated revocation")

    monkeypatch.setattr(store.files, "write", write)
    fails(
        {"changed": "version_changed", "revoked": "permission_denied", "publish_failed": "write_conflict"}[
            fault
        ],
        lambda: LibraryManagement(store, authorize=authorize).accept_edit(
            item["id"], artifact="entry", preview_token=preview["preview_token"], confirmed=True
        ),
    )
    assert store.snapshot() == before
    assert (
        "提交前变化" in store.files.read(path).decode()
        if fault == "changed"
        else "蓝莓独角兽" in store.files.read(path).decode()
    )


def test_changed_existing_note_cannot_be_overwritten_or_appended(library):
    store, item = library
    change_entry(store, item)
    (store.files.root / item["artifacts"]["user_note"]["path"]).write_text(
        "尚未确认的外部备注修改", encoding="utf-8"
    )
    fails("artifact_changed", lambda: manager(store).preview_edit(item["id"], artifact="entry"))


def test_entry_cli_preview_and_confirmation(library, capsys):
    store, item = library
    change_entry(store, item)
    argv = ["--workspace", str(store.files.root), "accept-edit", "--ref", item["id"], "--artifact", "entry"]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)["data"]
    assert main([*argv, "--preview-token", preview["preview_token"], "--confirm-edit"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["model_requests"] == 0
    assert ContextService(store).search("蓝莓独角兽")["total_matches"] == 1
