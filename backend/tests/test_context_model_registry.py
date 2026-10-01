"""Fixed profiles/private service secrets/reconstructed jobs; no real provider requests."""

import json
import os

import pytest
from test_context_media import PNG

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow


@pytest.fixture
def environment(tmp_path):
    store = LibraryStore.initialize(tmp_path / "独立库")
    secrets = FileSecrets.initialize(tmp_path / "独立服务凭据")
    key = secrets.put("synthetic-key-not-real")
    try:
        yield store, secrets, key
    finally:
        secrets.close()
        store.close()


def configure(store, key, role, model, **kwargs):
    return ModelCatalog(store).configure(
        role=role, base_url="https://fixture.invalid/v1", model=model, credential_ref=key, **kwargs
    )


def prepared(store):
    material = store.upsert(
        {"native_id": "81", "media_type": "image", "title": "合成图文"}, kind="saved", scope_id="s_saved"
    )["item"]
    identity = PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png"), (PNG, "image/png")])
    return material, identity


def requests(monkeypatch, callback=None):
    sent = []

    class Response:
        headers = {"x-request-id": "synthetic-request"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return json.dumps(
                {
                    "model": "synthetic-returned",
                    "choices": [{"message": {"content": "合成画布390[f_000000]。"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 5},
                }
            ).encode()

    original = ModelCatalog.client

    def client(self, identity, resolve):
        result = original(self, identity, resolve)

        def transport(request, timeout):
            sent.append(json.loads(request.data))
            if callback is not None:
                callback(sent)
            return Response()

        result.transport = transport
        return result

    monkeypatch.setattr(ModelCatalog, "client", client)
    return sent


def test_defaults_can_change_but_queued_job_keeps_profile_after_reopen(environment, monkeypatch):
    store, secrets, key = environment
    material, identity = prepared(store)
    configure(store, key, "vision", "vision-before")
    configure(store, key, "summary", "summary-before")
    sent = requests(monkeypatch)
    resolved = []

    def resolve(ref):
        resolved.append(ref)
        return secrets.get(ref)

    queued = ExtractionWorkflow(store, resolve).submit(identity, idempotency_key="fixed", max_calls=3)
    assert not sent and not resolved
    configure(store, key, "vision", "vision-after")
    configure(store, key, "summary", "summary-after")
    reopened = LibraryStore(store.files.root)
    try:
        workflow = ExtractionWorkflow(reopened, resolve)
        result = workflow.run(queued["id"])
        assert result["state"] == "succeeded"
        assert [request["model"] for request in sent] == ["vision-before", "vision-before", "summary-before"]
        assert result["stages"]["audio_not_applicable"]["state"] == "not_applicable"
        text = ContextService(reopened).read(material["id"], artifact="screen")["text"]
        assert "原图第1页" in text and "原图第2页" in text and "名义采样时间" not in text
        assert "synthetic-key-not-real" not in json.dumps(reopened.snapshot())
        assert (
            ContextService(reopened).status(material["id"])["artifacts"]["audio"]["state"] == "not_applicable"
        )
    finally:
        reopened.close()


def test_reconstructed_job_reuses_across_new_jobs_without_model_calls(environment, monkeypatch):
    store, secrets, key = environment
    _, identity = prepared(store)
    configure(store, key, "vision", "vision")
    configure(store, key, "summary", "summary")
    sent = requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    assert (
        workflow.run(workflow.submit(identity, idempotency_key="first", max_calls=3)["id"])["state"]
        == "succeeded"
    )
    second = workflow.run(workflow.submit(identity, idempotency_key="second", max_calls=0)["id"])
    assert second["state"] == "succeeded" and second["calls"] == [] and len(sent) == 3


def test_changed_input_or_excluded_source_stops_before_request(environment, monkeypatch):
    store, secrets, key = environment
    material, identity = prepared(store)
    configure(store, key, "vision", "vision")
    configure(store, key, "summary", "summary")
    sent = requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    job = workflow.submit(identity, idempotency_key="changed", max_calls=3)
    store.upsert(
        {"native_id": "81", "media_type": "image", "body": "新文案"}, kind="saved", scope_id="s_saved"
    )
    with pytest.raises(ContextError) as caught:
        workflow.run(job["id"])
    assert caught.value.code == "version_changed" and sent == []
    store.exclude(material["id"])
    with pytest.raises(ContextError) as caught:
        workflow.run(job["id"])
    assert caught.value.code == "not_found" and sent == []


def test_new_media_same_metadata_marks_machine_artifacts_stale_and_blocks_old_job(environment, monkeypatch):
    store, secrets, key = environment
    material, identity = prepared(store)
    configure(store, key, "vision", "vision")
    configure(store, key, "summary", "summary")
    sent = requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    done = workflow.run(workflow.submit(identity, idempotency_key="done", max_calls=3)["id"])
    assert done["state"] == "succeeded"
    store.save_artifact(
        material["id"],
        "user_note",
        "自己的备注",
        processor_version="user",
        expected_content_hash=material["content_hash"],
    )
    old = workflow.submit(identity, idempotency_key="old-media", max_calls=0)
    new_input = PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])
    assert new_input != identity and store.get(material["id"])["content_hash"] == material["content_hash"]
    status = ContextService(store).status(material["id"])["artifacts"]
    assert status["screen"]["state"] == "stale" and status["summary"]["state"] == "stale"
    assert status["user_note"]["state"] == "ready"
    with pytest.raises(ContextError) as caught:
        workflow.run(old["id"])
    assert caught.value.code == "input_superseded" and len(sent) == 3


def test_media_update_during_model_requests_cannot_publish_old_snapshot(environment, monkeypatch):
    store, secrets, key = environment
    material, identity = prepared(store)
    configure(store, key, "vision", "vision")
    configure(store, key, "summary", "summary")

    def changed(sent):
        if len(sent) == 3:
            PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])

    sent = requests(monkeypatch, changed)
    workflow = ExtractionWorkflow(store, secrets.get)
    result = workflow.run(workflow.submit(identity, idempotency_key="in-flight-update", max_calls=3)["id"])
    assert result["state"] == "partial" and len(sent) == 3
    assert result["stages"]["publish"]["error"]["code"] == "input_superseded"
    assert store.get(material["id"])["artifacts"] == {}
    assert all(call["state"] == "completed" for call in result["calls"])


def test_bundle_checks_media_version_inside_commit(environment, monkeypatch):
    store, _, _ = environment
    material, old_input = prepared(store)
    PreparedInputs(store).prepare_images(material["id"], [(PNG, "image/png")])
    with pytest.raises(ContextError) as caught:
        store.save_bundle(
            material["id"],
            {"screen": {"text": "旧媒体提取", "processor_version": "fixture"}},
            expected_content_hash=material["content_hash"],
            expected_prepared_input=old_input,
        )
    assert caught.value.code == "input_superseded" and store.get(material["id"])["artifacts"] == {}


def test_secret_permissions_paths_overlap_and_repr_are_safe(environment, tmp_path):
    store, secrets, key = environment
    assert secrets.get(key) == "synthetic-key-not-real"
    assert "synthetic-key-not-real" not in repr(secrets)
    with pytest.raises(ContextError):
        secrets.get("../escape")
    os.chmod(secrets.files.root / key, 0o644)
    with pytest.raises(ContextError) as caught:
        secrets.get(key)
    assert caught.value.code == "unsafe_secret_permissions"
    os.chmod(secrets.files.root / key, 0o600)
    inside = FileSecrets.initialize(store.files.root / "secrets")
    try:
        with pytest.raises(ContextError) as caught:
            ExtractionWorkflow(store, inside.get)
        assert caught.value.code == "secret_directory_overlap"
    finally:
        inside.close()
    with pytest.raises(ContextError):
        FileSecrets.initialize(secrets.files.root)
    os.chmod(secrets.files.root, 0o755)
    with pytest.raises(ContextError) as caught:
        secrets.get(key)
    assert caught.value.code == "unsafe_secret_permissions"


@pytest.mark.parametrize(
    "setting",
    [
        [],
        {"api_key": "never-store"},
        {"thinking": {"type": []}},
        {"temperature": float("nan")},
        {"temperature": True},
        {"max_tokens": 0},
        {"reasoning_effort": "unbounded"},
    ],
)
def test_uncontrolled_profile_parameters_are_rejected(environment, setting):
    store, _, key = environment
    with pytest.raises(ContextError) as caught:
        configure(store, key, "vision", "vision", parameters=setting)
    assert caught.value.code == "invalid_model_config" and "never-store" not in str(caught.value)
    assert store.snapshot().get("model_profiles", {}) == {}


def test_profile_roles_structure_and_id_tampering_rejected(environment):
    store, _, key = environment
    with pytest.raises(ContextError):
        configure(store, key, "audio", "audio", protocol="chat")
    with pytest.raises(ContextError):
        configure(store, key, "vision", "vision", protocol=[])
    identity = configure(store, key, "vision", "vision")
    store.transact(lambda state: state["model_profiles"][identity].update(model="silently-changed"))
    with pytest.raises(ContextError) as caught:
        ModelCatalog(store).get(identity)
    assert caught.value.code == "model_config_changed"


def test_public_cli_model_settings_never_exposes_credentials(environment, capsys):
    store, _, key = environment
    configure(store, key, "vision", "vision")
    assert main(["--workspace", str(store.files.root), "model-settings"]) == 0
    output = capsys.readouterr().out
    assert "vision" in output and key not in output and "synthetic-key-not-real" not in output
    assert (
        main(
            [
                "--workspace",
                str(store.files.root),
                "configure-model",
                "--role",
                "vision",
                "--base-url",
                "https://fixture.invalid/v1",
                "--model",
                "vision",
                "--credential-ref",
                key,
                "--parameters",
                "not-json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_model_config"


def test_missing_secret_cannot_fall_back_to_other_projects_or_defaults(environment, monkeypatch):
    store, secrets, key = environment
    _, identity = prepared(store)
    configure(store, key, "vision", "vision")
    configure(store, key, "summary", "summary")
    sent = requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    job = workflow.submit(identity, idempotency_key="missing-key", max_calls=3)
    secrets.files.unlink(key)
    with pytest.raises(ContextError) as caught:
        workflow.run(job["id"])
    assert caught.value.code == "credential_missing" and sent == []
    assert workflow.executor.jobs.get(job["id"])["calls"] == []
