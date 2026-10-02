"""Offline queue reconstruction, including plans created before prompt version pinning."""

import copy

import pytest
from test_context_model_registry import configure, prepared, requests

from collection_context.application.contracts import ContextError
from collection_context.library.store import LibraryStore
from collection_context.processing.stages import LEGACY_SUMMARY_VERSION, SUMMARY_VERSION, summary_stage
from collection_context.workflows.executor import plan
from collection_context.workflows.extraction import ExtractionWorkflow


@pytest.fixture
def environment(tmp_path):
    store = LibraryStore.initialize(tmp_path / "version-library")
    configure(store, "k_" + "1" * 32, "vision", "fixture-vision")
    configure(store, "k_" + "1" * 32, "summary", "fixture-summary")
    _, identity = prepared(store)
    try:
        yield store, identity
    finally:
        store.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_pinned_or_legacy_queue_reopens_without_reinterpreting_prompt(environment, monkeypatch, legacy):
    store, identity = environment
    sent = requests(monkeypatch)
    resolved = []

    def resolve(ref):
        resolved.append(ref)
        return "synthetic-no-real-key"

    workflow = ExtractionWorkflow(store, resolve)
    payload = workflow.prepare_plan(identity)
    version = LEGACY_SUMMARY_VERSION if legacy else SUMMARY_VERSION
    assert payload["extraction"]["summary_prompt_version"] == SUMMARY_VERSION
    if legacy:
        payload["extraction"].pop("summary_prompt_version")
        payload["plan"] = plan(workflow._build(payload["extraction"], planning=True))
    assert not resolved and not sent
    expected = copy.deepcopy(payload["plan"])
    job = workflow.executor.jobs.submit("process", payload, idempotency_key="pinned", max_calls=3)
    monkeypatch.setattr("collection_context.workflows.extraction.SUMMARY_VERSION", "future-default")
    monkeypatch.setattr("collection_context.processing.stages.SUMMARY_VERSION", "future-default")
    reopened = LibraryStore(store.files.root)
    try:
        after = ExtractionWorkflow(reopened, resolve)
        assert plan(after._build(payload["extraction"], planning=True)) == expected
        assert (
            next(
                stage
                for stage in after._build(payload["extraction"], planning=True)
                if stage.name == "summary"
            ).processor_version
            == version
        )
        result = after.run(job["id"])
        assert result["state"] == "succeeded" and len(sent) == 3
        summary_prompt = sent[-1]["messages"][0]["content"][0]["text"]
        assert ("allowed_citations" in summary_prompt) is not legacy
    finally:
        reopened.close()


@pytest.mark.parametrize("version", [None, [], {}, "extraction_summary_v999"])
def test_unknown_prompt_version_rejects_before_any_credential(environment, version):
    store, identity = environment
    workflow = ExtractionWorkflow(store, lambda _: pytest.fail("resolved credential"))
    context = workflow.prepare_plan(identity)["extraction"]
    context["summary_prompt_version"] = version
    with pytest.raises(ContextError) as caught:
        workflow._build(context)
    assert caught.value.code == "processor_version_changed"


def test_legacy_summary_identity_is_unchanged():
    from collection_context.processing.models import CloudModelClient, ModelProfile

    stage = summary_stage(
        "i_original_fixture",
        {
            "title": "原创界面参数测试",
            "body": "独立原创截图与本机合成语音。不是私人收藏；仅验收本轮指定片段。",
        },
        ("screen_f_000000",),
        CloudModelClient(ModelProfile("https://fixture.invalid/v1", "fixture", "synthetic-not-live-key")),
        source_coverage={"complete": False, "scope": "one original still page"},
        prompt_version=LEGACY_SUMMARY_VERSION,
    )
    assert stage.input_hash == "00017d84f8b516bae3304602eecfab61c153d24b56b4e8b1d41df8b64e09ff32"
