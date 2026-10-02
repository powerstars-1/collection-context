"""Paid call boundaries remain durable while redundant manifest writes are removed."""

from __future__ import annotations

import copy

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.workflows.executor import DurableExecutor, Stage, StageOutcome
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path / "付费阶段隔离库")
    try:
        yield value
    finally:
        value.close()


def begin(store, *, start_stage=True):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="original", max_calls=1)
    jobs.start(job["id"])
    call = jobs.begin_call(
        job["id"],
        stage="vision",
        input_hash="input-hash",
        processor_version="p1",
        start_stage=start_stage,
    )
    return jobs, job["id"], call


def result():
    return StageOutcome(
        {"text": "原创画面"},
        usage={"total_tokens": 5},
        actual_model="fixture",
        upstream_request_id="request-fixture",
    ).record()


def fails(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def test_original_executor_publishes_intent_and_completion_as_two_atomic_boundaries(store, monkeypatch):
    commits = []
    publish = store._publish

    def observed(files, state, **kwargs):
        publish(files, state, **kwargs)
        commits.append(copy.deepcopy(state))

    executor = DurableExecutor(store)
    invoked = []

    def invoke(_):
        current = executor.jobs.get(job["id"])
        assert current["state"] == "running"
        assert len(current["calls"]) == 1 and current["calls"][0]["state"] == "intent"
        assert current["stages"]["vision"]["state"] == "running"
        assert current["stages"]["vision"]["input_hash"] == current["calls"][0]["input_hash"]
        invoked.append(True)
        return StageOutcome({"text": "原创结果"}, usage={"total_tokens": 5}, actual_model="fixture")

    stages = [Stage("vision", "input", "p1", invoke, paid=True)]
    job = executor.submit(stages, idempotency_key="one", max_calls=1)
    monkeypatch.setattr(store, "_publish", observed)
    finished = executor.run(job["id"], stages)
    assert invoked == [True] and finished["state"] == "succeeded"
    with_calls = [state["jobs"][job["id"]] for state in commits if state["jobs"][job["id"]]["calls"]]
    assert [current["calls"][0]["state"] for current in with_calls] == ["intent", "completed", "completed"]
    assert [current["stages"]["vision"]["state"] for current in with_calls] == ["running", "ready", "ready"]
    stage_descriptor = finished["stages"]["vision"]["result"]
    assert stage_descriptor == finished["calls"][0]["result"]
    assert executor.jobs.read_result(stage_descriptor)["usage"] == {"total_tokens": 5}
    # No change to recovery/start/finish commits; only the two paid boundaries are coalesced.
    assert len(commits) == 5


def test_cancellation_retains_late_paid_result_but_does_not_mark_stage_ready(store):
    jobs, identity, call = begin(store)
    jobs.cancel(identity)
    descriptor = jobs.commit_call_result(identity, call["id"], result(), complete_stage=True)
    current = jobs.get(identity)
    assert current["state"] == "cancelled" and current["stages"]["vision"]["state"] == "cancelled"
    assert current["calls"][0]["state"] == "completed"
    assert current["calls"][0]["usage"] == {"total_tokens": 5}
    assert jobs.read_result(descriptor) == result()


def test_executor_authority_loss_after_response_retains_usage_without_ready_stage(store, monkeypatch):
    executor = DurableExecutor(store)
    received = []
    check = executor._check

    def require_authority(*args):
        if received:
            raise ContextError("executor_not_owned", "original fixture")
        return check(*args)

    def invoke(_):
        received.append(True)
        return StageOutcome({"text": "已返回结果"}, usage={"total_tokens": 5})

    monkeypatch.setattr(executor, "_check", require_authority)
    stages = [Stage("vision", "input", "p1", invoke, paid=True)]
    job = executor.submit(stages, idempotency_key="authority", max_calls=1)
    finished = executor.run(job["id"], stages)
    assert finished["state"] == "blocked" and finished["error"]["code"] == "executor_not_owned"
    assert finished["stages"]["vision"]["state"] == "blocked"
    assert finished["stages"]["vision"]["result"] is None
    assert received == [True] and finished["calls"][0]["state"] == "completed"
    assert finished["calls"][0]["usage"] == {"total_tokens": 5}
    assert executor.jobs.read_result(finished["calls"][0]["result"])["output"]["text"] == "已返回结果"


def test_interruption_after_atomic_checkpoint_reuses_both_result_and_stage_without_fee(store, monkeypatch):
    executor = DurableExecutor(store)
    dispatched = []

    def invoke(_):
        dispatched.append(True)
        return StageOutcome({"text": "原创结果"}, usage={"total_tokens": 5})

    stages = [Stage("vision", "input", "p1", invoke, paid=True)]
    job = executor.submit(stages, idempotency_key="atomic-recovery", max_calls=1)
    commit = executor.jobs.commit_call_result

    def interrupted(*args, **kwargs):
        commit(*args, **kwargs)
        raise KeyboardInterrupt("original fixture")

    monkeypatch.setattr(executor.jobs, "commit_call_result", interrupted)
    with pytest.raises(KeyboardInterrupt):
        executor.run(job["id"], stages)
    current = executor.jobs.get(job["id"])
    assert current["state"] == "running" and current["stages"]["vision"]["state"] == "ready"
    assert current["calls"][0]["state"] == "completed" and current["calls"][0]["usage"] == {"total_tokens": 5}
    monkeypatch.setattr(executor.jobs, "commit_call_result", commit)
    finished = executor.run(job["id"], stages)
    assert finished["state"] == "succeeded" and len(finished["calls"]) == 1
    assert dispatched == [True]


def test_stage_identity_conflict_still_commits_known_usage_and_output(store):
    jobs, identity, call = begin(store)

    def change(state):
        state["jobs"][identity]["stages"]["vision"]["input_hash"] = "changed"

    store.transact(change)
    fails(
        "stage_input_changed",
        lambda: jobs.commit_call_result(identity, call["id"], result(), complete_stage=True),
    )
    current = jobs.get(identity)
    assert current["calls"][0]["state"] == "completed" and current["calls"][0]["usage"] == {"total_tokens": 5}
    assert current["stages"]["vision"]["input_hash"] == "changed"
    assert current["stages"]["vision"]["state"] == "running"
    assert jobs.read_result(current["calls"][0]["result"]) == result()


def test_completion_commit_failure_leaves_intent_and_blocks_recovery_without_recharge(store, monkeypatch):
    jobs, identity, call = begin(store)
    publish = store._publish

    def fail_confirmation(files, state, **kwargs):
        if state["jobs"][identity]["calls"][0]["state"] == "completed":
            raise ContextError("fixture_publish_failure", "fixture")
        return publish(files, state, **kwargs)

    monkeypatch.setattr(store, "_publish", fail_confirmation)
    fails(
        "fixture_publish_failure",
        lambda: jobs.commit_call_result(identity, call["id"], result(), complete_stage=True),
    )
    current = jobs.get(identity)
    assert current["calls"][0]["state"] == "intent" and current["stages"]["vision"]["state"] == "running"
    assert "result" not in current["calls"][0]
    monkeypatch.setattr(store, "_publish", publish)
    with ExecutorLease(store.files.root) as lease:
        jobs.recover_interrupted(lease=lease)
    current = jobs.get(identity)
    assert current["state"] == "blocked" and current["error"]["possibly_charged"]
    assert len(current["calls"]) == 1 and current["calls"][0]["usage"] is None


def test_combined_checkpoint_still_reads_and_verifies_actual_result_file(store, monkeypatch):
    jobs, identity, call = begin(store)
    write = store.files.write

    def changed_after_write(path, body, **kwargs):
        write(path, body, **kwargs)
        if path == f".context/请求结果/{call['id']}.json":
            write(path, b'{"not":"the confirmed result"}', replace=True)

    monkeypatch.setattr(store.files, "write", changed_after_write)
    with pytest.raises(ContextError) as caught:
        jobs.commit_call_result(identity, call["id"], result(), complete_stage=True)
    assert caught.value.code == "corrupt_call_result" and caught.value.possibly_charged
    current = jobs.get(identity)
    assert current["calls"][0]["state"] == "intent" and current["calls"][0]["usage"] is None
    assert current["stages"]["vision"]["state"] == "running"


def test_intent_failure_does_not_publish_a_running_stage_without_call_or_dispatch(store, monkeypatch):
    executor = DurableExecutor(store)
    invoked = []
    stages = [Stage("vision", "input", "p1", lambda _: invoked.append(True), paid=True)]
    job = executor.submit(stages, idempotency_key="one", max_calls=1)
    publish = store._publish

    def fail_intent(files, state, **kwargs):
        if state["jobs"][job["id"]]["calls"]:
            raise ContextError("fixture_publish_failure", "fixture")
        return publish(files, state, **kwargs)

    monkeypatch.setattr(store, "_publish", fail_intent)
    finished = executor.run(job["id"], stages)
    assert finished["state"] == "blocked" and finished["error"]["code"] == "fixture_publish_failure"
    assert invoked == [] and finished["calls"] == [] and finished["stages"] == {}


def test_existing_job_manager_default_protocol_is_unchanged(store):
    jobs, identity, call = begin(store, start_stage=False)
    assert jobs.get(identity)["stages"] == {}
    descriptor = jobs.commit_call_result(identity, call["id"], result())
    assert jobs.get(identity)["stages"] == {} and jobs.read_result(descriptor) == result()


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_invalid_start_policy_does_not_change_committed_state(store, flag):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="original", max_calls=1)
    jobs.start(job["id"])
    before = store.snapshot()
    fails(
        "invalid_argument",
        lambda: jobs.begin_call(
            job["id"], stage="vision", input_hash="h", processor_version="p1", start_stage=flag
        ),
    )
    assert store.snapshot() == before


@pytest.mark.parametrize("flag", [None, 1, "true"])
def test_invalid_completion_policy_does_not_change_committed_state(store, flag):
    jobs, identity, call = begin(store)
    before = store.snapshot()
    fails(
        "invalid_argument",
        lambda: jobs.commit_call_result(identity, call["id"], result(), complete_stage=flag),
    )
    assert store.snapshot() == before
