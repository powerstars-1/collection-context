"""Original retained-media reuse: source binding, all-blob checks, no hidden download fallback."""

import json
from types import SimpleNamespace

import pytest
from test_context_media import PNG
from test_context_processing_stages import audio, frame
from test_context_sources import raw_item

from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.douyin import normalize_item, source_asset_identity
from collection_context.workflows.ingestion import IngestionWorkflow


def picture(*urls, body="原创媒体样例"):
    return normalize_item(raw_item(desc=body, images=[{"url_list": [url]} for url in urls]))


@pytest.fixture
def library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "原创媒体复用库")
    try:
        yield store
    finally:
        store.close()


def workflow(store):
    calls = []

    def fetch(observed):
        calls.append("download")
        return [(PNG, "image/png") for asset in observed.assets]

    return IngestionWorkflow(store, SimpleNamespace(), SimpleNamespace(fetch=fetch)), calls


def test_unchanged_media_reuses_across_sources_without_download_or_prepare(library, monkeypatch):
    ingest, calls = workflow(library)
    observed = picture("https://fixture.byteimg.com/a", "https://fixture.byteimg.com/b")
    first = ingest.import_item(observed, kind="saved", scope_id="s_saved", download=True)
    monkeypatch.setattr(
        PreparedInputs,
        "prepare_images",
        lambda *a, **k: pytest.fail("Must not decode/reprepare cached media"),
    )
    second = ingest.import_item(observed, kind="liked", scope_id="s_liked", download=True)
    assert first["download_reused"] is False and second["download_reused"] is True
    assert first["input_id"] == second["input_id"] and calls == ["download"]
    assert len(library.get(first["material_ref"])["relations"]) == 2
    assert not library.snapshot()["jobs"] and second["model_requests"] == 0


def test_rotating_signed_queries_are_not_content_changes_or_persisted(library):
    ingest, calls = workflow(library)
    first = picture("https://fixture.byteimg.com/a?signature=one-never-export")
    second = picture("https://fixture.byteimg.com/a?signature=two-never-export")
    assert source_asset_identity(first) == source_asset_identity(second)
    ingest.import_item(first, kind="saved", scope_id="s_saved", download=True)
    result = ingest.import_item(second, kind="saved", scope_id="s_saved", download=True)
    assert result["download_reused"] and calls == ["download"]
    assert "never-export" not in json.dumps(library.snapshot())


def test_resource_change_invalidates_old_evidence_even_without_download(library):
    ingest, calls = workflow(library)
    first = ingest.import_item(
        picture("https://fixture.byteimg.com/a"), kind="saved", scope_id="s_saved", download=True
    )
    material = library.get(first["material_ref"])
    library.save_artifact(
        material["id"],
        "screen",
        "原来页面",
        processor_version="fixture_v1",
        expected_content_hash=material["content_hash"],
    )
    second = ingest.import_item(
        picture("https://fixture.byteimg.com/replaced"), kind="saved", scope_id="s_saved"
    )
    assert not second["content_changed"] and second["source_assets_changed"]
    current = library.get(material["id"])
    assert not current.get("prepared_input") and current["artifacts"]["screen"]["state"] == "stale"
    assert calls == ["download"]


def test_caption_change_does_not_reuse_old_bound_snapshot(library):
    ingest, calls = workflow(library)
    first = ingest.import_item(
        picture("https://fixture.byteimg.com/a"), kind="saved", scope_id="s_saved", download=True
    )
    second = ingest.import_item(
        picture("https://fixture.byteimg.com/a", body="改过的说明"),
        kind="saved",
        scope_id="s_saved",
        download=True,
    )
    assert second["content_changed"] and not second["download_reused"]
    assert first["input_id"] != second["input_id"] and len(calls) == 2


@pytest.mark.parametrize("part", ["originals", "frames", "audio"])
def test_any_corrupt_retained_blob_blocks_reuse_without_download_fallback(library, part):
    observed = normalize_item(raw_item())
    ingest = IngestionWorkflow(
        library,
        SimpleNamespace(),
        SimpleNamespace(fetch=lambda *a: pytest.fail("No fallback download for corruption")),
    )
    initial = ingest.import_item(observed, kind="saved", scope_id="s_saved")
    material = library.get(initial["material_ref"])
    registry = PreparedInputs(library)
    identity = registry.save(
        material["id"],
        content_hash=material["content_hash"],
        originals=[(b"original-fixture-video", "video/mp4")],
        audio=[audio()],
        frames=[frame()],
        coverage={"has_audio": True},
        processor_version="local_media_v3",
        strategy_hash="fixture-strategy",
        kind="video",
    )
    registry.bind_source(material["id"], identity, source_asset_identity(observed), material["content_hash"])
    payload = registry.load(identity)
    blob = payload[part][0] if part == "originals" else payload[part][0]["blob"]
    library.files.write(blob["path"], b"changed", replace=True)
    result = ingest.import_item(observed, kind="saved", scope_id="s_saved", download=True)
    assert result["download"] == "blocked" and result["error"]["code"] == "media_changed"
    assert not library.snapshot()["jobs"]


def test_unbound_manual_input_is_not_silently_classified_as_downloaded_source(library):
    ingest, calls = workflow(library)
    observed = picture("https://fixture.byteimg.com/a")
    initial = ingest.import_item(observed, kind="saved", scope_id="s_saved")
    PreparedInputs(library).prepare_images(initial["material_ref"], [(PNG, "image/png")])
    result = ingest.import_item(observed, kind="saved", scope_id="s_saved", download=True)
    assert not result["download_reused"] and calls == ["download"]


def test_page_order_or_count_change_forces_new_input(library):
    ingest, calls = workflow(library)
    first = ingest.import_item(
        picture("https://fixture.byteimg.com/a"), kind="saved", scope_id="s_saved", download=True
    )
    second = ingest.import_item(
        picture("https://fixture.byteimg.com/a", "https://fixture.byteimg.com/b"),
        kind="saved",
        scope_id="s_saved",
        download=True,
    )
    assert second["source_assets_changed"] and not second["download_reused"]
    assert first["input_id"] != second["input_id"] and len(calls) == 2
