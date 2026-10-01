"""Original synchronous worker core; public submission stays asynchronous and separately authorized."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes, digest, valid_id
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


@dataclass(frozen=True)
class StageOutcome:
    output: dict[str, Any]
    status: str = "ready"
    actual_model: str | None = None
    usage: dict[str, Any] | None = None
    upstream_request_id: str | None = None
    elapsed_seconds: float | None = None

    def record(self) -> dict[str, Any]:
        result = {
            "output": self.output,
            "status": self.status,
            "actual_model": self.actual_model,
            "usage": self.usage,
            "upstream_request_id": self.upstream_request_id,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if not isinstance(self.output, dict) or self.status not in {"ready", "partial", "not_applicable"}:
            raise ContextError("invalid_stage_result", "处理器未返回有效阶段结果。")
        try:
            if len(canonical_bytes(result)) > 2_000_000:
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise ContextError("invalid_stage_result", "处理器结果过大或不是有效 JSON。") from None
        return result


@dataclass(frozen=True)
class Stage:
    name: str
    input_hash: str
    processor_version: str
    invoke: Callable[[dict[str, dict[str, Any]]], StageOutcome] = field(repr=False, compare=False)
    dependencies: tuple[str, ...] = ()
    paid: bool = False
    allow_partial_dependencies: bool = False
    cacheable: bool = True
    validate: Callable[[dict[str, dict[str, Any]]], None] | None = field(
        default=None, repr=False, compare=False
    )

    def descriptor(self) -> dict[str, Any]:
        valid_id(self.name)
        if (
            not isinstance(self.input_hash, str)
            or not 1 <= len(self.input_hash) <= 200
            or not isinstance(self.processor_version, str)
            or not 1 <= len(self.processor_version) <= 200
            or type(self.paid) is not bool
            or type(self.cacheable) is not bool
            or type(self.allow_partial_dependencies) is not bool
            or not callable(self.invoke)
            or (self.validate is not None and not callable(self.validate))
        ):
            raise ContextError("invalid_stage", "阶段输入、处理器或策略无效。")
        for dependency in self.dependencies:
            valid_id(dependency)
        return {
            "name": self.name,
            "input_hash": self.input_hash,
            "processor_version": self.processor_version,
            "dependencies": list(self.dependencies),
            "paid": self.paid,
            "allow_partial_dependencies": self.allow_partial_dependencies,
            "cacheable": self.cacheable,
        }


def plan(stages: list[Stage]) -> list[dict[str, Any]]:
    if not isinstance(stages, list) or not stages or len(stages) > 500:
        raise ContextError("invalid_plan", "处理计划为空或阶段过多。")
    seen: set[str] = set()
    result = []
    for stage in stages:
        if not isinstance(stage, Stage):
            raise ContextError("invalid_plan", "处理计划含未注册的阶段。")
        descriptor = stage.descriptor()
        if stage.name in seen or set(stage.dependencies) - seen:
            raise ContextError("invalid_plan", "阶段重复、依赖不存在或顺序不正确。")
        seen.add(stage.name)
        result.append(descriptor)
    return result


class DurableExecutor:
    """Callables are registered by own code, never reconstructed from source text or JSON commands."""

    def __init__(self, store: LibraryStore):
        self.store, self.jobs = store, JobManager(store)

    def submit(
        self, stages: list[Stage], *, idempotency_key: str, max_calls: int = 0, principal: str = "local_owner"
    ) -> dict[str, Any]:
        return self.jobs.submit(
            "process",
            {"plan": plan(stages)},
            idempotency_key=idempotency_key,
            max_calls=max_calls,
            principal=principal,
        )

    def _check(self, ref: str, lease: ExecutorLease, principal: str) -> None:
        lease.check()
        job = self.jobs.get(ref, principal=principal)
        if job["state"] != "running" or job["cancel_requested"]:
            raise ContextError("job_not_running", "任务已停止，未派发后续阶段。")

    def _unknown(self, signature: str, principal: str) -> None:
        # A newly submitted job must not silently duplicate a prior unconfirmed request.
        for job in self.store.snapshot()["jobs"].values():
            if job["principal"] != principal:
                continue
            if any(
                call["signature"] == signature and call["state"] in {"intent", "unknown"}
                for call in job["calls"]
            ):
                raise ContextError(
                    "upstream_outcome_unknown",
                    "同一阶段有结果未明的请求；未自动重发。",
                    possibly_charged=True,
                    next_action="核对上游请求后显式解决该阻塞。",
                )

    def _stage(
        self, ref: str, stage: Stage, results: dict[str, dict[str, Any]], lease: ExecutorLease, principal: str
    ) -> dict[str, Any]:
        self._check(ref, lease, principal)
        dependencies = {name: results[name] for name in stage.dependencies}
        actual_input = digest({"declared_input": stage.input_hash, "dependencies": dependencies})
        signature = digest([stage.name, actual_input, stage.processor_version])
        if not stage.allow_partial_dependencies and any(
            value["status"] not in {"ready", "not_applicable"} for value in dependencies.values()
        ):
            error = ContextError("dependency_incomplete", "前置阶段缺失或部分完成；未自动补写内容。")
            self.jobs.set_stage(
                ref,
                stage.name,
                state_name="failed",
                input_hash=actual_input,
                error=error,
                principal=principal,
            )
            return {"status": "failed", "error": error.as_dict()}
        if stage.paid:
            self._unknown(signature, principal)
        previous = self.jobs.get(ref, principal=principal)["stages"].get(stage.name)
        if (
            previous
            and previous["input_hash"] == actual_input
            and previous["state"] in {"ready", "partial", "not_applicable"}
        ):
            return self.jobs.read_result(previous["result"])
        if stage.cacheable:
            cached = self.jobs.reusable_stage(signature, principal=principal)
            if cached is not None:
                result = self.jobs.read_result(cached)
                self.jobs.commit_stage_result(
                    ref,
                    stage.name,
                    input_hash=actual_input,
                    signature=signature,
                    result=result,
                    descriptor=cached,
                    principal=principal,
                )
                return result
        if previous and previous["state"] == "failed":
            return {"status": "failed", "error": previous["error"]}
        if stage.validate is not None:
            try:
                stage.validate(copy.deepcopy(dependencies))
            except ContextError as preflight_error:
                self.jobs.set_stage(
                    ref,
                    stage.name,
                    state_name="failed",
                    input_hash=actual_input,
                    error=preflight_error,
                    principal=principal,
                )
                return {"status": "failed", "error": preflight_error.as_dict()}
        self.jobs.set_stage(
            ref, stage.name, state_name="running", input_hash=actual_input, principal=principal
        )
        call = None
        if stage.paid:
            # Commit intent/budget before reaching model code, then check cancellation again.
            call = self.jobs.begin_call(
                ref,
                stage=stage.name,
                input_hash=actual_input,
                processor_version=stage.processor_version,
                principal=principal,
            )
            try:
                self._check(ref, lease, principal)
            except ContextError:
                self.jobs.finish_call(ref, call["id"], outcome="not_dispatched", principal=principal)
                raise
        try:
            outcome = stage.invoke(copy.deepcopy(dependencies))
            if not isinstance(outcome, StageOutcome):
                raise ContextError("invalid_stage_result", "处理器返回类型无效。")
            result = outcome.record()
        except Exception as caught:
            error = (
                caught
                if isinstance(caught, ContextError)
                else ContextError("processor_failed", "处理阶段异常，未回显内部输入或凭据。")
            )
            uncertain = call is not None and (
                error.possibly_charged
                or not isinstance(caught, ContextError)
                or error.code == "invalid_stage_result"
            )
            if call is not None:
                self.jobs.finish_call(
                    ref, call["id"], outcome="unknown" if uncertain else "failed", principal=principal
                )
            if uncertain:
                raise ContextError(
                    "upstream_outcome_unknown",
                    "请求结果未确认；保留调用记录且不重发。",
                    possibly_charged=True,
                ) from None
            self._check(ref, lease, principal)
            self.jobs.set_stage(
                ref,
                stage.name,
                state_name="failed",
                input_hash=actual_input,
                error=error,
                principal=principal,
            )
            return {"status": "failed", "error": error.as_dict()}
        descriptor = None
        if call is not None:
            # Cancellation during a cloud request retains the late result and actual usage.
            descriptor = self.jobs.commit_call_result(ref, call["id"], result, principal=principal)
        self._check(ref, lease, principal)
        self.jobs.commit_stage_result(
            ref,
            stage.name,
            input_hash=actual_input,
            signature=signature,
            result=result,
            descriptor=descriptor,
            principal=principal,
        )
        return result

    def run(self, ref: str, stages: list[Stage], *, principal: str = "local_owner") -> dict[str, Any]:
        descriptors = plan(stages)
        with ExecutorLease(self.store.files.root) as lease:
            job = self.jobs.get(ref, principal=principal)
            if job["payload"].get("plan") != descriptors:
                raise ContextError("plan_changed", "任务的模型/输入/阶段计划已固定，未改用当前默认值。")
            self.jobs.recover_interrupted(lease=lease, principal=principal)
            job = self.jobs.get(ref, principal=principal)
            if job["state"] != "queued":
                return job
            self.jobs.start(ref, principal=principal)
            results: dict[str, dict[str, Any]] = {}
            try:
                for stage in stages:
                    results[stage.name] = self._stage(ref, stage, results, lease, principal)
                statuses = [result["status"] for result in results.values()]
                state = (
                    "succeeded"
                    if all(value in {"ready", "not_applicable"} for value in statuses)
                    else ("failed" if all(value == "failed" for value in statuses) else "partial")
                )
                self.jobs.finish(ref, state, principal=principal)
            except ContextError as error:
                current = self.jobs.get(ref, principal=principal)
                if current["state"] == "running":
                    self.jobs.finish(ref, "blocked", error=error, principal=principal)
                elif current["state"] != "cancelled":
                    raise
            return self.jobs.get(ref, principal=principal)
