"""Durable stages, real kernel ownership and a real local HTTP request/crash boundary."""

from __future__ import annotations

import dataclasses
import json
import os
import select
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError, digest
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.workflows.executor import DurableExecutor, Stage, StageOutcome
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path / "独立任务库")
    yield value
    value.close()


def stage(name="audio", **kwargs):
    return Stage(
        name,
        "original_input_hash",
        "original_stage_v1",
        kwargs.pop("invoke", lambda deps: StageOutcome({"text": "原创合成结果"})),
        **kwargs,
    )


def test_executor_reuses_committed_stage_across_new_jobs_and_counts_only_dispatched_calls(store):
    invoked = []
    stages = [
        stage(
            invoke=lambda deps: (
                invoked.append("audio") or StageOutcome({"text": "已确认音频"}, usage={"total_tokens": 12})
            ),
            paid=True,
        )
    ]
    executor = DurableExecutor(store)
    first = executor.submit(stages, idempotency_key="first", max_calls=1)
    result = executor.run(first["id"], stages)
    assert result["state"] == "succeeded" and len(result["calls"]) == 1
    assert result["calls"][0]["usage"] == {"total_tokens": 12}
    second = executor.submit(stages, idempotency_key="second", max_calls=0)
    reused = executor.run(second["id"], stages)
    assert reused["state"] == "succeeded" and reused["calls"] == []
    assert invoked == ["audio"]
    assert reused["stages"]["audio"]["result"] == result["stages"]["audio"]["result"]


def test_profile_or_input_plan_change_never_hot_switches_running_job(store):
    executor = DurableExecutor(store)
    original = [stage(paid=True)]
    job = executor.submit(original, idempotency_key="fixed", max_calls=1)
    with pytest.raises(ContextError) as caught:
        executor.run(job["id"], [dataclasses.replace(original[0], input_hash="new_model_or_media")])
    assert caught.value.code == "plan_changed"
    assert executor.jobs.get(job["id"])["state"] == "queued"


def test_budget_zero_never_enters_paid_processor(store):
    executor = DurableExecutor(store)
    stages = [stage(paid=True, invoke=lambda deps: pytest.fail("No model dispatch allowed"))]
    job = executor.submit(stages, idempotency_key="no-fee")
    result = executor.run(job["id"], stages)
    assert result["state"] == "blocked" and result["calls"] == []
    assert result["error"]["code"] == "budget_required"
    assert result["stages"]["audio"]["state"] == "blocked"


def test_preflight_failure_has_no_intent_or_model_call(store):
    def validation(deps):
        raise ContextError("summary_input_limit", "输入超出能力上限。")

    executor = DurableExecutor(store)
    stages = [
        stage(paid=True, validate=validation, invoke=lambda deps: pytest.fail("Preflight must run first"))
    ]
    job = executor.submit(stages, idempotency_key="validate", max_calls=1)
    result = executor.run(job["id"], stages)
    assert result["state"] == "failed" and result["calls"] == []


def test_dependency_failure_does_not_invent_complete_summary(store):
    def failure(deps):
        raise ContextError("local_media_failed", "本地失败。")

    executor = DurableExecutor(store)
    stages = [
        stage("prepare", invoke=failure),
        stage(
            "audio",
            dependencies=("prepare",),
            paid=True,
            invoke=lambda deps: pytest.fail("Do not fill missing audio"),
        ),
    ]
    result = executor.run(executor.submit(stages, idempotency_key="missing", max_calls=1)["id"], stages)
    assert result["state"] == "failed" and result["calls"] == []
    assert result["stages"]["audio"]["error"]["code"] == "dependency_incomplete"


def test_summary_can_explicitly_use_partial_evidence_and_inherit_gap(store):
    seen = []
    executor = DurableExecutor(store)
    stages = [
        stage("audio", invoke=lambda deps: StageOutcome({"text": "截断转写"}, status="partial"), paid=True),
        stage(
            "summary",
            dependencies=("audio",),
            paid=True,
            allow_partial_dependencies=True,
            invoke=lambda deps: (
                seen.append(deps) or StageOutcome({"text": "明确音频有缺口"}, status="partial")
            ),
        ),
    ]
    job = executor.run(executor.submit(stages, idempotency_key="partial", max_calls=2)["id"], stages)
    assert job["state"] == "partial" and len(job["calls"]) == 2
    assert seen[0]["audio"]["status"] == "partial"


def test_unknown_request_blocks_new_job_with_same_stage_without_redispatch(store):
    calls = []

    def unknown(deps):
        calls.append(1)
        raise ContextError("upstream_outcome_unknown", "网络中断。", possibly_charged=True)

    executor = DurableExecutor(store)
    stages = [stage(paid=True, invoke=unknown)]
    first = executor.run(executor.submit(stages, idempotency_key="unknown-1", max_calls=1)["id"], stages)
    second = executor.run(executor.submit(stages, idempotency_key="unknown-2", max_calls=1)["id"], stages)
    assert first["state"] == second["state"] == "blocked"
    assert first["calls"][0]["state"] == "unknown" and second["calls"] == []
    assert calls == [1]


def test_cancel_during_request_keeps_late_result_and_usage_and_stops_followup(store):
    executor = DurableExecutor(store)
    holder = {}

    def response_after_cancel(deps):
        executor.jobs.cancel(holder["id"])
        return StageOutcome(
            {"text": "已发出请求的晚到结果"}, actual_model="returned-model", usage={"cached_tokens": 7}
        )

    stages = [
        stage(paid=True, invoke=response_after_cancel),
        stage(
            "summary",
            dependencies=("audio",),
            paid=True,
            invoke=lambda deps: pytest.fail("No next request after cancellation"),
        ),
    ]
    holder.update(executor.submit(stages, idempotency_key="cancel", max_calls=2))
    result = executor.run(holder["id"], stages)
    assert result["state"] == "cancelled" and len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["state"] == "completed" and call["actual_model"] == "returned-model"
    assert call["usage"] == {"cached_tokens": 7}
    assert executor.jobs.read_result(call["result"])["output"]["text"] == "已发出请求的晚到结果"


def test_result_confirmed_before_stage_checkpoint_can_resume_without_fee(store, monkeypatch):
    executor = DurableExecutor(store)
    stages = [stage(paid=True)]
    job = executor.submit(stages, idempotency_key="commit-gap", max_calls=1)
    commit = executor.jobs.commit_stage_result

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("Simulated process termination, not a recoverable provider exception")

    monkeypatch.setattr(executor.jobs, "commit_stage_result", crash)
    with pytest.raises(KeyboardInterrupt):
        executor.run(job["id"], stages)
    original = executor.jobs.get(job["id"])
    assert original["state"] == "running" and original["calls"][0]["state"] == "completed"
    monkeypatch.setattr(executor.jobs, "commit_stage_result", commit)
    no_dispatch = [
        dataclasses.replace(stages[0], invoke=lambda deps: pytest.fail("Confirmed output must be reused"))
    ]
    resumed = executor.run(job["id"], no_dispatch)
    assert resumed["state"] == "succeeded" and len(resumed["calls"]) == 1


def test_corrupt_confirmed_result_does_not_repair_by_billing_again(store):
    executor = DurableExecutor(store)
    stages = [stage(paid=True)]
    first = executor.run(executor.submit(stages, idempotency_key="confirmed", max_calls=1)["id"], stages)
    descriptor = first["calls"][0]["result"]
    store.files.write(descriptor["path"], b"{}", replace=True)
    second = executor.run(executor.submit(stages, idempotency_key="corrupt", max_calls=1)["id"], stages)
    assert second["state"] == "blocked" and second["calls"] == []
    assert second["error"]["code"] == "corrupt_call_result"


def test_executor_scope_does_not_recover_other_principals(store):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="other", principal="other_owner")
    jobs.start(job["id"], principal="other_owner")
    executor = DurableExecutor(store)
    stages = [stage()]
    own = executor.submit(stages, idempotency_key="own")
    assert executor.run(own["id"], stages)["state"] == "succeeded"
    assert jobs.get(job["id"], principal="other_owner")["state"] == "running"


def test_lease_blocks_other_executor_and_detects_replacement(store):
    with ExecutorLease(store.files.root) as owner:
        with pytest.raises(ContextError) as caught:
            ExecutorLease(store.files.root)
        assert caught.value.code == "executor_busy"
        store.files.write(owner.path, b"{}", replace=True)
        with pytest.raises(ContextError) as caught:
            owner.check()
        assert caught.value.code == "executor_ownership_changed"


def test_recovery_requires_live_ownership_of_correct_library(store, tmp_path):
    other = LibraryStore.initialize(tmp_path / "other")
    try:
        with ExecutorLease(other.files.root) as lease:
            with pytest.raises(ContextError) as caught:
                JobManager(store).recover_interrupted(lease=lease)
            assert caught.value.code == "executor_not_owned"
        with pytest.raises(ContextError) as caught:
            JobManager(store).recover_interrupted(lease=lease)
        assert caught.value.code == "executor_not_owned"
    finally:
        other.close()


def child_environment():
    return {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}


def test_kernel_releases_real_process_lease_after_death_not_metadata_guess(store):
    code = "import sys; from pathlib import Path; from collection_context.infrastructure.ownership import ExecutorLease; lease=ExecutorLease(Path(sys.argv[1])); print('owned',flush=True); sys.stdin.read(1)"
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(store.files.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=child_environment(),
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "owned"
        with pytest.raises(ContextError) as caught:
            ExecutorLease(store.files.root)
        assert caught.value.code == "executor_busy"
        process.kill()
        assert process.wait(timeout=5) < 0
        assert (store.files.root / ".context/执行所有权.lock").exists()
        with ExecutorLease(store.files.root) as lease:
            lease.check()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def test_real_http_response_then_process_exit_never_redispatches(store):
    received = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            payload = json.dumps(
                {
                    "model": "synthetic-returned-model",
                    "choices": [
                        {"message": {"content": "原创协议夹具，不是真实转写"}, "finish_reason": "stop"}
                    ],
                    "usage": {"total_tokens": 12},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    executor = DurableExecutor(store)
    stages = [stage(paid=True)]
    job = executor.submit(stages, idempotency_key="real-http-crash", max_calls=1)
    code = """import os,sys
from pathlib import Path
from collection_context.library.store import LibraryStore
from collection_context.processing.models import CloudModelClient,ModelProfile
from collection_context.workflows.executor import DurableExecutor,Stage
store=LibraryStore(Path(sys.argv[1]))
def invoke(deps):
 CloudModelClient(ModelProfile(sys.argv[3],"synthetic-model","synthetic-fixture-key")).text("原创协议测试")
 os._exit(7)
DurableExecutor(store).run(sys.argv[2],[Stage("audio","original_input_hash","original_stage_v1",invoke,paid=True)])
"""
    try:
        child = subprocess.run(
            [sys.executable, "-c", code, str(store.files.root), job["id"], endpoint],
            env=child_environment(),
            capture_output=True,
            timeout=15,
        )
        assert child.returncode == 7 and len(received) == 1
        assert executor.jobs.get(job["id"])["calls"][0]["state"] == "intent"
        result = executor.run(
            job["id"],
            [dataclasses.replace(stages[0], invoke=lambda deps: pytest.fail("Do not duplicate request"))],
        )
        assert result["state"] == "blocked" and result["error"]["possibly_charged"]
        assert len(received) == 1 and len(result["calls"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_result_and_actual_usage_are_committed_together(store):
    executor = DurableExecutor(store)
    stages = [
        stage(
            paid=True, invoke=lambda deps: StageOutcome({"text": "协议返回"}, actual_model=None, usage=None)
        )
    ]
    result = executor.run(executor.submit(stages, idempotency_key="unknown-usage", max_calls=1)["id"], stages)
    call = result["calls"][0]
    assert call["usage"] is None and call["actual_model"] is None
    saved = executor.jobs.read_result(call["result"])
    assert saved["usage"] is None and saved["actual_model"] is None
    assert call["result"] == result["stages"]["audio"]["result"]


def test_result_pointer_cannot_read_another_file(store):
    with pytest.raises(ContextError) as caught:
        JobManager(store).read_result(
            {"call_id": "x_fixture", "path": "context-workspace.json", "sha256": digest({})}
        )
    assert caught.value.code == "corrupt_call_result"
