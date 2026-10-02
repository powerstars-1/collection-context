"""Durable jobs with explicit paid-call intent and conservative recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from collection_context.application.contracts import (
    TERMINAL_STATES,
    ContextError,
    canonical_bytes,
    digest,
    utc_now,
    valid_id,
)
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.workflows.policy import execution_allowed


class JobManager:
    def __init__(self, store: LibraryStore):
        self.store = store

    def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        principal: str = "local_owner",
        max_calls: int = 0,
        admission: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self.store.transact(
            self._submission(
                kind,
                payload,
                idempotency_key=idempotency_key,
                principal=principal,
                max_calls=max_calls,
                admission=admission,
            )
        )

    def _submission(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        principal: str = "local_owner",
        max_calls: int = 0,
        admission: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """Validated mutation for an atomic batch; never opens a nested writer transaction."""
        valid_id(principal)
        if kind not in {"add", "sync", "process"} or not isinstance(payload, dict):
            raise ContextError("invalid_argument", "任务类型或参数无效。")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ContextError("invalid_argument", "写任务需要长度受限的幂等键。")
        if type(max_calls) is not int or not 0 <= max_calls <= 1000:
            raise ContextError("invalid_budget", "调用次数上限须在 0 到 1000 之间。")
        if len(str(payload)) > 65_536:
            raise ContextError("invalid_argument", "任务参数过长。")
        fingerprint = digest({"kind": kind, "payload": payload, "max_calls": max_calls})
        key = digest([principal, idempotency_key])
        now = utc_now()

        def save(state):
            previous = state["idempotency"].get(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ContextError("idempotency_conflict", "同一幂等键已用于不同任务参数。")
                return state["jobs"][previous["job_id"]]
            job: dict[str, Any] = {
                "id": "j_" + uuid.uuid4().hex,
                "kind": kind,
                "payload": payload,
                "principal": principal,
                "state": "queued",
                "created_at": now,
                "updated_at": now,
                "stages": {},
                "calls": [],
                "budget": {"max_calls": max_calls},
                "error": None,
                "attempt": 1,
                "cancel_requested": False,
            }
            if admission is not None:
                admission(state, job)
            state["jobs"][job["id"]] = job
            state["idempotency"][key] = {"fingerprint": fingerprint, "job_id": job["id"]}
            return job

        return save

    def get(self, ref: str, *, principal: str = "local_owner") -> dict[str, Any]:
        job = self.store.snapshot()["jobs"].get(valid_id(ref))
        if not job or job["principal"] != principal:
            raise ContextError("not_found", "此凭据无权查看该任务。")
        return job

    @staticmethod
    def _job(state, ref: str, principal: str) -> dict[str, Any]:
        job = state["jobs"].get(ref)
        if not job or job["principal"] != principal:
            raise ContextError("not_found", "此凭据无权操作该任务。")
        return job

    def start(self, ref: str, *, principal: str = "local_owner") -> dict[str, Any]:
        valid_id(ref)

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] != "queued":
                raise ContextError("job_not_queued", "只启动已排队任务，不重复启动或绕过阻塞。")
            if not execution_allowed(state, job):
                raise ContextError("automatic_processing_paused", "此自动任务已暂停，未启动或发模型请求。")
            job.update(state="running", updated_at=utc_now())
            return job

        return self.store.transact(change)

    def block_queued(
        self, ref: str, error: ContextError, *, principal: str = "local_owner"
    ) -> dict[str, Any]:
        """Persist reconstruction failure without pretending a request was dispatched."""
        valid_id(ref)

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] != "queued":
                return job  # A concurrent cancellation/start is never overwritten.
            job.update(state="blocked", error=error.as_dict(), updated_at=utc_now())
            return job

        return self.store.transact(change)

    def suspend_source(self, ref: str, *, lease: ExecutorLease) -> dict[str, Any]:
        """Keep a non-paid source checkpoint queued when its registered timer is paused."""
        lease.check()
        if lease.files.root != self.store.files.root or lease.files.identity != self.store.files.identity:
            raise ContextError("executor_not_owned", "来源检查点不属于此执行器。")

        def change(state):
            job = self._job(state, valid_id(ref), "local_owner")
            if job["state"] != "running":
                return job
            if (
                job["kind"] != "sync"
                or job["payload"].get("dispatch", {}).get("mode") != "scheduled_sync"
                or job["calls"]
                or job["budget"]["max_calls"] != 0
            ):
                raise ContextError("invalid_dispatch_policy", "只能暂停不含模型请求的固定定时来源任务。")
            if execution_allowed(state, job):
                return job  # A concurrent explicit re-enable won; do not overwrite current authority.
            job.update(state="queued", updated_at=utc_now())
            for stage in job["stages"].values():
                if stage["state"] == "running":
                    stage.update(state="interrupted", updated_at=utc_now())
            return job

        return self.store.transact(change)

    def begin_call(
        self,
        ref: str,
        *,
        stage: str,
        input_hash: str,
        processor_version: str,
        principal: str = "local_owner",
        start_stage: bool = False,
    ) -> dict[str, Any]:
        """Commit an intent BEFORE dispatch. An unresolved intent never silently retries."""
        valid_id(ref)
        valid_id(stage)
        if type(start_stage) is not bool:
            raise ContextError("invalid_argument", "阶段启动策略须为明确布尔值。")
        if (
            not isinstance(input_hash, str)
            or not input_hash
            or not isinstance(processor_version, str)
            or not processor_version
        ):
            raise ContextError("invalid_argument", "调用需要输入与处理器版本。")
        signature = digest([stage, input_hash, processor_version])

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] != "running" or job["cancel_requested"]:
                raise ContextError("job_not_running", "任务未运行或已取消。")
            previous = next((c for c in job["calls"] if c["signature"] == signature), None)
            if previous:
                if previous["state"] == "completed":
                    return {**previous, "reused": True}
                raise ContextError(
                    "upstream_outcome_unknown",
                    "此请求可能已发出；未自动重复计费。",
                    next_action="核对上游结果后显式选择重试。",
                    possibly_charged=True,
                )
            if len(job["calls"]) >= job["budget"]["max_calls"]:
                raise ContextError(
                    "budget_required", "已达到任务调用上限。", next_action="调整此任务额度或结束处理。"
                )
            previous_stage = job["stages"].get(stage)
            if start_stage and previous_stage and previous_stage["input_hash"] != input_hash:
                raise ContextError("stage_input_changed", "任务阶段输入已固定，不能热切换。")
            call = {
                "id": "x_" + uuid.uuid4().hex,
                "signature": signature,
                "stage": stage,
                "input_hash": input_hash,
                "processor_version": processor_version,
                "state": "intent",
                "created_at": utc_now(),
                "usage": None,
                "actual_model": None,
                "upstream_request_id": None,
                "elapsed_seconds": None,
            }
            job["calls"].append(call)
            if start_stage:
                # The execution marker and paid intent share ONE visibility boundary.
                # No network invocation occurs until this durable commit returns.
                job["stages"][stage] = {
                    "state": "running",
                    "input_hash": input_hash,
                    "result": None,
                    "error": None,
                    "updated_at": call["created_at"],
                }
            job["updated_at"] = utc_now()
            return {**call, "reused": False}

        return self.store.transact(change)

    def finish_call(
        self,
        ref: str,
        call_id: str,
        *,
        outcome: str,
        actual_model: str | None = None,
        usage: dict[str, Any] | None = None,
        upstream_request_id: str | None = None,
        elapsed_seconds: float | None = None,
        principal: str = "local_owner",
    ) -> dict[str, Any]:
        valid_id(ref)
        valid_id(call_id)
        if outcome not in {"completed", "failed", "unknown", "not_dispatched"}:
            raise ContextError("invalid_argument", "调用结果状态无效。")

        def change(state):
            job = self._job(state, ref, principal)
            call = next((c for c in job["calls"] if c["id"] == call_id), None)
            if not call or call["state"] != "intent":
                raise ContextError("call_state_conflict", "调用不存在或结果已记录，未覆盖原记录。")
            call.update(
                state=outcome,
                actual_model=actual_model,
                usage=usage,
                upstream_request_id=upstream_request_id,
                elapsed_seconds=elapsed_seconds,
                finished_at=utc_now(),
            )
            job["updated_at"] = utc_now()
            return call

        return self.store.transact(change)

    def finish(
        self,
        ref: str,
        state_name: str,
        *,
        error: ContextError | None = None,
        principal: str = "local_owner",
        authorization: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        valid_id(ref)
        if state_name not in TERMINAL_STATES | {"blocked"}:
            raise ContextError("invalid_argument", "结束状态无效。")

        def change(state):
            if authorization is not None:
                authorization()
            job = self._job(state, ref, principal)
            if job["state"] != "running":
                raise ContextError("job_not_running", "任务当前未运行。")
            unresolved = any(c["state"] in {"intent", "unknown"} for c in job["calls"])
            if state_name == "succeeded" and unresolved:
                raise ContextError(
                    "upstream_outcome_unknown", "上游结果尚未确认，不能标为成功。", possibly_charged=True
                )
            job.update(state=state_name, error=error.as_dict() if error else None, updated_at=utc_now())
            if state_name in {"blocked", "failed"}:
                for stage in job["stages"].values():
                    if stage["state"] == "running":
                        stage.update(
                            state=state_name, error=error.as_dict() if error else None, updated_at=utc_now()
                        )
            return job

        return self.store.transact(change)

    def cancel(self, ref: str, *, principal: str = "local_owner") -> dict[str, Any]:
        valid_id(ref)

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] not in TERMINAL_STATES:
                job.update(state="cancelled", cancel_requested=True, updated_at=utc_now())
                for stage in job["stages"].values():
                    if stage["state"] == "running":
                        stage.update(state="cancelled", updated_at=utc_now())
            return job

        return self.store.transact(change)

    def recover_interrupted(
        self, *, lease: ExecutorLease, principal: str | None = None, job_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Kernel-backed ownership is mandatory; no live-process guessing from a state file."""
        lease.check()
        if job_ids is not None:
            for identity in job_ids:
                valid_id(identity)
        if lease.files.root != self.store.files.root or lease.files.identity != self.store.files.identity:
            raise ContextError("executor_not_owned", "执行所有权不属于此库。")

        def change(state):
            recovered = []
            for job in state["jobs"].values():
                if job_ids is not None and job["id"] not in job_ids:
                    continue
                if principal is not None and job["principal"] != principal:
                    continue
                if job["state"] != "running":
                    continue
                uncertain = any(c["state"] in {"intent", "unknown"} for c in job["calls"])
                job.update(state="blocked" if uncertain else "queued", updated_at=utc_now())
                if uncertain:
                    job["error"] = ContextError(
                        "upstream_outcome_unknown", "中断前的请求结果待确认，未重发。", possibly_charged=True
                    ).as_dict()
                for stage in job["stages"].values():
                    if stage["state"] == "running":
                        stage.update(state="blocked" if uncertain else "interrupted", updated_at=utc_now())
                recovered.append(job)
            return recovered

        return self.store.transact(change)

    def set_stage(
        self,
        ref: str,
        stage: str,
        *,
        state_name: str,
        input_hash: str,
        result: dict[str, Any] | None = None,
        error: ContextError | None = None,
        principal: str = "local_owner",
    ) -> dict[str, Any]:
        valid_id(ref)
        valid_id(stage)
        if (
            state_name not in {"running", "ready", "partial", "failed", "not_applicable", "blocked"}
            or not input_hash
        ):
            raise ContextError("invalid_stage", "处理阶段状态或输入版本无效。")

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] != "running" or job["cancel_requested"]:
                raise ContextError("job_not_running", "任务已停止，未更改阶段。")
            previous = job["stages"].get(stage)
            if previous and previous["input_hash"] != input_hash:
                raise ContextError("stage_input_changed", "任务阶段输入已固定，不能热切换。")
            value = {
                "state": state_name,
                "input_hash": input_hash,
                "result": result,
                "error": error.as_dict() if error else None,
                "updated_at": utc_now(),
            }
            job["stages"][stage] = value
            job["updated_at"] = utc_now()
            return value

        return self.store.transact(change)

    def commit_call_result(
        self,
        ref: str,
        call_id: str,
        result: dict[str, Any],
        *,
        principal: str = "local_owner",
        complete_stage: bool = False,
    ) -> dict[str, Any]:
        """Text + returned usage become confirmed in ONE manifest commit; an orphan is not success."""
        valid_id(ref)
        valid_id(call_id)
        if type(complete_stage) is not bool:
            raise ContextError("invalid_argument", "阶段完成策略须为明确布尔值。")
        if complete_stage and result.get("status") not in {"ready", "partial", "not_applicable"}:
            raise ContextError("invalid_stage_result", "阶段结果状态无效；未标记阶段完成。")
        body = canonical_bytes(result)
        if len(body) > 2_000_000:
            raise ContextError(
                "result_limit", "模型结果超过持久化上限，未确认该请求。", possibly_charged=True
            )
        path = f".context/请求结果/{call_id}.json"

        def change(state):
            job = self._job(state, ref, principal)
            call = next((value for value in job["calls"] if value["id"] == call_id), None)
            if not call or call["state"] != "intent":
                raise ContextError("call_state_conflict", "调用不存在或结果已登记，未覆盖。")
            self.store.files.write(path, body)
            descriptor = {"call_id": call_id, "path": path, "sha256": hashlib.sha256(body).hexdigest()}
            if complete_stage:
                # Retain the old stage publication's actual file/hash check;
                # coalescing transactions must not replace it with in-memory data.
                try:
                    if self.read_result(descriptor) != result:
                        raise ContextError("corrupt_call_result", "请求结果与落盘文件不一致。")
                except ContextError as error:
                    if error.code != "corrupt_call_result":
                        raise
                    raise ContextError(
                        "corrupt_call_result",
                        "返回结果回读校验失败；保留调用意图，不标记完成或重发。",
                        possibly_charged=True,
                    ) from None
            call.update(
                state="completed",
                result=descriptor,
                finished_at=utc_now(),
                actual_model=result.get("actual_model"),
                usage=result.get("usage"),
                upstream_request_id=result.get("upstream_request_id"),
                elapsed_seconds=result.get("elapsed_seconds"),
            )
            conflict = False
            if complete_stage and job["state"] == "running" and not job["cancel_requested"]:
                previous = job["stages"].get(call["stage"])
                if previous is None or previous["input_hash"] != call["input_hash"]:
                    # Still retain the paid result/usage even when the stage identity
                    # no longer agrees. Report this conflict AFTER committing it.
                    conflict = True
                else:
                    job["stages"][call["stage"]] = {
                        "state": result["status"],
                        "input_hash": call["input_hash"],
                        "signature": call["signature"],
                        "result": descriptor,
                        "error": None,
                        "updated_at": call["finished_at"],
                    }
            job["updated_at"] = utc_now()
            return descriptor, conflict

        descriptor, conflict = self.store.transact(change)
        if conflict:
            raise ContextError(
                "stage_input_changed", "阶段身份已变化；已保留返回正文与实际用量，未标记完成。"
            )
        return descriptor

    def read_result(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        try:
            call_id = valid_id(descriptor["call_id"])
            if descriptor["path"] != f".context/请求结果/{call_id}.json":
                raise ValueError
            body = self.store.files.read(descriptor["path"], max_bytes=2_000_000)
            if hashlib.sha256(body).hexdigest() != descriptor["sha256"]:
                raise ValueError
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError):
            raise ContextError(
                "corrupt_call_result", "已确认请求结果校验失败，未通过重复计费修复。"
            ) from None

    def reusable_result(self, signature: str, *, principal: str = "local_owner") -> dict[str, Any] | None:
        for job in self.store.snapshot()["jobs"].values():
            if job["principal"] != principal:
                continue
            for call in job["calls"]:
                if call["signature"] == signature and call["state"] == "completed" and call.get("result"):
                    result = self.read_result(call["result"])
                    if result.get("status") in {"ready", "not_applicable"}:
                        return call["result"]
        return None

    def commit_stage_result(
        self,
        ref: str,
        stage: str,
        *,
        input_hash: str,
        signature: str,
        result: dict[str, Any],
        descriptor: dict[str, Any] | None = None,
        principal: str = "local_owner",
    ) -> dict[str, Any]:
        valid_id(ref)
        valid_id(stage)
        body = canonical_bytes(result)
        if len(body) > 2_000_000 or result.get("status") not in {"ready", "partial", "not_applicable"}:
            raise ContextError("invalid_stage_result", "阶段结果无效或超过保存上限。")
        if descriptor is not None:
            if self.read_result(descriptor) != result:
                raise ContextError("corrupt_call_result", "请求记录与阶段结果不一致。")

        def change(state):
            job = self._job(state, ref, principal)
            if job["state"] != "running" or job["cancel_requested"]:
                raise ContextError("job_not_running", "任务已停止，仍保留已确认请求用量。")
            previous = job["stages"].get(stage)
            if previous and previous["input_hash"] != input_hash:
                raise ContextError("stage_input_changed", "阶段输入不能热切换。")
            target = descriptor
            if target is None:
                result_id = "x_" + uuid.uuid4().hex
                path = f".context/请求结果/{result_id}.json"
                self.store.files.write(path, body)
                target = {"call_id": result_id, "path": path, "sha256": hashlib.sha256(body).hexdigest()}
            stage_result = {
                "state": result["status"],
                "input_hash": input_hash,
                "signature": signature,
                "result": target,
                "error": None,
                "updated_at": utc_now(),
            }
            job["stages"][stage] = stage_result
            job["updated_at"] = utc_now()
            return stage_result

        return self.store.transact(change)

    def reusable_stage(self, signature: str, *, principal: str = "local_owner") -> dict[str, Any] | None:
        for job in self.store.snapshot()["jobs"].values():
            if job["principal"] != principal:
                continue
            for stage in job["stages"].values():
                if stage.get("signature") == signature and stage["state"] in {
                    "ready",
                    "not_applicable",
                }:
                    self.read_result(stage["result"])
                    return stage["result"]
        return self.reusable_result(signature, principal=principal)
