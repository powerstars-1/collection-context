"""Owner-confirmed retries of a fixed extraction plan; old paid-call ledgers stay immutable."""

from __future__ import annotations

import copy
from typing import Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id
from collection_context.library.store import LibraryStore
from collection_context.workflows.executor import plan
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.jobs import JobManager

RETRY_STATES = {"partial", "failed", "cancelled", "blocked"}
COMPLETE_STATES = {"ready", "not_applicable"}


def denied_secret(_: str) -> str:
    raise ContextError("permission_denied", "恢复预览不读取密钥或发送模型请求。")


class ModelRecovery:
    def __init__(self, store: LibraryStore):
        self.store = store
        self.jobs = JobManager(store)

    def _fixed(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(valid_id(job_id))
        if job["kind"] != "process" or job["state"] not in RETRY_STATES:
            raise ContextError(
                "invalid_retry_selection", "只为部分完成、失败、取消或受阻的媒体提取创建新尝试。"
            )
        context = job["payload"].get("extraction")
        stages = ExtractionWorkflow(self.store, denied_secret)._build(context, planning=True)
        if plan(stages) != job["payload"].get("plan"):
            raise ContextError("plan_changed", "旧任务的固定输入或处理版本不兼容，未更换模型后重试。")
        return job

    def preview(self, job_id: str, stages: list[str] | None = None) -> dict[str, Any]:
        self._fixed(job_id)
        return self._preview(self.store.snapshot(), job_id, stages)

    def _preview(self, state, job_id, stages):
        job = self.jobs._job(state, job_id, "local_owner")
        if job["state"] not in RETRY_STATES:
            raise ContextError("invalid_retry_selection", "任务状态已变化，请重新查看。")
        descriptors = job["payload"]["plan"]
        candidates = [
            {
                "name": item["name"],
                "state": job["stages"].get(item["name"], {}).get("state", "not_started"),
                "paid": item["paid"],
                "selectable": job["stages"].get(item["name"], {}).get("state") not in COMPLETE_STATES,
            }
            for item in descriptors
        ]
        stages = [] if stages is None else stages
        if (
            not isinstance(stages, list)
            or len(stages) > 500
            or any(not isinstance(name, str) for name in stages)
            or len(set(stages)) != len(stages)
            or set(stages) - {item["name"] for item in candidates if item["selectable"]}
        ):
            raise ContextError(
                "invalid_retry_selection", "请选择未完成阶段；成功阶段会复用，不可强制重新计费。"
            )
        affected = set(stages)
        for item in descriptors:
            if affected.intersection(item["dependencies"]):
                affected.add(item["name"])
        runnable = [item["name"] for item in descriptors if item["name"] in affected]
        stage_plans = {item["name"]: item for item in descriptors}
        unknown = []
        for previous in state["jobs"].values():
            if previous["principal"] != "local_owner":
                continue
            prior_plans = {item["name"]: item for item in previous["payload"].get("plan", [])}
            for call in previous["calls"]:
                if (
                    call["stage"] in affected
                    and call["state"] in {"intent", "unknown"}
                    and prior_plans.get(call["stage"]) == stage_plans[call["stage"]]
                ):
                    unknown.append(
                        {
                            "job_id": previous["id"],
                            "call_id": call["id"],
                            "stage": call["stage"],
                            "signature": call["signature"],
                            "state": call["state"],
                            "upstream_request_id": call.get("upstream_request_id"),
                            "actual_model": call.get("actual_model"),
                            "created_at": call.get("created_at"),
                            "call_version": digest(call),
                        }
                    )
        unknown.sort(key=lambda value: (value["job_id"], value["call_id"]))
        if len(unknown) > 1000:
            raise ContextError("recovery_limit", "待核对请求超过本次恢复范围，未解除任何阻塞。")
        profiles = job["payload"]["extraction"]["model_profiles"]
        fixed_models = {
            role: state["model_profiles"][identity]["model"] for role, identity in profiles.items()
        }
        max_calls = sum(item["paid"] for item in descriptors if item["name"] in affected)
        return {
            "job_id": job_id,
            "attempt": job.get("attempt", 1),
            "stages": candidates,
            "selected_stages": stages,
            "affected_stages": runnable,
            "reused_stages": [item["name"] for item in candidates if not item["selectable"]],
            "max_calls": max_calls,
            "unknown_calls": unknown,
            "fixed_models": fixed_models,
            "possibly_charged": bool(unknown),
            "amount": None,
            "preview_token": digest([job, stages, runnable, unknown]),
            "model_requests": 0,
        }

    def retry(
        self,
        job_id: str,
        *,
        stages: list[str],
        preview_token: str,
        idempotency_key: str,
        max_calls: int,
        allow_model_calls: bool = False,
        reviewed_call_ids: list[str],
        duplicate_charge_confirmed: bool = False,
    ) -> dict[str, Any]:
        if allow_model_calls is not True or type(duplicate_charge_confirmed) is not bool:
            raise ContextError(
                "processing_authorization_required", "请明确确认本次调用上限，未知请求可能重复计费。"
            )
        if not isinstance(stages, list) or not stages:
            raise ContextError("invalid_retry_selection", "至少选择一个未完成阶段。")
        if (
            not isinstance(reviewed_call_ids, list)
            or any(not isinstance(value, str) for value in reviewed_call_ids)
            or len(set(reviewed_call_ids)) != len(reviewed_call_ids)
        ):
            raise ContextError("invalid_argument", "请逐项核对未知请求。")
        if type(max_calls) is not int or not 0 <= max_calls <= 1000:
            raise ContextError("invalid_budget", "本次调用上限须为 0 至 1000 的整数。")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ContextError("invalid_argument", "恢复请求需要长度受限的幂等键。")
        if not isinstance(preview_token, str) or len(preview_token) != 64:
            raise ContextError("invalid_argument", "请先预览当前阶段和请求。")
        fixed = self._fixed(job_id)
        payload = copy.deepcopy(fixed["payload"])
        payload.pop("dispatch", None)  # A new explicit manual attempt, never renewed auto/history authority.
        request = {
            "parent_job_id": job_id,
            "selected_stages": stages,
            "preview_token": preview_token,
            "max_calls": max_calls,
            "reviewed_call_ids": reviewed_call_ids,
            "duplicate_charge_confirmed": duplicate_charge_confirmed,
        }
        fingerprint = digest(request)
        key = digest(["model-retry", idempotency_key])
        authorization_id = "r_" + key
        # Older executors reject this mode instead of ignoring the selected scope.
        payload["dispatch"] = {"mode": "model_retry", "authorization_id": authorization_id}

        def save(state):
            previous = state.get("model_retries", {}).get(authorization_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ContextError("idempotency_conflict", "同一恢复请求不能更改范围或计费确认。")
                return self.jobs._job(state, previous["job_id"], "local_owner")
            preview = self._preview(state, job_id, stages)
            if preview["preview_token"] != preview_token:
                raise ContextError("retry_preview_changed", "阶段、结果或未知请求已变化，请重新预览核对。")
            if max_calls < preview["max_calls"]:
                raise ContextError(
                    "retry_budget_insufficient", "本次上限低于所选阶段计划，请明确调整范围或额度。"
                )
            unknown = preview["unknown_calls"]
            if set(reviewed_call_ids) != {call["call_id"] for call in unknown} or (
                unknown and duplicate_charge_confirmed is not True
            ):
                raise ContextError(
                    "unknown_review_required",
                    "请逐项核对上游请求，并明确接受可能重复计费；原账本仍保留未知。",
                )
            if any(
                j.get("recovery", {}).get("parent_job_id") == job_id and j["state"] in {"queued", "running"}
                for j in state["jobs"].values()
            ):
                raise ContextError("retry_already_queued", "此任务已有等待或运行中的新尝试，请先查看它。")
            result = self.jobs._submission(
                "process", payload, idempotency_key="retry_" + key, max_calls=max_calls
            )(state)
            root_job_id = fixed.get("recovery", {}).get("root_job_id", job_id)
            result["attempt"] = 1 + max(
                j.get("attempt", 1)
                for j in state["jobs"].values()
                if j["id"] == root_job_id or j.get("recovery", {}).get("root_job_id") == root_job_id
            )
            result["recovery"] = {
                "schema_version": 1,
                "root_job_id": root_job_id,
                "parent_job_id": job_id,
                "selected_stages": stages,
                "affected_stages": preview["affected_stages"],
                "unknown_authorizations": unknown,
                "reviewed_at": utc_now(),
                "duplicate_charge_confirmed": duplicate_charge_confirmed,
            }
            result["stages"] = {
                name: copy.deepcopy(value)
                for name, value in fixed["stages"].items()
                if name not in preview["affected_stages"]
            }
            state.setdefault("model_retries", {})[authorization_id] = {
                "fingerprint": fingerprint,
                "job_id": result["id"],
                "payload_hash": digest(result["payload"]),
                "recovery_hash": digest(result["recovery"]),
                "max_calls": max_calls,
            }
            return result

        return self.store.transact(save)
