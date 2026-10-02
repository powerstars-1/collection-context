from __future__ import annotations

import io
import json
import zipfile

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.library_management import LibraryManagement
from collection_context.application.service import ContextService
from collection_context.library.store import LEGACY_GUARD, LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def store(tmp_path):
    result = LibraryStore.initialize(tmp_path / "中文 资料库")
    yield result
    result.close()


def add(store, native="123"):
    item = store.upsert(
        {"native_id": native, "title": "管理合成教程", "body": "可读原文", "author": "演示作者"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "画面示例",
        processor_version="fixture",
        expected_content_hash=item["content_hash"],
    )
    return store.get(item["id"])


def manager(store):
    return LibraryManagement(store, authorize=lambda: None)


def fails(code, action):
    with pytest.raises(ContextError) as error:
        action()
    assert error.value.code == code


def test_exclusion_preview_confirmation_restore_and_immediate_index(store):
    item = add(store)
    service = manager(store)
    generation = store.snapshot()["generation"]
    preview = service.preview_exclusion(item["id"], excluded=True)
    assert store.snapshot()["generation"] == generation
    fails(
        "confirmation_required",
        lambda: service.set_exclusion(
            item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=False
        ),
    )
    service.set_exclusion(item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True)
    read = ContextService(store)
    assert read.search("教程")["total_matches"] == 0
    fails("not_found", lambda: read.read(item["id"], artifact="screen"))
    assert store.files.read(item["artifacts"]["screen"]["path"])
    assert service.excluded_items()["items"][0]["material_ref"] == item["id"]
    preview = service.preview_exclusion(item["id"], excluded=False)
    service.set_exclusion(item["id"], excluded=False, preview_token=preview["preview_token"], confirmed=True)
    assert read.search("教程")["total_matches"] == 1
    assert read.read(item["id"], artifact="screen")["text"] == "画面示例"


def test_version_change_and_token_replay_rejected(store):
    item = add(store)
    service = manager(store)
    preview = service.preview_exclusion(item["id"], excluded=True)
    add(store, "124")
    fails(
        "version_changed",
        lambda: service.set_exclusion(
            item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True
        ),
    )
    preview = service.preview_exclusion(item["id"], excluded=True)
    service.set_exclusion(item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True)
    fails(
        "version_changed",
        lambda: service.set_exclusion(
            item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True
        ),
    )


def test_pending_jobs_block_exclusion_without_cancelling_or_model_request(store):
    item = add(store)
    job = JobManager(store).submit("sync", {"synthetic": True}, idempotency_key="busy")
    service = manager(store)
    preview = service.preview_exclusion(item["id"], excluded=True)
    assert preview["pending_jobs"] == 1
    fails(
        "library_busy",
        lambda: service.set_exclusion(
            item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True
        ),
    )
    assert store.get(item["id"]) and JobManager(store).get(job["id"])["state"] == "queued"


def test_authorization_is_required_and_revocation_during_export_is_checked(store):
    item = add(store)

    def deny():
        raise ContextError("permission_denied", "fixture")

    service = LibraryManagement(store, authorize=deny)
    for action in (
        service.overview,
        service.excluded_items,
        lambda: service.preview_exclusion(item["id"], excluded=True),
        lambda: service.export_preview(item["id"]),
    ):
        fails("permission_denied", action)
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls > 1:
            deny()

    fails("permission_denied", lambda: LibraryManagement(store, authorize=revoke).export_preview(item["id"]))


def test_storage_is_metadata_only_and_credentials_are_out_of_scope(store, monkeypatch):
    item = add(store)
    store.files.write(".context/凭据.json", b"not-to-export")
    original = store.files.read

    def read(path, **kwargs):
        assert path != item["artifacts"]["screen"]["path"]
        return original(path, **kwargs)

    monkeypatch.setattr(store.files, "read", read)
    result = manager(store).overview()
    assert result["storage"]["text_bytes"] == len("画面示例".encode())
    assert result["storage"]["media_bytes"] == 0
    assert result["storage"]["disk_free_bytes"] > 0
    assert "credentials" in result["storage_excludes"]
    assert result["accuracy"] == "safe_file_sizes_not_content_hash_verification"


def test_export_preview_and_archive_have_no_internal_paths_or_secrets(store):
    item = add(store)
    store.files.write(".context/凭据.json", b"super-secret-fixture")
    service = manager(store)
    preview = service.export_preview(item["id"])
    assert preview["not_a_full_backup"] and not preview["credentials_included"]
    generation = store.snapshot()["generation"]
    body = service.export_archive(
        item["id"], media_scope="none", preview_token=preview["preview_token"], confirmed=True
    )
    assert store.snapshot()["generation"] == generation
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert set(archive.namelist()) == {
            "original.md",
            "material.json",
            "artifacts/screen.md",
            "export-manifest.json",
        }
        assert archive.read("artifacts/screen.md").decode() == "画面示例"
        metadata = json.loads(archive.read("material.json"))
        assert metadata["relations"][0]["kind"] == "saved"
        assert metadata["accuracy"] == "not_verified"
    assert b"super-secret-fixture" not in body and b".context/" not in body
    assert str(store.files.root).encode() not in body


def test_export_stale_missing_modified_and_secret_manifest_fail_closed(store):
    item = add(store)
    service = manager(store)
    preview = service.export_preview(item["id"])
    add(store, "124")
    fails(
        "version_changed",
        lambda: service.export_archive(
            item["id"], media_scope="none", preview_token=preview["preview_token"], confirmed=True
        ),
    )
    store.files.write(item["artifacts"]["screen"]["path"], b"changed", replace=True)
    fails("artifact_changed", lambda: service.export_preview(item["id"]))
    store.files.write(".context/secret.json", b"secret")

    def corrupt(state):
        state["items"][item["id"]]["artifacts"]["screen"]["path"] = ".context/secret.json"

    store.transact(corrupt)
    fails("forbidden_path", lambda: service.export_preview(item["id"]))
    assert service.overview()["integrity_gaps"][0]["code"] == "forbidden_path"


def test_export_blocks_linked_artifact_and_root_replacement(store, tmp_path):
    item = add(store)
    path = store.files.root / item["artifacts"]["screen"]["path"]
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    path.symlink_to(outside)
    fails("forbidden_path", lambda: manager(store).export_preview(item["id"]))
    old = store.files.root
    old.rename(tmp_path / "moved")
    old.mkdir()
    fails("storage_unavailable", manager(store).overview)


def test_management_uses_writer_guard_and_never_accepts_client_path(store):
    item = add(store)
    service = manager(store)
    preview = service.preview_exclusion(item["id"], excluded=True)
    store.files.unlink(LEGACY_GUARD)
    fails(
        "lock_changed",
        lambda: service.set_exclusion(
            item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True
        ),
    )
    fails("lock_changed", lambda: service.export_preview(item["id"]))
    with pytest.raises(TypeError):
        service.export_preview(item["id"], output="/arbitrary/client/path")


def test_typed_arguments_and_excluded_pagination(store):
    item = add(store)
    service = manager(store)
    fails("invalid_argument", lambda: service.preview_exclusion(item["id"], excluded="true"))
    fails("invalid_argument", lambda: service.excluded_items(limit=True))
    fails("version_required", lambda: service.excluded_items(offset=1))
    fails("invalid_argument", lambda: service.export_preview(item["id"], media_scope="private"))
    preview = service.preview_exclusion(item["id"], excluded=True)
    service.set_exclusion(item["id"], excluded=True, preview_token=preview["preview_token"], confirmed=True)
    fails("not_found", lambda: service.export_preview(item["id"]))
    version = service.excluded_items()["version"]
    add(store, "124")
    fails("version_changed", lambda: service.excluded_items(version=version))


def test_media_export_preserves_order_and_counts_unique_blobs_without_paths(store):
    from test_context_media import PNG

    item = store.upsert(
        {"native_id": "678", "title": "合成图文", "media_type": "image"}, kind="saved", scope_id="s_saved"
    )["item"]
    # Original valid raster passes full batch preflight without any model request.
    data = PNG
    identity = PreparedInputs(store).prepare_images(item["id"], [(data, "image/png")] * 2)
    service = manager(store)
    omitted = service.export_preview(item["id"], media_scope="none")
    assert omitted["omitted_media_files"] == 1 and omitted["omitted_media_bytes"] == len(data)
    overview = service.overview()
    assert overview["storage"]["media_bytes"] == len(data)
    preview = service.export_preview(item["id"], media_scope="all")
    archive_body = service.export_archive(
        item["id"], media_scope="all", confirmed=True, preview_token=preview["preview_token"]
    )
    with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
        manifest = json.loads(archive.read("media-evidence.json"))
        assert manifest["input_id"] == identity
        assert len(manifest["originals"]) == 2
        assert [frame["page_index"] for frame in manifest["frames"]] == [0, 1]
        assert archive.read(manifest["originals"][0]["name"]) == data
        assert b"content-vault/" not in archive.read("media-evidence.json")
    blob = PreparedInputs(store).load(identity)["originals"][0]
    store.files.write(blob["path"], b"modified", replace=True)
    fails("media_changed", lambda: service.export_preview(item["id"], media_scope="all"))


def test_export_size_limit_is_explicit_not_silent_omission(store, monkeypatch):
    item = add(store)
    monkeypatch.setattr("collection_context.application.library_management.MAX_EXPORT_BYTES", 1)
    fails("export_size_limit", lambda: manager(store).export_preview(item["id"]))
