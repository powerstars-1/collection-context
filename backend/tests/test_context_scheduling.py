"""Automatic observation/fee boundaries and atomic selected history, without real cloud access."""

import json

import pytest
from test_context_media import PNG
from test_context_model_registry import configure, requests

from collection_context.application.contracts import ContextError
from collection_context.cli import main
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.policy import execution_allowed
from collection_context.workflows.scheduling import ProcessingSchedule
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def env(tmp_path):
    store = LibraryStore.initialize(tmp_path / "独立自动规则库")
    secrets = FileSecrets.initialize(tmp_path / "独立凭据")
    key = secrets.put("synthetic-key-not-real")
    configure(store, key, "audio", "audio-before", protocol="chat_audio")
    configure(store, key, "vision", "vision-before")
    configure(store, key, "summary", "summary-before")
    workflow = ExtractionWorkflow(store, secrets.get)
    try:
        yield store, secrets, key, workflow, ProcessingSchedule(workflow)
    finally:
        secrets.close()
        store.close()


def prepared(env, native="201", **kwargs):
    item = env[0].upsert(
        {"native_id": native, "media_type": "image", "title": "原创规则样例"},
        kind="saved",
        scope_id="s_saved",
        **kwargs,
    )["item"]
    return item, PreparedInputs(env[0]).prepare_images(item["id"], [(PNG, "image/png")])


def enable(env, **kwargs):
    return env[4].configure(True, allow_model_calls=True, max_calls=2, **kwargs)


def worker(env):
    return BackgroundWorker(env[3]).serve(allow_model_calls=True, once=True, max_jobs=20)


def test_default_disabled_and_enabling_without_fee_authority_does_not_mutate(env):
    prepared(env)
    before = env[0].snapshot()
    assert env[4].status()["enabled"] is False and env[4].admit_new() == []
    with pytest.raises(ContextError) as caught:
        env[4].configure(True, max_calls=2)
    assert caught.value.code == "processing_authorization_required"
    assert env[0].snapshot() == before


def test_old_library_unknown_generation_and_unknown_action_time_are_not_new(env, monkeypatch):
    old, _ = prepared(env, "201")
    env[0].transact(lambda s: s["items"][old["id"]].pop("first_observed_generation"))
    enable(env)
    recent, _ = prepared(env, "202")
    sent = requests(monkeypatch)
    worker(env)
    assert len(sent) == 2 and len(env[0].snapshot()["jobs"]) == 1
    assert all(r["action_at"] is None for r in recent["relations"].values())
    jobs = list(env[0].snapshot()["jobs"].values())
    assert jobs[0]["payload"]["dispatch"]["mode"] == "automatic"
    assert env[4].status()["unknown_action_time_is_recent"] is False


def test_existing_work_new_relation_keeps_observation_boundary_no_reextract(env, monkeypatch):
    item, _ = prepared(env)
    generation = item["first_observed_generation"]
    enable(env)
    env[0].upsert(
        {"native_id": "201", "media_type": "image", "title": "原创规则样例"}, kind="liked", scope_id="s_liked"
    )
    sent = requests(monkeypatch)
    worker(env)
    assert not sent and env[0].get(item["id"])["first_observed_generation"] == generation
    assert len(env[0].get(item["id"])["relations"]) == 2


def test_low_permission_discovery_never_inherits_owner_auto_fee_authority(env, monkeypatch):
    enable(env)
    prepared(env, observer_principal="readonly_agent")
    sent = requests(monkeypatch)
    assert worker(env)["handled"] == 0 and not sent and not env[0].snapshot()["jobs"]


def test_pause_skips_queued_auto_but_manual_runs_and_old_backlog_does_not_resume(env, monkeypatch):
    enable(env)
    _, identity = prepared(env)
    automatic = env[4].admit_new()[0]
    env[4].configure(False)
    manual = env[3].submit(identity, idempotency_key="explicit-manual", max_calls=2)
    sent = requests(monkeypatch)
    worker(env)
    assert len(sent) == 2 and env[3].executor.jobs.get(automatic)["state"] == "queued"
    assert env[3].executor.jobs.get(manual["id"])["state"] == "succeeded"
    enable(env)
    worker(env)
    assert len(sent) == 2 and env[4].status()["paused_job_ids"] == [automatic]


def test_selected_resume_requires_current_preview_and_uses_original_models(env, monkeypatch):
    enable(env)
    prepared(env)
    automatic = env[4].admit_new()[0]
    env[4].configure(False)
    configure(env[0], env[2], "vision", "vision-after")
    configure(env[0], env[2], "summary", "summary-after")
    enable(env)
    preview = env[4].resume_preview([automatic])
    assert preview["count"] == 1 and preview["max_calls"] == 2
    with pytest.raises(ContextError) as caught:
        env[4].resume([automatic], preview_token=preview["preview_token"])
    assert caught.value.code == "processing_authorization_required"
    env[4].resume([automatic], preview_token=preview["preview_token"], allow_model_calls=True)
    sent = requests(monkeypatch)
    worker(env)
    assert [p["model"] for p in sent] == ["vision-before", "summary-before"]


def test_resume_preview_invalidated_by_switch_and_cancel(env):
    enable(env)
    prepared(env)
    automatic = env[4].admit_new()[0]
    env[4].configure(False)
    enable(env)
    preview = env[4].resume_preview([automatic])
    env[4].configure(False)
    enable(env)
    with pytest.raises(ContextError) as caught:
        env[4].resume([automatic], preview_token=preview["preview_token"], allow_model_calls=True)
    assert caught.value.code == "resume_preview_changed"
    env[3].executor.jobs.cancel(automatic)
    with pytest.raises(ContextError):
        env[4].resume_preview([automatic])


def test_disable_after_resume_clears_selected_backlog_authority(env):
    enable(env)
    prepared(env)
    automatic = env[4].admit_new()[0]
    env[4].configure(False)
    enable(env)
    preview = env[4].resume_preview([automatic])
    env[4].resume([automatic], preview_token=preview["preview_token"], allow_model_calls=True)
    assert execution_allowed(env[0].snapshot(), env[3].executor.jobs.get(automatic))
    env[4].configure(False)
    assert not execution_allowed(env[0].snapshot(), env[3].executor.jobs.get(automatic))


def test_new_task_cap_is_total_not_reset_by_polling_and_auto_does_not_switch_models(env, monkeypatch):
    enable(env, max_new_tasks=1)
    configure(env[0], env[2], "vision", "vision-after")
    for native in ("201", "202", "203"):
        prepared(env, native)
    sent = requests(monkeypatch)
    worker(env)
    worker(env)
    assert len(env[0].snapshot()["jobs"]) == 1 and len(sent) == 2
    assert [p["model"] for p in sent] == ["vision-before", "summary-before"]
    assert env[4].status()["remaining_new_tasks"] == 0


def test_input_refresh_or_failed_task_is_not_automatic_retry_of_same_work(env, monkeypatch):
    enable(env)
    item, _ = prepared(env)
    automatic = env[4].admit_new()[0]
    env[3].executor.jobs.cancel(automatic)
    env[0].upsert(
        {"native_id": "201", "media_type": "image", "title": "更新原文"}, kind="saved", scope_id="s_saved"
    )
    PreparedInputs(env[0]).prepare_images(item["id"], [(PNG, "image/png"), (PNG, "image/png")])
    sent = requests(monkeypatch)
    worker(env)
    assert not sent and len(env[0].snapshot()["jobs"]) == 1


def test_bad_preparation_is_visible_once_and_does_not_starve_other_tasks(env, monkeypatch):
    enable(env)
    bad, identity = prepared(env)
    original = env[0].snapshot()["prepared_inputs"][identity]
    env[0].files.write(original["path"], b"{}", replace=True)
    prepared(env, "202")
    sent = requests(monkeypatch)
    worker(env)
    assert len(sent) == 2 and env[4].status()["preparation_blocked_count"] == 1
    assert env[4].status()["preparation_issues"][0]["material_ref"] == bad["id"]
    generation = env[0].snapshot()["generation"]
    worker(env)
    assert env[0].snapshot()["generation"] == generation


def test_policy_changes_during_admission_do_not_create_or_charge_task(env, monkeypatch):
    enable(env)
    prepared(env)
    real = env[3].prepare_plan

    def changed(*args, **kwargs):
        result = real(*args, **kwargs)
        env[4].configure(False)
        return result

    monkeypatch.setattr(env[3], "prepare_plan", changed)
    assert env[4].admit_new() == [] and not env[0].snapshot()["jobs"]


def test_policy_pause_between_plan_reconstruction_and_start_prevents_dispatch(env, monkeypatch):
    enable(env)
    prepared(env)
    job = env[4].admit_new()[0]
    original = env[3]._build

    def changed(*args, **kwargs):
        stages = original(*args, **kwargs)
        env[4].configure(False)
        return stages

    monkeypatch.setattr(env[3], "_build", changed)
    sent = requests(monkeypatch)
    with pytest.raises(ContextError) as caught:
        env[3].run(job)
    assert caught.value.code == "automatic_processing_paused" and not sent
    assert env[3].executor.jobs.get(job)["state"] == "queued"


def test_pause_during_active_request_finishes_current_not_next(env, monkeypatch):
    enable(env)
    for native in ("201", "202"):
        prepared(env, native)
    env[4].admit_new()
    sent = requests(monkeypatch, lambda calls: env[4].configure(False) if len(calls) == 1 else None)
    worker(env)
    assert len(sent) == 2
    assert sorted(j["state"] for j in env[0].snapshot()["jobs"].values()) == ["queued", "succeeded"]


def test_history_batch_atomic_idempotent_and_explicit_even_when_auto_disabled(env, monkeypatch):
    ids = [prepared(env, native)[1] for native in ("201", "202")]
    before = env[0].snapshot()
    with pytest.raises(ContextError):
        env[4].history(ids, idempotency_key="history", max_calls=2)
    assert env[0].snapshot() == before
    batch = env[4].history(ids, idempotency_key="history", max_calls=2, allow_model_calls=True)
    assert batch["max_calls_total"] == 4
    assert env[4].history(ids, idempotency_key="history", max_calls=2, allow_model_calls=True) == batch
    sent = requests(monkeypatch)
    worker(env)
    assert len(sent) == 4 and len(env[0].snapshot()["jobs"]) == 2
    assert all(env[3].executor.jobs.get(ref)["state"] == "succeeded" for ref in batch["job_ids"])


def test_bad_history_member_or_disk_commit_never_partially_queues_batch(env, monkeypatch):
    identity = prepared(env)[1]
    with pytest.raises(ContextError):
        env[4].history([identity, "u_missing"], idempotency_key="bad", max_calls=2, allow_model_calls=True)
    assert not env[0].snapshot()["jobs"] and not env[0].snapshot().get("history_batches")
    original = env[0]._publish
    monkeypatch.setattr(
        env[0],
        "_publish",
        lambda *a, **k: (_ for _ in ()).throw(ContextError("storage_unavailable", "fixture")),
    )
    with pytest.raises(ContextError):
        env[4].history([identity], idempotency_key="bad", max_calls=2, allow_model_calls=True)
    monkeypatch.setattr(env[0], "_publish", original)
    assert not env[0].snapshot()["jobs"] and not env[0].snapshot().get("history_batches")


def test_history_conflicting_reuse_and_batch_cancel_preserve_confirmed_usage(env, monkeypatch):
    ids = [prepared(env, native)[1] for native in ("201", "202")]
    batch = env[4].history(ids, idempotency_key="history", max_calls=2, allow_model_calls=True)
    with pytest.raises(ContextError) as caught:
        env[4].history(ids[::-1], idempotency_key="history", max_calls=2, allow_model_calls=True)
    assert caught.value.code == "idempotency_conflict"
    sent = requests(monkeypatch, lambda _: env[4].cancel_history(batch["id"]))
    worker(env)
    assert len(sent) == 1
    jobs = [env[3].executor.jobs.get(ref) for ref in batch["job_ids"]]
    assert all(j["state"] == "cancelled" for j in jobs)
    assert jobs[0]["calls"][0]["state"] == "completed" and jobs[0]["calls"][0]["usage"] == {"total_tokens": 5}


@pytest.mark.parametrize("selection", [[], ["u_a"] * 21, ["u_a", "u_a"], [{}]])
def test_history_bounds_and_invalid_selection_have_no_jobs(env, selection):
    with pytest.raises(ContextError):
        env[4].history(selection, idempotency_key="invalid", max_calls=2, allow_model_calls=True)
    assert not env[0].snapshot()["jobs"]


@pytest.mark.parametrize("kwargs", [{"max_calls": True}, {"max_calls": -1}, {"max_new_tasks": 0}])
def test_automatic_budget_invalid_before_setting_changes(env, kwargs):
    with pytest.raises(ContextError):
        env[4].configure(True, allow_model_calls=True, **kwargs)
    assert not env[4].status()["enabled"]


def test_forged_automatic_origin_is_not_fee_authority(env, monkeypatch):
    enabled = enable(env)
    _, identity = prepared(env)
    payload = env[3].prepare_plan(identity)
    payload["dispatch"] = {"mode": "automatic", "policy_id": enabled["policy_id"]}
    job = env[3].executor.jobs.submit("process", payload, idempotency_key="forged", max_calls=2)
    sent = requests(monkeypatch)
    with pytest.raises(ContextError) as caught:
        env[3].run(job["id"])
    assert caught.value.code == "invalid_dispatch_policy" and not sent


def test_cli_policy_and_history_do_not_read_credentials_or_request_models(env, capsys):
    identity = prepared(env)[1]
    root = str(env[0].files.root)
    assert main(["--workspace", root, "configure-auto", "--enabled", "yes"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "processing_authorization_required"
    assert (
        main(
            [
                "--workspace",
                root,
                "configure-auto",
                "--enabled",
                "yes",
                "--max-calls",
                "2",
                "--allow-model-calls",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["max_new_tasks"] == 5
    assert (
        main(
            [
                "--workspace",
                root,
                "submit-history",
                "--input-id",
                identity,
                "--max-calls",
                "2",
                "--idempotency-key",
                "cli",
                "--allow-model-calls",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["max_calls_total"] == 2
