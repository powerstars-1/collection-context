"""Own serial background queue runner; not a web callback or implicit fee authority."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.storage import KernelLease
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.policy import execution_allowed
from collection_context.workflows.scheduling import ProcessingSchedule
from collection_context.workflows.source_schedule import SourceSchedule

if TYPE_CHECKING:
    from collection_context.workflows.synchronization import SynchronizationWorkflow

# Contention is retried on a bounded poll, not interpreted as a dead owner.
WAIT_CODES = {"executor_busy", "writer_busy", "automatic_processing_paused"}
# Integrity and storage failures stop the worker instead of marking every queued job blocked.
FATAL_CODES = {
    "storage_unavailable",
    "forbidden_path",
    "invalid_workspace",
    "workspace_limit",
    "lock_changed",
    "writer_unavailable",
    "writer_upgrade_required",
    "corrupt_workspace",
    "writer_not_owned",
    "executor_ownership_changed",
    "executor_not_owned",
    "executor_unavailable",
    "worker_ownership_changed",
    "worker_not_owned",
}


class BackgroundWorker:
    """One OS-owned polling process per library; one extraction at a time.

    It executes local-owner registered extraction jobs and separately authorized fixed source jobs. Starting the process is
    explicit authority to execute those previously submitted, individually budgeted
    jobs, not permission to read arbitrary sources or retry terminal jobs. Model and source authority are separate.
    """

    def __init__(
        self,
        workflow: ExtractionWorkflow,
        sync_workflow: SynchronizationWorkflow | None = None,
        *,
        addition_authority: Callable[[str], None] | None = None,
    ):
        self.workflow = workflow
        self.store = workflow.store
        if sync_workflow is not None and sync_workflow.store is not self.store:
            raise ContextError("invalid_workspace", "同步与处理必须属于同一个资料库。")
        self.sync_workflow = sync_workflow
        self.addition = AdditionWorkflow(
            self.store,
            sync_workflow.source_factory if sync_workflow else None,
            agent_authority=addition_authority,
            runtime_dir=sync_workflow.runtime_dir if sync_workflow else None,
        )

    def _drain(
        self,
        lease: KernelLease,
        stop: threading.Event,
        limit: int,
        emit: Callable[[dict[str, Any]], None],
        allow_model_calls: bool,
        allow_source_sync: bool,
    ) -> dict[str, Any]:
        lease.check()
        with self.store.storage.executor(
            self.store.files.root, expected_identity=self.store.files.identity
        ) as executor:
            snapshot = self.store.snapshot()
            interrupted = any(
                job["state"] == "running" and job["principal"] == "local_owner"
                for job in self.store.snapshot()["jobs"].values()
            )
            recovered = (
                self.workflow.executor.jobs.recover_interrupted(lease=executor, principal="local_owner")
                if interrupted
                else []
            )
            agent_running = (
                {
                    j["id"]
                    for j in snapshot["jobs"].values()
                    if j["state"] == "running" and self.addition.admitted(snapshot, j)
                }
                if allow_source_sync and self.addition.agent_authority is not None
                else set()
            )
            if agent_running:
                recovered.extend(
                    self.workflow.executor.jobs.recover_interrupted(lease=executor, job_ids=agent_running)
                )
        for job in recovered:
            emit({"event": "recovered", "job_id": job["id"], "state": job["state"]})
        if stop.is_set():
            return {"handled": 0, "states": {}}
        if allow_source_sync:
            assert self.sync_workflow is not None
            for ref in SourceSchedule(self.sync_workflow).admit_due(limit=5):
                emit({"event": "source_sync_job_admitted", "job_id": ref})
        for ref in ProcessingSchedule(self.workflow).admit_new(limit=5) if allow_model_calls else []:
            emit({"event": "automatic_job_admitted", "job_id": ref})
        state = self.store.snapshot()
        pending = [
            job
            for job in state["jobs"].values()
            if (
                job["principal"] == "local_owner"
                or (
                    allow_source_sync
                    and self.addition.agent_authority is not None
                    and self.addition.admitted(state, job)
                )
            )
            and job["state"] == "queued"
            and (
                allow_model_calls
                and job["kind"] == "process"
                and "extraction" in job["payload"]
                or allow_source_sync
                and job["kind"] == "sync"
                and "sync" in job["payload"]
                or allow_source_sync
                and job["kind"] == "add"
                and "link" in job["payload"]
            )
            and execution_allowed(state, job)
        ]
        pending.sort(key=lambda job: (job["created_at"], job["id"]))
        counts: dict[str, int] = {}
        handled = 0
        for job in pending[:limit]:
            if stop.is_set():
                break
            lease.check()
            try:
                if job["kind"] == "add":
                    result = self.addition.run(job["id"], principal=job["principal"])
                elif job["kind"] == "sync":
                    assert self.sync_workflow is not None
                    result = self.sync_workflow.run(job["id"])
                else:
                    result = self.workflow.run(job["id"])
            except ContextError as error:
                if error.code in WAIT_CODES | FATAL_CODES:
                    raise
                result = self.workflow.executor.jobs.block_queued(
                    job["id"], error, principal=job["principal"]
                )
            lease.check()
            state = result["state"]
            counts[state] = counts.get(state, 0) + 1
            handled += 1
            # No body, model credentials, payload or raw upstream response in lifecycle logs.
            emit(
                {
                    "event": "job_finished",
                    "job_id": job["id"],
                    "state": state,
                    "error_code": (result.get("error") or {}).get("code"),
                }
            )
        return {"handled": handled, "states": counts}

    def serve(
        self,
        *,
        allow_model_calls: bool,
        allow_source_sync: bool = False,
        once: bool = False,
        poll_seconds: float = 5,
        max_jobs: int = 1,
        stop: threading.Event | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if type(allow_model_calls) is not bool or type(allow_source_sync) is not bool:
            raise ContextError("invalid_argument", "后台同步和模型授权须为明确的布尔值。")
        if not allow_model_calls and not allow_source_sync:
            raise ContextError(
                "processing_authorization_required", "启动后台须明确允许已排队任务上传媒体并计费。"
            )
        if allow_source_sync and self.sync_workflow is None:
            raise ContextError("source_setup_required", "同步后台需要显式配置独立来源连接。")
        if type(once) is not bool or type(max_jobs) is not int or not 1 <= max_jobs <= 20:
            raise ContextError("invalid_argument", "每轮处理上限须为1到20条。")
        if type(poll_seconds) not in {int, float} or not 0.1 <= poll_seconds <= 60:
            raise ContextError("invalid_argument", "后台轮询间隔须在0.1到60秒之间。")
        stop = stop if stop is not None else threading.Event()
        emit = emit if emit is not None else lambda _: None
        total = 0
        with self.store.storage.worker(
            self.store.files.root,
            model_calls=allow_model_calls,
            source_sync=allow_source_sync,
            expected_identity=self.store.files.identity,
        ) as lease:
            emit(
                {
                    "event": "worker_started",
                    "concurrency": 1,
                    "automatic_submission": allow_model_calls
                    and self.store.snapshot()["settings"].get("auto_process") is True,
                    "source_sync_authorized": allow_source_sync,
                    "automatic_source_sync": allow_source_sync
                    and self.store.snapshot()["settings"].get("auto_sync") is True,
                    "model_calls_authorized": allow_model_calls,
                }
            )
            while not stop.is_set():
                try:
                    result = self._drain(lease, stop, max_jobs, emit, allow_model_calls, allow_source_sync)
                    total += result["handled"]
                except ContextError as error:
                    if error.code not in WAIT_CODES:
                        raise
                    emit({"event": "waiting", "error_code": error.code})
                if once:
                    break
                stop.wait(poll_seconds)
            lease.check()
            emit({"event": "worker_stopped", "handled": total})
        return {
            "stopped": True,
            "handled": total,
            "automatic_submission": allow_model_calls
            and self.store.snapshot()["settings"].get("auto_process") is True,
        }
