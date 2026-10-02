"""Own immutable input snapshots, including image pages; no real platform or cloud quality claim."""

import hashlib

import pytest
from test_context_media import PNG
from test_context_processing_stages import audio, frame

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path / "独立输入库")
    yield value
    value.close()


def item(store, kind="image", body="合成文案"):
    return store.upsert(
        {"native_id": "71", "media_type": kind, "body": body}, kind="saved", scope_id="s_saved"
    )["item"]


def test_ordered_originals_reopen_and_hash_identity_dedupe(store):
    material = item(store)
    inputs = PreparedInputs(store)
    identity = inputs.prepare_images(material["id"], [(PNG, "image/png"), (PNG, "image/png")])
    assert inputs.prepare_images(material["id"], [(PNG, "image/png"), (PNG, "image/png")]) == identity
    reopened = LibraryStore(store.files.root)
    try:
        registry = PreparedInputs(reopened)
        payload = registry.load(identity)
        assert len(payload["originals"]) == 2 and payload["audio"] == []
        pages = registry.frames(payload)
        assert [page.page_index for page in pages] == [0, 1]
        assert [page.candidate.evidence_id for page in pages] == ["f_000000", "f_000001"]
        assert pages[0].data == PNG and payload["originals"][0]["path"] == payload["originals"][1]["path"]
        assert (
            ContextService(reopened).status(material["id"])["artifacts"]["audio"]["state"] == "not_applicable"
        )
        with pytest.raises(ContextError) as caught:
            ContextService(reopened).read(material["id"], artifact="audio")
        assert caught.value.code == "artifact_not_applicable"
    finally:
        reopened.close()


def test_registered_blob_change_never_returns_old_input(store):
    material = item(store)
    inputs = PreparedInputs(store)
    identity = inputs.prepare_images(material["id"], [(PNG, "image/png")])
    blob = inputs.load(identity)["originals"][0]
    store.files.write(blob["path"], b"modified", replace=True)
    with pytest.raises(ContextError) as caught:
        inputs.load(identity)
    assert caught.value.code == "media_changed"
    with pytest.raises(ContextError):
        inputs.prepare_images(material["id"], [(PNG, "image/png")])


def test_manifest_change_and_pointer_escape_are_rejected(store):
    material = item(store)
    inputs = PreparedInputs(store)
    identity = inputs.prepare_images(material["id"], [(PNG, "image/png")])
    path = store.snapshot()["prepared_inputs"][identity]["path"]
    store.files.write(path, b"{}", replace=True)
    with pytest.raises(ContextError) as caught:
        inputs.load(identity)
    assert caught.value.code == "media_changed"
    store.transact(lambda state: state["prepared_inputs"][identity].update(path="context-workspace.json"))
    with pytest.raises(ContextError) as caught:
        inputs.load(identity)
    assert caught.value.code == "forbidden_path"


def test_uncommitted_manifest_is_not_an_input_but_can_be_reregistered(store, monkeypatch):
    material = item(store)
    publish = store._publish
    monkeypatch.setattr(
        store,
        "_publish",
        lambda *_, **__: (_ for _ in ()).throw(ContextError("storage_unavailable", "合成提交失败")),
    )
    with pytest.raises(ContextError):
        PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])
    assert store.snapshot().get("prepared_inputs", {}) == {}
    monkeypatch.setattr(store, "_publish", publish)
    identity = PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])
    assert PreparedInputs(store).load(identity)["material_ref"] == material["id"]


def test_audio_snapshot_roundtrip_keeps_ranges_and_overlap(store):
    material = item(store, "video")
    inputs = PreparedInputs(store)
    identity = inputs.save(
        material["id"],
        content_hash=material["content_hash"],
        originals=[(b"synthetic manifest fixture; not a decoded video", "video/mp4")],
        audio=[audio()],
        frames=[frame()],
        coverage={"complete": False},
        processor_version="fixture",
        strategy_hash="fixture",
        kind="video",
    )
    restored = inputs.audio(inputs.load(identity))[0]
    assert restored == audio() and restored.sha256 == hashlib.sha256(restored.data).hexdigest()


def test_preparation_version_or_exclusion_change_refuses_registration(store):
    material = item(store)
    newer = item(store, body="文案已变")
    registry = PreparedInputs(store)
    with pytest.raises(ContextError) as caught:
        registry.save(
            material["id"],
            content_hash=material["content_hash"],
            originals=[(PNG, "image/png")],
            audio=[],
            frames=[frame()],
            coverage={},
            processor_version="fixture",
            strategy_hash="fixture",
            kind="image",
        )
    assert caught.value.code == "version_changed"
    store.exclude(newer["id"])
    with pytest.raises(ContextError) as caught:
        registry.prepare_images(newer["id"], [(PNG, "image/png")])
    assert caught.value.code == "not_found"


def test_total_size_and_page_limits_block_without_truncation(store, monkeypatch):
    material = item(store)
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")] * 241)
    assert caught.value.code == "media_input_limit"
    monkeypatch.setattr("collection_context.processing.inputs.MAX_TOTAL", 1)
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])
    assert caught.value.code == "media_input_limit"
    assert store.snapshot().get("prepared_inputs", {}) == {}


def test_missing_audio_cannot_be_mislabeled_as_no_audio(store):
    material = item(store, "video")
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store).save(
            material["id"],
            content_hash=material["content_hash"],
            originals=[(b"synthetic-video-fixture", "video/mp4")],
            audio=[],
            frames=[frame()],
            coverage={"complete": False},
            processor_version="fixture",
            strategy_hash="fixture",
            kind="video",
        )
    assert caught.value.code == "audio_presence_unknown"
    assert store.snapshot().get("prepared_inputs", {}) == {}


@pytest.mark.parametrize("bad", [PNG[:-1], PNG[:-1] + b"x", b"\xff\xd8\xffbad", b"not an image"])
def test_bad_later_original_refuses_entire_batch_before_any_blob_write(store, monkeypatch, bad):
    material = item(store)
    before = store.snapshot()
    monkeypatch.setattr(store.files, "write", lambda *a, **k: pytest.fail("No partial image batch write"))
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png"), (bad, "image/png")])
    assert caught.value.code in {"media_image_invalid", "unsupported_media"}
    assert store.snapshot() == before


def test_old_broken_image_registry_cannot_create_paid_stages(store, monkeypatch):
    from test_context_model_registry import configure, requests

    from collection_context.workflows.extraction import ExtractionWorkflow

    material = item(store)
    registry = PreparedInputs(store)
    # Simulate an immutable pre-validation registry, not a current production bypass.
    with monkeypatch.context() as historical:
        historical.setattr("collection_context.processing.inputs.validate_raster", lambda *a, **k: None)
        identity = registry.prepare_images(material["id"], [(PNG[:-1], "image/png")])
    configure(store, "k_" + "0" * 32, "vision", "original-vision")
    configure(store, "k_" + "0" * 32, "summary", "original-summary")
    sent = requests(monkeypatch)
    with pytest.raises(ContextError) as caught:
        ExtractionWorkflow(store, lambda _: "synthetic-not-a-real-key").submit(
            identity, idempotency_key="old-invalid-media", max_calls=2
        )
    assert caught.value.code == "media_image_invalid"
    assert sent == [] and store.snapshot()["jobs"] == {}


def test_exact_reappearance_reduces_real_workflow_transport_without_losing_brief_code_page(
    store, monkeypatch
):
    import io

    from PIL import Image, ImageDraw
    from test_context_model_registry import configure, requests
    from test_context_ocr_selection import page, select

    from collection_context.workflows.extraction import ExtractionWorkflow

    def code_page(text):
        out = io.BytesIO()
        canvas = Image.new("RGB", (400, 80), "white")
        ImageDraw.Draw(canvas).text((10, 10), text, fill="black")
        canvas.save(out, format="PNG")
        return out.getvalue()

    first = code_page("Original prompt: width=390")
    brief = code_page("Original prompt: width=390; mode=strict")
    new_character = code_page("Original prompt: width=391; mode=strict")
    selector = select()
    # These selected samples model 0.1-second pages after 20 seconds. This test
    # proves dedup/transport preservation, not real-video temporal recall.
    for candidate in (
        page(0, data=first, seconds=0, reasons=("first",)),
        page(201, data=brief, seconds=20.1),
        page(202, data=first, seconds=20.2),
        page(203, data=new_character, seconds=20.3),
        page(210, data=first, seconds=21, reasons=("last",)),
    ):
        selector.feed(candidate)
    material = item(store, "video")
    registry = PreparedInputs(store)
    identity = registry.save(
        material["id"],
        content_hash=material["content_hash"],
        originals=[(b"original protocol fixture, not decoded video", "video/mp4")],
        audio=[],
        frames=selector.frames,
        coverage={**selector.coverage(), "has_audio": False},
        processor_version="original-offline-test",
        strategy_hash="original-offline-test",
        kind="video",
    )
    configure(store, "k_" + "0" * 32, "vision", "original-vision")
    configure(store, "k_" + "0" * 32, "summary", "original-summary")
    sent = requests(monkeypatch)
    workflow = ExtractionWorkflow(store, lambda _: "synthetic-not-a-real-key")
    job = workflow.submit(identity, idempotency_key="reappearing-pages", max_calls=5)
    done = workflow.run(job["id"])
    assert done["state"] == "succeeded"
    assert [call["model"] for call in sent] == ["original-vision"] * 4 + ["original-summary"]
    assert "duplicate_occurrences" in str(sent[-1]) and "20.2" in str(sent[-1])
    payload = registry.load(identity)
    assert [entry["candidate"]["evidence_id"] for entry in payload["frames"]] == [
        "f_000000",
        "f_000201",
        "f_000203",
        "f_000210",
    ]
    assert len(selector.engine.calls) == 3
    assert payload["coverage"]["ocr_records"][2]["duplicate_of"] == "f_000000"
    assert payload["coverage"]["ocr_records"][2]["nominal_seconds"] == 20.2
    assert [entry["nominal_seconds"] for entry in payload["coverage"]["ocr_records"]] == [
        0,
        20.1,
        20.2,
        20.3,
        21,
    ]
