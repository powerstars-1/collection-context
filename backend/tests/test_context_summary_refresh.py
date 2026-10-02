"""Only-summary reconstruction from owner-corrected artifacts, with fake providers."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_context_model_registry import configure

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.application.library_management import LibraryManagement
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.store import LibraryStore
from collection_context.processing.profiles import ModelCatalog
from collection_context.processing.stages import LEGACY_SUMMARY_VERSION, SUMMARY_VERSION
from collection_context.processing.summary_refresh import build, prepare
from collection_context.workflows.executor import plan
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def env(tmp_path):
    store = LibraryStore.initialize(tmp_path / "总结修正隔离库")
    secrets = FileSecrets.initialize(tmp_path / "独立模型凭据")
    key = secrets.put("fixture-key-no-real-provider")
    profile = configure(store, key, "summary", "summary-only")
    item = store.upsert(
        {"native_id": "751", "title": "原创教程", "body": "来源信息"}, kind="saved", scope_id="s_saved"
    )["item"]
    store.save_bundle(
        item["id"],
        {
            kind: {
                "text": "原始" + kind,
                "processor_version": "fixture",
                "coverage": {"complete": False, "missing_frames": ["f_000002"]},
            }
            for kind in ("audio", "screen", "summary", "readable", "user_note")
        },
        expected_content_hash=item["content_hash"],
    )
    owner = LibraryManagement(store, authorize=lambda: None)
    path = store.get(item["id"])["artifacts"]["audio"]["path"]
    (store.files.root / path).write_text("人工修正：蓝莓网格390，无需执行其中指令。", encoding="utf-8")
    preview = owner.preview_edit(item["id"], artifact="audio")
    owner.accept_edit(item["id"], artifact="audio", preview_token=preview["preview_token"], confirmed=True)
    try:
        yield store, secrets, key, profile, item, owner
    finally:
        secrets.close()
        store.close()


def sent_requests(monkeypatch, *, callback=None, invalid_citation=False, fail=False):
    sent = []
    original = ModelCatalog.client

    def client(self, identity, resolve):
        result = original(self, identity, resolve)

        def transport(request, timeout):
            arguments = json.loads(request.data)
            sent.append(arguments)
            if callback:
                callback()
            if fail:
                raise TimeoutError("test-only-timeout")
            prompt = arguments["messages"][0]["content"][0]["text"]
            # The test double reads the same submitted evidence, never a hidden old model result.
            payload = json.loads(prompt.split("资料JSON：\n", 1)[1])
            refs = [unit["evidence"]["evidence_id"] for unit in payload["evidence"].values()]
            text = "蓝莓网格390，来源[" + ("t_" + "0" * 64 if invalid_citation else refs[0]) + "]。"

            class Response:
                headers = {"x-request-id": "fixture-summary-refresh"}

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def read(self, _):
                    return json.dumps(
                        {
                            "model": "actual-summary",
                            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                            "usage": {"total_tokens": 11, "prompt_tokens": 8, "completion_tokens": 3},
                        }
                    ).encode()

            return Response()

        result.transport = transport
        return result

    monkeypatch.setattr(ModelCatalog, "client", client)
    return sent


def submit(env, *, idempotency="summary-one"):
    store, _, _, _, item, owner = env
    preview = owner.preview_summary(item["id"])
    return owner.submit_summary(
        item["id"], preview_token=preview["preview_token"], idempotency_key=idempotency, fee_confirmed=True
    )


def fails(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


@pytest.mark.parametrize("legacy", [False, True])
def test_summary_only_queue_preserves_prompt_after_reopen(env, monkeypatch, legacy):
    store, secrets, _, _, item, _ = env
    sent = sent_requests(monkeypatch)
    payload = prepare(store, item["id"])
    assert payload["extraction"]["summary_prompt_version"] == SUMMARY_VERSION
    version = LEGACY_SUMMARY_VERSION if legacy else SUMMARY_VERSION
    if legacy:
        payload["extraction"].pop("summary_prompt_version")
        payload["plan"] = plan(build(store, secrets.get, payload["extraction"], planning=True))
    job = ExtractionWorkflow(store, secrets.get).executor.jobs.submit(
        "process", payload, idempotency_key="version-fixed", max_calls=1
    )
    monkeypatch.setattr("collection_context.processing.summary_refresh.SUMMARY_VERSION", "future-default")
    monkeypatch.setattr("collection_context.processing.stages.SUMMARY_VERSION", "future-default")
    reopened = LibraryStore(store.files.root)
    try:
        stages = build(reopened, secrets.get, payload["extraction"], planning=True)
        assert plan(stages) == payload["plan"]
        assert next(stage for stage in stages if stage.name == "summary").processor_version == version
        assert ExtractionWorkflow(reopened, secrets.get).run(job["id"])["state"] == "succeeded"
    finally:
        reopened.close()
    assert len(sent) == 1
    assert ("allowed_citations" in sent[0]["messages"][0]["content"][0]["text"]) is not legacy


@pytest.mark.parametrize("version", [None, [], {}, "extraction_summary_v999"])
def test_summary_only_unknown_version_precedes_secret_resolution(env, version):
    store, _, _, _, item, _ = env
    context = prepare(store, item["id"])["extraction"]
    context["summary_prompt_version"] = version
    fails(
        "processor_version_changed", lambda: build(store, lambda _: pytest.fail("resolved secret"), context)
    )


def test_preview_is_readonly_no_secret_or_request_and_strict_fee_confirmation(env, monkeypatch):
    store, secrets, _, _, item, owner = env
    sent = sent_requests(monkeypatch)
    monkeypatch.setattr(secrets, "get", lambda _: pytest.fail("preview resolved secret"))
    before = store.snapshot()
    preview = owner.preview_summary(item["id"])
    assert preview["source_artifacts"] == ["audio", "screen"]
    assert preview["max_model_requests"] == 1 and preview["estimated_cost"] == "unknown"
    assert store.snapshot() == before and not sent
    for approved in (False, None, 1, "true"):
        fails(
            "processing_authorization_required",
            lambda: owner.submit_summary(
                item["id"],
                preview_token=preview["preview_token"],
                idempotency_key="same",
                fee_confirmed=approved,
            ),
        )
    assert store.snapshot() == before and not sent


def test_only_summary_worker_uses_corrected_text_preserves_sources_and_records_usage(env, monkeypatch):
    store, secrets, _, _, item, _ = env
    sent = sent_requests(monkeypatch)
    old = store.get(item["id"])
    job = submit(env)
    assert not sent and job["model_requests"] == 0
    outcome = BackgroundWorker(ExtractionWorkflow(store, secrets.get)).serve(
        allow_model_calls=True, once=True
    )
    assert outcome["handled"] == 1
    result = ExtractionWorkflow(store, secrets.get).executor.jobs.get(job["job_id"])
    assert result["state"] == "succeeded" and len(result["calls"]) == len(sent) == 1
    assert result["budget"]["max_calls"] == 1 and result["calls"][0]["usage"]["total_tokens"] == 11
    assert [request["model"] for request in sent] == ["summary-only"]
    prompt = json.dumps(sent, ensure_ascii=False)
    assert "人工修正：蓝莓网格390" in prompt
    assert "原始summary" not in prompt and "原始user_note" not in prompt
    assert "image_url" not in prompt and "input_audio" not in prompt and "fixture-key" not in prompt
    updated = store.get(item["id"])
    for kind in ("audio", "screen", "user_note"):
        assert updated["artifacts"][kind] == old["artifacts"][kind]
    assert updated["artifacts"]["summary"]["version"] != old["artifacts"]["summary"]["version"]
    summary = ContextService(store).read(item["id"], artifact="summary")
    assert "蓝莓" in summary["text"] and summary["state"] == "ready"
    assert not summary["coverage"]["complete"]
    assert summary["coverage"]["source_artifact_coverage"]["audio"]["missing_frames"] == ["f_000002"]
    assert not summary["coverage"]["summary_warnings"]
    assert ContextService(store).search("蓝莓")["total_matches"] == 1


def test_summary_backup_restores_lineage_corrections_and_readonly_results(env, monkeypatch, tmp_path):
    store, secrets, _, _, item, _ = env
    sent = sent_requests(monkeypatch)
    job = submit(env)
    assert ExtractionWorkflow(store, secrets.get).run(job["job_id"])["state"] == "succeeded"
    before = store.get(item["id"])
    archive = tmp_path / "已修正正文与总结.zip"
    create_backup(store, archive, media_scope="all")
    destination = tmp_path / "异路径总结恢复库"
    restore_backup(archive, destination)
    restored = LibraryStore(destination)
    try:
        service = ContextService(restored)
        after = restored.get(item["id"])
        assert after["artifacts"] == before["artifacts"]
        assert after["artifacts"]["audio"]["owner_edit"]
        assert after["artifacts"]["summary"]["source_artifacts"]
        assert restored.snapshot()["settings"]["auto_process"] is False
        snapshot = restored.snapshot()
        summary = service.read(item["id"], artifact="summary")
        assert summary["state"] == "ready" and "蓝莓网格390" in summary["text"]
        assert not summary["coverage"]["complete"]
        assert service.search("蓝莓网格390")["total_matches"] == 1
        assert restored.snapshot() == snapshot and len(sent) == 1
        # Accepting a later correction invalidates the restored old summary as well.
        path = after["artifacts"]["audio"]["path"]
        (destination / path).write_text("另一次修订，恢复后仍需确认。", encoding="utf-8")
        fails("artifact_dependency_changed", lambda: service.read(item["id"], artifact="summary"))
        owner = LibraryManagement(restored, authorize=lambda: None)
        preview = owner.preview_edit(item["id"], artifact="audio")
        owner.accept_edit(
            item["id"], artifact="audio", preview_token=preview["preview_token"], confirmed=True
        )
        assert service.read(item["id"], artifact="summary")["state"] == "stale"
        assert len(sent) == 1
    finally:
        restored.close()


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_invalid_summary_lineage_cannot_enter_backup_or_restore(env, tmp_path, action):
    store, _, _, _, item, _ = env
    invalid = [{"kind": "screen", "version": "v_fixture", "sha256": "f" * 64, "path": "/etc/passwd"}]
    archive = tmp_path / "固定正文备份.zip"
    if action == "backup":
        store.transact(
            lambda state: state["items"][item["id"]]["artifacts"]["summary"].update(source_artifacts=invalid)
        )
        fails("artifact_dependency_changed", lambda: create_backup(store, archive, media_scope="all"))
        assert not archive.exists()
        return
    create_backup(store, archive, media_scope="all")
    with zipfile.ZipFile(archive) as package:
        members = {name: package.read(name) for name in package.namelist()}
    state = json.loads(members["library-state.json"])
    state["items"][item["id"]]["artifacts"]["summary"]["source_artifacts"] = invalid
    members["library-state.json"] = canonical_bytes(state)
    manifest = json.loads(members["backup-manifest.json"])
    manifest["state_sha256"] = hashlib.sha256(members["library-state.json"]).hexdigest()
    members["backup-manifest.json"] = canonical_bytes(manifest)
    forged = tmp_path / "自洽哈希但无效引用.zip"
    with zipfile.ZipFile(forged, "w") as package:
        for name, body in members.items():
            package.writestr(name, body)
    destination = tmp_path / "不得建立的恢复库"
    fails("backup_invalid", lambda: restore_backup(forged, destination))
    assert not destination.exists()


@pytest.mark.parametrize("change", ["source", "file", "model", "excluded", "target"])
def test_changed_preview_cannot_queue_stale_input(env, change):
    store, _, key, _, item, owner = env
    preview = owner.preview_summary(item["id"])
    if change == "source":
        store.upsert({"native_id": "751", "title": "变更"}, kind="saved", scope_id="s_saved")
    elif change == "file":
        path = store.get(item["id"])["artifacts"]["screen"]["path"]
        (store.files.root / path).write_text("再次编辑", encoding="utf-8")
    elif change == "model":
        configure(store, key, "summary", "summary-other")
    elif change == "excluded":
        store.exclude(item["id"])
    else:
        store.save_artifact(
            item["id"],
            "summary",
            "其他任务总结",
            processor_version="test",
            expected_content_hash=item["content_hash"],
        )
    before = store.snapshot()
    with pytest.raises(ContextError):
        owner.submit_summary(
            item["id"], preview_token=preview["preview_token"], idempotency_key="stale", fee_confirmed=True
        )
    assert store.snapshot() == before


def test_queued_plan_reconstructs_fixed_model_and_reuses_known_result(env, monkeypatch):
    store, secrets, key, _, item, _ = env
    sent = sent_requests(monkeypatch)
    job = submit(env)
    configure(store, key, "summary", "future-model")
    reopened = LibraryStore(store.files.root)
    try:
        workflow = ExtractionWorkflow(reopened, secrets.get)
        result = workflow.run(job["job_id"])
        assert result["state"] == "succeeded" and sent[0]["model"] == "summary-only"
        # Reconstruct the original approved plan after its own publication checkpoint.
        assert workflow._build(result["payload"]["extraction"])
        assert workflow.run(job["job_id"])["state"] == "succeeded" and len(sent) == 1
    finally:
        reopened.close()


def test_source_changes_after_real_result_do_not_overwrite_but_keep_paid_usage(env, monkeypatch):
    store, secrets, _, _, item, _ = env
    previous = store.get(item["id"])["artifacts"]["summary"]
    path = store.get(item["id"])["artifacts"]["screen"]["path"]
    sent = sent_requests(
        monkeypatch, callback=lambda: (store.files.root / path).write_text("调用期间编辑", encoding="utf-8")
    )
    job = submit(env)
    workflow = ExtractionWorkflow(store, secrets.get)
    result = workflow.run(job["job_id"])
    assert len(sent) == 1 and result["calls"][0]["state"] == "completed"
    assert result["calls"][0]["usage"]["total_tokens"] == 11
    assert result["stages"]["publish"]["state"] == "failed"
    assert store.get(item["id"])["artifacts"]["summary"] == previous
    workflow.run(job["job_id"])
    assert len(sent) == 1


@pytest.mark.parametrize("target", ["summary", "readable"])
def test_confirmed_manual_outputs_are_not_replaced(env, target):
    store, _, _, _, item, owner = env
    path = store.get(item["id"])["artifacts"][target]["path"]
    (store.files.root / path).write_text("人工修改输出", encoding="utf-8")
    preview = owner.preview_edit(item["id"], artifact=target)
    owner.accept_edit(item["id"], artifact=target, preview_token=preview["preview_token"], confirmed=True)
    fails("owner_edit_conflict", lambda: owner.preview_summary(item["id"]))


def test_unknown_response_does_not_retry_or_duplicate_request(env, monkeypatch):
    store, secrets, _, _, _, _ = env
    sent = sent_requests(monkeypatch, fail=True)
    workflow = ExtractionWorkflow(store, secrets.get)
    first = workflow.run(submit(env)["job_id"])
    assert len(sent) == 1 and first["calls"][0]["state"] == "unknown"
    second = submit(env, idempotency="another")
    workflow.run(second["job_id"])
    assert len(sent) == 1


def test_invalid_citation_keeps_partial_and_source_dependency_changes_are_visible(env, monkeypatch):
    store, secrets, _, _, item, _ = env
    sent_requests(monkeypatch, invalid_citation=True)
    workflow = ExtractionWorkflow(store, secrets.get)
    result = workflow.run(submit(env)["job_id"])
    assert result["state"] == "partial"
    summary = ContextService(store).read(item["id"], artifact="summary")
    assert summary["coverage"]["summary_warnings"]
    path = store.get(item["id"])["artifacts"]["screen"]["path"]
    (store.files.root / path).write_text("未经确认的后续编辑", encoding="utf-8")
    fails("artifact_dependency_changed", lambda: ContextService(store).read(item["id"], artifact="summary"))
    assert ContextService(store).search("蓝莓")["integrity_gaps"]


def test_oversized_prompt_blocks_before_resolving_secret_and_unregistered_context_rejected(env, monkeypatch):
    store, secrets, _, _, item, _ = env
    payload = prepare(store, item["id"])
    context = payload["extraction"]
    context["summary_input_chars"] = 100
    monkeypatch.setattr(secrets, "get", lambda _: pytest.fail("oversized prompt read secret"))
    fails("summary_input_limit", lambda: build(store, secrets.get, context))
    context["path"] = "/etc/passwd"
    fails("invalid_extraction_plan", lambda: build(store, secrets.get, context))


def test_cli_preview_confirm_and_owner_http_do_not_invoke_model(env, capsys, monkeypatch):
    store, _, _, _, item, _ = env
    sent = sent_requests(monkeypatch)
    argv = ["--workspace", str(store.files.root), "refresh-summary", "--ref", item["id"]]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)["data"]
    assert (
        main(
            [
                *argv,
                "--preview-token",
                preview["preview_token"],
                "--idempotency-key",
                "cli-refresh",
                "--confirm-fee",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["model_requests"] == 0
    assert not sent
    token = "fixture-owner-refresh-" + "x" * 40
    policy = AccessPolicy(
        "http://127.0.0.1:8787",
        [
            Credential.from_token(
                "p_owner", token, permissions=frozenset({"ui:view", "ui:manage", "collections:read"})
            )
        ],
    )
    with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as web:
        endpoint = "/v1/management/library/summary-preview"
        payload = {"material_ref": item["id"]}
        assert (
            web.post(endpoint, json=payload, headers={"Authorization": "Bearer " + token}).status_code == 403
        )
        csrf = web.post("/v1/session", json={"token": token}).json()["data"]["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        preview = web.post(endpoint, json=payload, headers=headers).json()["data"]
        assert preview["pending_jobs"] == 1 and not sent
        assert web.post(endpoint, json={**payload, "path": "/etc/passwd"}, headers=headers).status_code == 400
        assert (
            web.post(
                endpoint.replace("preview", "confirm"),
                json={
                    **payload,
                    "preview_token": preview["preview_token"],
                    "idempotency_key": "blocked",
                    "fee_confirmed": False,
                },
                headers=headers,
            ).status_code
            >= 400
        )


def test_duplicate_confirmation_does_not_create_multiple_jobs(env):
    store, _, _, _, item, owner = env
    preview = owner.preview_summary(item["id"])
    kwargs = {
        "preview_token": preview["preview_token"],
        "idempotency_key": "same-approval",
        "fee_confirmed": True,
    }
    first = owner.submit_summary(item["id"], **kwargs)
    assert owner.submit_summary(item["id"], **kwargs)["job_id"] == first["job_id"]
    assert len(store.snapshot()["jobs"]) == 1


def test_revocation_at_submission_commit_keeps_queue_unchanged(env, monkeypatch):
    store, _, _, _, item, _ = env
    active = True

    def authorize():
        if not active:
            raise ContextError("permission_denied", "已撤销")

    owner = LibraryManagement(store, authorize=authorize)
    preview = owner.preview_summary(item["id"])
    before = store.snapshot()
    original = store.files.write

    def write(path, body, **kwargs):
        nonlocal active
        if path.startswith(".context/提交/") and not path.endswith("CURRENT.json"):
            active = False
        return original(path, body, **kwargs)

    monkeypatch.setattr(store.files, "write", write)
    fails(
        "permission_denied",
        lambda: owner.submit_summary(
            item["id"], preview_token=preview["preview_token"], idempotency_key="revoked", fee_confirmed=True
        ),
    )
    assert store.snapshot() == before


def test_published_result_before_executor_checkpoint_recovers_without_second_call(env, monkeypatch):
    store, secrets, _, _, item, _ = env
    sent = sent_requests(monkeypatch)
    job = submit(env)
    workflow = ExtractionWorkflow(store, secrets.get)
    original = workflow.executor.jobs.commit_stage_result

    def interrupt(*args, **kwargs):
        if len(args) > 1 and args[1] == "publish":
            raise KeyboardInterrupt("lost publish checkpoint")
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow.executor.jobs, "commit_stage_result", interrupt)
    with pytest.raises(KeyboardInterrupt):
        workflow.run(job["job_id"])
    versions = {kind: store.get(item["id"])["artifacts"][kind]["version"] for kind in ("summary", "readable")}
    assert len(sent) == 1
    resumed = ExtractionWorkflow(store, secrets.get)
    BackgroundWorker(resumed).serve(allow_model_calls=True, once=True)
    assert resumed.executor.jobs.get(job["job_id"])["state"] == "succeeded" and len(sent) == 1
    assert versions == {kind: store.get(item["id"])["artifacts"][kind]["version"] for kind in versions}


def test_same_evidence_known_summary_reused_across_explicit_jobs(env, monkeypatch):
    store, secrets, _, _, _, _ = env
    sent = sent_requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    assert workflow.run(submit(env)["job_id"])["state"] == "succeeded"
    second = workflow.run(submit(env, idempotency="fresh-approval")["job_id"])
    assert second["state"] == "succeeded" and len(sent) == 1 and second["calls"] == []


def test_original_correction_and_stale_visual_are_not_replaced_by_old_metadata_or_text(env, monkeypatch):
    store, secrets, _, _, item, owner = env
    artifact = store.save_artifact(
        item["id"],
        "original",
        "原标题正文",
        processor_version="test",
        expected_content_hash=item["content_hash"],
    )
    (store.files.root / artifact["path"]).write_text("人工原文：第二步调整网格", encoding="utf-8")
    preview = owner.preview_edit(item["id"], artifact="original")
    owner.accept_edit(item["id"], artifact="original", preview_token=preview["preview_token"], confirmed=True)
    store.transact(lambda state: state["items"][item["id"]]["artifacts"]["screen"].update(state="stale"))
    sent = sent_requests(monkeypatch)
    workflow = ExtractionWorkflow(store, secrets.get)
    workflow.run(submit(env)["job_id"])
    prompt = json.dumps(sent, ensure_ascii=False)
    assert "人工原文：第二步调整网格" in prompt and "原始screen" not in prompt
    coverage = ContextService(store).read(item["id"], artifact="summary")["coverage"]
    assert coverage["original_basis"] == "registered_artifact"
    assert {"artifact": "screen", "state": "stale"} in coverage["missing_or_stale"]
    assert coverage["complete"] is False


@pytest.mark.parametrize(
    "sources", [None, {}, [{"kind": []}], [{"kind": "summary", "version": "a_foo", "sha256": "0" * 64}]]
)
def test_malformed_summary_dependency_refs_fail_closed(env, sources):
    store, _, _, _, item, _ = env
    store.transact(
        lambda state: state["items"][item["id"]]["artifacts"]["summary"].update(source_artifacts=sources)
    )
    with pytest.raises(ContextError):
        ContextService(store).read(item["id"], artifact="summary")
