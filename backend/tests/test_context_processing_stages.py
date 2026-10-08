"""Own role adapters + atomic library handoff; fixtures do not prove cloud model quality."""

import hashlib
import io
import json
import wave
from dataclasses import replace

import pytest
from test_context_media import PNG

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.infrastructure.media import AudioSegment, FrameCandidate, PreparedFrame
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.processing.models import CloudModelClient, ModelProfile
from collection_context.processing.stages import audio_stage, publish_stage, summary_stage, vision_stage
from collection_context.workflows.executor import DurableExecutor


@pytest.fixture
def store(tmp_path):
    library = LibraryStore.initialize(tmp_path / "提取样例库")
    yield library
    library.close()


def item(store):
    return store.upsert(
        {"native_id": "9", "title": "原创合成 UI 教程", "body": "测试资料，不是真实收藏"},
        kind="saved",
        scope_id="s_saved",
    )["item"]


def test_publish_uses_one_automatic_index_maintenance_and_readonly_validation(store, monkeypatch):
    material = item(store)
    rebuilt = []
    maintain = FileIndex.rebuild_committed

    def count(index, state, owner, **kwargs):
        rebuilt.append(state["generation"])
        return maintain(index, state, owner, **kwargs)

    monkeypatch.setattr(FileIndex, "rebuild_committed", count)
    monkeypatch.setattr(
        FileIndex, "rebuild", lambda *a, **kw: pytest.fail("publication rebuilt the index twice")
    )
    stage = publish_stage(store, material["id"], material["content_hash"], ("summary",))
    result = stage.invoke(
        {"summary": {"status": "ready", "output": {"kind": "summary", "text": "原创总结，保留参数390。"}}}
    )
    assert result.status == "ready" and len(rebuilt) == 1
    indexed = FileIndex(store).load(store.snapshot())
    assert result.output["library_version"] == indexed["library_version"]
    assert ContextService(store).search("保留参数390")["total_matches"] == 1
    assert len(rebuilt) == 1


def test_publish_retains_index_integrity_failure_after_content_commit(store, monkeypatch):
    material = item(store)
    maintained = FileIndex.rebuild_committed

    def damage(index, state, owner, **kwargs):
        result = maintained(index, state, owner, **kwargs)
        index.store.files.write(".context/索引/CURRENT.json", b"{}", replace=True)
        return result

    monkeypatch.setattr(FileIndex, "rebuild_committed", damage)
    stage = publish_stage(store, material["id"], material["content_hash"], ("summary",))
    with pytest.raises(ContextError) as caught:
        stage.invoke({"summary": {"status": "ready", "output": {"kind": "summary", "text": "原创结果。"}}})
    assert caught.value.code == "index_unavailable"
    assert store.get(material["id"])["artifacts"]["summary"]["state"] == "ready"


def audio():
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    data = target.getvalue()
    return AudioSegment("a_000000", 0, 1, 1, data, hashlib.sha256(data).hexdigest(), False)


def frame():
    return PreparedFrame(
        FrameCandidate("f_000000", 0, 0, ("first",), 0), PNG, hashlib.sha256(PNG).hexdigest()
    )


def client(model, response_text, requests, *, protocol="chat", parameters=None, reason="stop"):
    class Response:
        headers = {"x-request-id": "fixture-request"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return json.dumps(
                {
                    "model": model + "-returned",
                    "choices": [{"message": {"content": response_text}, "finish_reason": reason}],
                    "usage": {"total_tokens": 12, "cached_tokens": 3},
                }
            ).encode()

    def transport(request, timeout):
        requests.append(json.loads(request.data))
        return Response()

    return CloudModelClient(
        ModelProfile(
            "https://fixture.invalid/v1",
            model,
            "synthetic-key",
            protocol=protocol,
            parameters=parameters or {},
        ),
        transport=transport,
    )


def stages(store, material, requests, *, summary="参数390见[f_000000]，音频见[a_000000]。", coverage=None):
    coverage = coverage or {"complete": False, "sampling_gap": "固定采样，精度未知"}
    voice = audio_stage(
        material["id"], audio(), client("fixture-audio", "[无可辨识讲话]", requests, protocol="chat_audio")
    )
    visual = vision_stage(material["id"], frame(), client("fixture-vision", "画布390 x 844", requests))
    combined = summary_stage(
        material["id"],
        {"title": material["title"], "body": material["body"]},
        (voice.name, visual.name),
        client("fixture-summary", summary, requests),
        source_coverage=coverage,
    )
    published = publish_stage(
        store,
        material["id"],
        material["content_hash"],
        (voice.name, visual.name, combined.name),
        source_coverage=coverage,
    )
    return [voice, visual, combined, published]


def test_three_roles_publish_searchable_evidence_and_reuse_without_new_calls(store):
    material = item(store)
    requests = []
    configured = stages(store, material, requests)
    executor = DurableExecutor(store)
    result = executor.run(executor.submit(configured, idempotency_key="first", max_calls=3)["id"], configured)
    assert result["state"] == "succeeded" and len(requests) == 3
    assert [request["model"] for request in requests] == [
        "fixture-audio",
        "fixture-vision",
        "fixture-summary",
    ]
    service = ContextService(store)
    assert service.search("390")["items"][0]["material_ref"] == material["id"]
    screen = service.read(material["id"], artifact="screen")
    assert "[f_000000]" in screen["text"] and "390" in screen["text"]
    assert screen["coverage"]["complete"] is False and screen["warnings"]
    original = service.read(material["id"])["text"]
    second = executor.run(
        executor.submit(configured, idempotency_key="second", max_calls=0)["id"], configured
    )
    assert second["state"] == "succeeded" and second["calls"] == [] and len(requests) == 3
    assert service.read(material["id"])["text"] == original


def test_summary_keeps_ocr_diagnostics_local_without_duplicate_raw_text_upload(store):
    material = item(store)
    requests = []
    coverage = {
        "complete": False,
        "ocr_state": "partial",
        "ocr_failures": ["f_000001"],
        "ocr_calls": 3,
        "ocr_records": [{"lines": [{"text": "ONLY_LOCAL_OCR_" * 20_000}]}],
    }
    configured = stages(store, material, requests, coverage=coverage)
    executor = DurableExecutor(store)
    result = executor.run(
        executor.submit(configured, idempotency_key="local-ocr", max_calls=3)["id"], configured
    )
    assert result["state"] == "succeeded" and len(requests) == 3
    sent = json.dumps(requests[-1], ensure_ascii=False)
    assert "ONLY_LOCAL_OCR" not in sent and "ocr_records" not in sent
    assert "ocr_failures" in sent and "f_000001" in sent and "ocr_calls" in sent
    stored = ContextService(store).read(material["id"], artifact="screen")["coverage"]
    assert stored["source_coverage"]["ocr_records_count"] == len(coverage["ocr_records"])
    assert store.get(material["id"])["artifacts"]["summary"]["coverage"]["source_coverage"]["ocr_records"] == coverage["ocr_records"]


def test_metadata_only_update_reuses_audio_vision_but_changes_summary(store):
    material = item(store)
    requests = []
    first = stages(store, material, requests)
    executor = DurableExecutor(store)
    assert (
        executor.run(executor.submit(first, idempotency_key="metadata-1", max_calls=3)["id"], first)["state"]
        == "succeeded"
    )
    updated = store.upsert(
        {"native_id": "9", "title": material["title"], "body": "作者更新原文，媒体未变"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    second = stages(store, updated, requests)
    result = executor.run(executor.submit(second, idempotency_key="metadata-2", max_calls=1)["id"], second)
    assert result["state"] == "succeeded"
    assert [request["model"] for request in requests] == [
        "fixture-audio",
        "fixture-vision",
        "fixture-summary",
        "fixture-summary",
    ]


def test_missing_or_invented_summary_reference_is_partial_not_success(store):
    material = item(store)
    for index, text in enumerate(("有参数却没引用", "参数见[f_999999]")):
        configured = stages(store, material, [], summary=text, coverage={"complete": True})
        # New explicit prompt strategy for a separately requested trial; no automatic repair request.
        configured[2] = replace(configured[2], processor_version=f"summary_trial_{index}")
        executor = DurableExecutor(store)
        result = executor.run(
            executor.submit(configured, idempotency_key=f"citation-{index}", max_calls=3)["id"], configured
        )
        assert result["state"] == "partial"
        assert ContextService(store).read(material["id"], artifact="summary")["coverage"]["complete"] is False


def test_summary_input_limit_stops_before_request_intent(store):
    material = item(store)
    requests = []
    combined = summary_stage(
        material["id"],
        {"body": "原文" * 1000},
        (),
        client("fixture-summary", "unused", requests),
        source_coverage={},
        max_input_chars=100,
    )
    executor = DurableExecutor(store)
    result = executor.run(
        executor.submit([combined], idempotency_key="long-summary", max_calls=1)["id"], [combined]
    )
    assert result["state"] == "failed" and result["calls"] == [] and requests == []


def test_role_profile_is_frozen_and_secrets_are_not_persisted(store):
    material = item(store)
    requests = []
    model = client("fixture-vision", "画布390", requests, parameters={"thinking": {"type": "disabled"}})
    visual = vision_stage(material["id"], frame(), model)
    model.profile.parameters["thinking"]["type"] = "enabled"
    executor = DurableExecutor(store)
    result = executor.run(
        executor.submit([visual], idempotency_key="profile-fixed", max_calls=1)["id"], [visual]
    )
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "synthetic-key" not in json.dumps(store.snapshot())
    assert "synthetic-key" not in json.dumps(executor.jobs.read_result(result["calls"][0]["result"]))


def test_wrong_role_or_changed_media_never_creates_a_paid_stage(store):
    material = item(store)
    with pytest.raises(ContextError) as caught:
        audio_stage(material["id"], audio(), client("text-only", "unused", []))
    assert caught.value.code == "model_capability_required"
    bad = replace(frame(), sha256="wrong")
    with pytest.raises(ContextError) as caught:
        vision_stage(material["id"], bad, client("vision", "unused", []))
    assert caught.value.code == "media_changed"


def test_media_preflight_rejects_empty_or_unsupported_image_before_job(store):
    material = item(store)
    bad_audio = replace(audio(), data=b"", sha256=hashlib.sha256(b"").hexdigest())
    with pytest.raises(ContextError) as caught:
        audio_stage(material["id"], bad_audio, client("audio", "unused", [], protocol="chat_audio"))
    assert caught.value.code == "media_input_limit"
    with pytest.raises(ContextError) as caught:
        vision_stage(
            material["id"], replace(frame(), mime_type="image/svg+xml"), client("vision", "unused", [])
        )
    assert caught.value.code == "unsupported_media"
    assert store.snapshot()["jobs"] == {}


def test_bundle_failure_keeps_old_all_artifacts_visible(store, monkeypatch):
    material = item(store)
    old = store.save_bundle(
        material["id"],
        {kind: {"text": "旧" + kind, "processor_version": "old"} for kind in ("audio", "screen")},
        expected_content_hash=material["content_hash"],
    )
    write = store.files.write

    def fail(path, data, **kwargs):
        if path.endswith("画面文字.md"):
            raise ContextError("storage_unavailable", "模拟第二个文件写入失败")
        return write(path, data, **kwargs)

    monkeypatch.setattr(store.files, "write", fail)
    with pytest.raises(ContextError):
        store.save_bundle(
            material["id"],
            {kind: {"text": "新" + kind, "processor_version": "new"} for kind in ("audio", "screen")},
            expected_content_hash=material["content_hash"],
        )
    assert store.get(material["id"])["artifacts"] == old


def test_publish_refuses_external_edit_and_preserves_user_note(store):
    material = item(store)
    note = store.save_artifact(
        material["id"],
        "user_note",
        "我的备注",
        processor_version="user",
        expected_content_hash=material["content_hash"],
    )
    existing = store.save_artifact(
        material["id"],
        "screen",
        "旧机器文字",
        processor_version="old",
        expected_content_hash=material["content_hash"],
    )
    store.files.write(existing["path"], "用户手动修正文案".encode(), replace=True)
    configured = stages(store, material, [])
    executor = DurableExecutor(store)
    result = executor.run(
        executor.submit(configured, idempotency_key="edit-conflict", max_calls=3)["id"], configured
    )
    assert result["state"] == "partial" and result["stages"]["publish"]["error"]["code"] == "artifact_changed"
    assert store.get(material["id"])["artifacts"]["user_note"] == note
    assert store.files.read(existing["path"]).decode() == "用户手动修正文案"
