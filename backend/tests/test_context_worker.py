"""Original background execution; fixtures never contact real platforms/providers."""

import json
import threading

import pytest
from test_context_model_registry import configure, prepared, requests

from collection_context.application.contracts import ContextError
from collection_context.cli import main
from collection_context.infrastructure.ownership import ExecutorLease, WorkerLease
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def environment(tmp_path):
    store = LibraryStore.initialize(tmp_path / "独立后台库")
    secrets = FileSecrets.initialize(tmp_path / "独立凭据")
    key = secrets.put("synthetic-key-not-real")
    configure(store, key, "vision", "fixture-vision")
    configure(store, key, "summary", "fixture-summary")
    _, identity = prepared(store)
    workflow = ExtractionWorkflow(store, secrets.get)
    try:
        yield store, secrets, identity, workflow
    finally:
        secrets.close()
        store.close()


def submit(env, key="explicit", **kwargs):
    return env[3].submit(env[2], idempotency_key=key, max_calls=kwargs.pop("max_calls", 3), **kwargs)


def test_explicit_execution_authority_required_before_leases_or_secret_resolution(environment, monkeypatch):
    calls = requests(monkeypatch)
    job = submit(environment)
    with pytest.raises(ContextError) as caught:
        BackgroundWorker(environment[3]).serve(allow_model_calls=False, once=True)
    assert caught.value.code == "processing_authorization_required"
    assert not calls and environment[3].executor.jobs.get(job["id"])["state"] == "queued"
    assert not (environment[0].files.root / WorkerLease.path).exists()


def test_serial_queue_reuses_success_and_never_automatically_submits(environment, monkeypatch):
    sent = requests(monkeypatch)
    first = submit(environment, "first")
    second = submit(environment, "second", max_calls=0)
    events = []
    result = BackgroundWorker(environment[3]).serve(
        allow_model_calls=True, once=True, max_jobs=2, emit=events.append
    )
    assert result["handled"] == 2 and len(sent) == 3
    assert environment[3].executor.jobs.get(first["id"])["state"] == "succeeded"
    assert environment[3].executor.jobs.get(second["id"])["calls"] == []
    assert len(environment[0].snapshot()["jobs"]) == 2
    assert len([e for e in events if e["event"] == "job_finished"]) == 2
    assert "synthetic-key-not-real" not in json.dumps(events)
    assert "payload" not in json.dumps(events)


def test_round_limit_and_other_principal_are_not_global_fee_authority(environment, monkeypatch):
    sent = requests(monkeypatch)
    submit(environment, "first")
    submit(environment, "second")
    remote = submit(environment, "remote", principal="remote_owner")
    assert BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)["handled"] == 1
    jobs = environment[0].snapshot()["jobs"]
    assert sum(j["state"] == "queued" for j in jobs.values()) == 2
    assert jobs[remote["id"]]["state"] == "queued" and len(sent) == 3


def test_graceful_stop_finishes_current_job_but_never_starts_next(environment, monkeypatch):
    stop = threading.Event()
    sent = requests(monkeypatch, lambda _: stop.set())
    first, second = submit(environment, "first"), submit(environment, "second")
    result = BackgroundWorker(environment[3]).serve(
        allow_model_calls=True, max_jobs=20, poll_seconds=0.1, stop=stop
    )
    assert result["handled"] == 1 and len(sent) == 3
    assert environment[3].executor.jobs.get(first["id"])["state"] == "succeeded"
    assert environment[3].executor.jobs.get(second["id"])["state"] == "queued"


def test_cancel_during_request_retains_usage_but_stops_later_dispatch(environment, monkeypatch):
    job = submit(environment)
    sent = requests(monkeypatch, lambda _: environment[3].executor.jobs.cancel(job["id"]))
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
    result = environment[3].executor.jobs.get(job["id"])
    assert result["state"] == "cancelled" and len(sent) == 1
    assert result["calls"][0]["state"] == "completed" and result["calls"][0]["usage"] == {"total_tokens": 5}


def test_reconstruction_failure_blocked_once_without_hot_retry(environment, monkeypatch):
    job = submit(environment)
    environment[0].transact(lambda s: s["jobs"][job["id"]]["payload"]["extraction"].update(workflow="shell"))
    sent = requests(monkeypatch)
    worker = BackgroundWorker(environment[3])
    assert worker.serve(allow_model_calls=True, once=True)["handled"] == 1
    result = environment[3].executor.jobs.get(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "processor_version_changed"
    assert worker.serve(allow_model_calls=True, once=True)["handled"] == 0 and not sent


def test_stale_input_is_blocked_not_polled_forever(environment, monkeypatch):
    job = submit(environment)
    material = environment[0].snapshot()["prepared_inputs"][environment[2]]
    # Change the original content through the same business path, not the task plan.
    assert material
    environment[0].upsert(
        {"native_id": "81", "media_type": "image", "title": "更改后的原文"}, kind="saved", scope_id="s_saved"
    )
    sent = requests(monkeypatch)
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
    result = environment[3].executor.jobs.get(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "version_changed" and not sent


def test_missing_secret_blocks_only_task_and_no_fallback(environment, monkeypatch):
    job = submit(environment)
    environment[3].resolve_secret = lambda _: (_ for _ in ()).throw(
        ContextError("secret_missing", "合成缺失")
    )
    sent = requests(monkeypatch)
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
    result = environment[3].executor.jobs.get(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "secret_missing" and not sent


def test_budget_zero_does_not_dispatch(environment, monkeypatch):
    sent = requests(monkeypatch)
    job = submit(environment, max_calls=0)
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
    assert environment[3].executor.jobs.get(job["id"])["error"]["code"] == "budget_required" and not sent


def test_unresolved_intent_on_restart_is_blocked_without_cloud_request(environment, monkeypatch):
    job = submit(environment)
    manager = environment[3].executor.jobs
    manager.start(job["id"])
    manager.begin_call(job["id"], stage="vision_fixture", input_hash="fixture", processor_version="fixture")
    sent = requests(monkeypatch)
    events = []
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True, emit=events.append)
    assert manager.get(job["id"])["error"]["code"] == "upstream_outcome_unknown" and not sent
    assert any(e["event"] == "recovered" and e["state"] == "blocked" for e in events)


def test_interrupted_before_dispatch_is_requeued_and_completed(environment, monkeypatch):
    job = submit(environment)
    environment[3].executor.jobs.start(job["id"])
    sent = requests(monkeypatch)
    BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
    assert len(sent) == 3 and environment[3].executor.jobs.get(job["id"])["state"] == "succeeded"


def test_second_worker_rejected_and_live_foreground_executor_never_recovered(environment, monkeypatch):
    sent = requests(monkeypatch)
    job = submit(environment)
    with WorkerLease(environment[0].files.root):
        with pytest.raises(ContextError) as caught:
            BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)
        assert caught.value.code == "worker_busy"
    events = []
    with ExecutorLease(environment[0].files.root):
        environment[3].executor.jobs.start(job["id"])
        result = BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True, emit=events.append)
    assert result["handled"] == 0 and not sent
    assert environment[3].executor.jobs.get(job["id"])["state"] == "running"
    assert any(e.get("error_code") == "executor_busy" for e in events)


def test_queue_cancel_and_generic_jobs_are_not_executed(environment, monkeypatch):
    job = submit(environment)
    environment[3].executor.jobs.cancel(job["id"])
    generic = environment[3].executor.jobs.submit("sync", {}, idempotency_key="not-extraction")
    sent = requests(monkeypatch)
    assert BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True)["handled"] == 0
    assert not sent and environment[3].executor.jobs.get(generic["id"])["state"] == "queued"


def test_idle_poll_does_not_create_unbounded_library_commits(environment):
    before = environment[0].snapshot()["generation"]
    worker = BackgroundWorker(environment[3])
    for _ in range(3):
        assert worker.serve(allow_model_calls=True, once=True)["handled"] == 0
    assert environment[0].snapshot()["generation"] == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_jobs": 0},
        {"max_jobs": 21},
        {"max_jobs": True},
        {"poll_seconds": float("nan")},
        {"poll_seconds": 61},
    ],
)
def test_invalid_limits_rejected_before_any_task(environment, kwargs):
    with pytest.raises(ContextError) as caught:
        BackgroundWorker(environment[3]).serve(allow_model_calls=True, once=True, **kwargs)
    assert caught.value.code == "invalid_argument"


def test_cli_authority_gate_and_cancel(environment, capsys):
    root = str(environment[0].files.root)
    assert main(["--workspace", root, "worker", "--credential-dir", "/missing", "--once"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "processing_authorization_required"
    job = submit(environment)
    assert main(["--workspace", root, "cancel-job", "--job-id", job["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "cancelled"
