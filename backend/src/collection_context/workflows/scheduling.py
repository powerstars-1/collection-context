"""Own explicit automatic boundary, bounded history batches and selected backlog resumption."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.policy import automatic_policy, execution_allowed

if TYPE_CHECKING:
    from collection_context.workflows.extraction import ExtractionWorkflow


class ProcessingSchedule:
    def __init__(self, workflow: ExtractionWorkflow):
        self.workflow, self.store = workflow, workflow.store

    def status(self) -> dict[str, Any]:
        state = self.store.snapshot()
        settings = state["settings"]
        identity = settings.get("automatic_policy")
        policy = automatic_policy(state, identity) if identity else None
        admitted = sum(p == identity for p in state.get("automatic_admissions", {}).values())
        paused = [
            job["id"]
            for job in state["jobs"].values()
            if job["principal"] == "local_owner"
            and job["state"] == "queued"
            and job["payload"].get("dispatch", {}).get("mode") == "automatic"
            and not execution_allowed(state, job)
        ]
        return {
            "enabled": settings.get("auto_process") is True,
            "policy_id": identity,
            "boundary_generation": policy["boundary_generation"] if policy else None,
            "max_calls_per_task": policy["max_calls"] if policy else None,
            "max_new_tasks": policy["max_new_tasks"] if policy else 0,
            "admitted_new_tasks": admitted,
            "remaining_new_tasks": max(0, policy["max_new_tasks"] - admitted) if policy else 0,
            "paused_job_ids": sorted(paused),
            "paused_count": len(paused),
            "preparation_blocked_count": len(state.get("automatic_preparation_errors", {})),
            "preparation_issues": [
                {
                    "material_ref": issue["material_ref"],
                    "input_id": issue["input_id"],
                    "error_code": issue["error"]["code"],
                }
                for issue in list(state.get("automatic_preparation_errors", {}).values())[:20]
            ],
            "unknown_action_time_is_recent": False,
            "auto_sync_changed": False,
        }

    def configure(
        self,
        enabled: bool,
        *,
        allow_model_calls: bool = False,
        max_calls: int = 0,
        max_new_tasks: int = 5,
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ContextError("invalid_argument", "自动提取开关须为布尔值。")
        if enabled and allow_model_calls is not True:
            raise ContextError("processing_authorization_required", "启用自动提取须确认上传媒体和调用额度。")
        if (
            type(max_calls) is not int
            or not 0 <= max_calls <= 1000
            or type(max_new_tasks) is not int
            or not 1 <= max_new_tasks <= 1000
        ):
            raise ContextError("invalid_budget", "每条调用上限0至1000，本次新任务上限1至1000。")
        profiles = ModelCatalog(self.store).pin(("audio", "vision", "summary")) if enabled else None

        def change(state):
            state["settings"]["auto_process"] = enabled
            state["settings"]["automatic_resumed_jobs"] = []
            if enabled:
                policy = {
                    "schema_version": 1,
                    "boundary_generation": state["generation"],
                    "created_at": utc_now(),
                    "model_profiles": profiles,
                    "max_calls": max_calls,
                    "max_new_tasks": max_new_tasks,
                }
                identity = "a_" + digest(policy)
                state.setdefault("automatic_policies", {})[identity] = policy
                state["settings"]["automatic_policy"] = identity

        self.store.transact(change)
        return self.status()

    @staticmethod
    def _duplicate(state, input_id: str, material_ref: str) -> bool:
        return any(
            j["kind"] == "process"
            and j["principal"] == "local_owner"
            and (
                j["payload"].get("extraction", {}).get("input_id") == input_id
                or state.get("prepared_inputs", {})
                .get(j["payload"].get("extraction", {}).get("input_id"), {})
                .get("material_ref")
                == material_ref
            )
            for j in state["jobs"].values()
        )

    def admit_new(self, *, limit: int = 5) -> list[str]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ContextError("invalid_argument", "每轮最多登记20条新资料。")
        state = self.store.snapshot()
        if state["settings"].get("auto_process") is not True:
            return []
        identity = state["settings"].get("automatic_policy")
        policy = automatic_policy(state, identity)
        candidates = sorted(
            state["items"].values(), key=lambda i: (i.get("first_observed_generation", -1), i["id"])
        )
        jobs = []
        for item in candidates:
            if (
                sum(p == identity for p in state.get("automatic_admissions", {}).values())
                >= policy["max_new_tasks"]
            ):
                break
            input_id = item.get("prepared_input")
            marker = digest([identity, item["id"], input_id])
            if (
                item["excluded"]
                or item.get("first_observed_principal") != "local_owner"
                or not input_id
                or item.get("first_observed_generation", -1) <= policy["boundary_generation"]
                or self._duplicate(state, input_id, item["id"])
                or marker in state.get("automatic_preparation_errors", {})
            ):
                continue
            # Rebuild only registered plans. A per-item failure is visible to the worker, not silently swallowed.
            try:
                payload = self.workflow.prepare_plan(input_id, model_profiles=policy["model_profiles"])
            except ContextError as error:
                if error.code not in {
                    "version_changed",
                    "input_superseded",
                    "media_changed",
                    "model_config_missing",
                    "model_config_changed",
                    "invalid_extraction_plan",
                    "processor_version_changed",
                    "excluded_material",
                    "not_found",
                }:
                    raise
                # A bad candidate cannot starve the entire manual queue. Retain a non-retrying visible issue.
                failure = error.as_dict()
                self.store.transact(
                    lambda s: s.setdefault("automatic_preparation_errors", {}).update(
                        {
                            marker: {
                                "policy_id": identity,
                                "material_ref": item["id"],
                                "input_id": input_id,
                                "error": failure,
                                "created_at": utc_now(),
                            },
                        }
                    )
                )
                state = self.store.snapshot()
                continue
            payload["dispatch"] = {"mode": "automatic", "policy_id": identity}

            def admission(current, job, *, ref=item["id"], expected_input=input_id):
                config = current["settings"]
                actual = current["items"].get(ref)
                admissions = current.setdefault("automatic_admissions", {})
                if config.get("auto_process") is not True or config.get("automatic_policy") != identity:
                    raise ContextError("automatic_processing_paused", "自动策略已变化，未登记新任务。")
                automatic_policy(current, identity)
                if sum(p == identity for p in admissions.values()) >= policy["max_new_tasks"]:
                    raise ContextError("automatic_task_limit", "已达到本次新任务上限，需显式追加授权。")
                if (
                    not actual
                    or actual["excluded"]
                    or actual.get("first_observed_principal") != "local_owner"
                    or actual.get("prepared_input") != expected_input
                    or actual.get("first_observed_generation", -1) <= policy["boundary_generation"]
                    or self._duplicate(current, expected_input, ref)
                ):
                    raise ContextError("automatic_candidate_changed", "资料或已有任务变化，未重复登记。")
                admissions[job["id"]] = identity

            try:
                job = self.workflow.executor.jobs.submit(
                    "process",
                    payload,
                    idempotency_key="auto_" + digest([identity, item["id"], input_id]),
                    max_calls=policy["max_calls"],
                    admission=admission,
                )
            except ContextError as error:
                if error.code in {"automatic_processing_paused", "automatic_task_limit"}:
                    break
                if error.code == "automatic_candidate_changed":
                    continue
                raise
            jobs.append(job["id"])
            state = self.store.snapshot()
            if len(jobs) >= limit:
                break
        return jobs

    @staticmethod
    def _selection(values: list[str]) -> None:
        if not isinstance(values, list) or not 1 <= len(values) <= 20:
            raise ContextError("invalid_argument", "每批请明确选择1至20个不重复引用。")
        for value in values:
            valid_id(value)
        if len(set(values)) != len(values):
            raise ContextError("invalid_argument", "批次引用不能重复。")

    def resume_preview(self, job_ids: list[str]) -> dict[str, Any]:
        self._selection(job_ids)
        state = self.store.snapshot()
        return self._resume_preview(state, job_ids)

    @staticmethod
    def _resume_preview(state, job_ids):
        selected = []
        for ref in job_ids:
            valid_id(ref)
            job = state["jobs"].get(ref)
            if (
                not job
                or job["principal"] != "local_owner"
                or job["state"] != "queued"
                or job["payload"].get("dispatch", {}).get("mode") != "automatic"
                or execution_allowed(state, job)
            ):
                raise ContextError("invalid_resume_selection", "只恢复明确选择的已排队暂停自动任务。")
            selected.append(
                {
                    "job_id": ref,
                    "max_calls": job["budget"]["max_calls"],
                    "plan_hash": digest(job["payload"]),
                    "calls_hash": digest(job["calls"]),
                }
            )
        token = digest(
            {
                "policy": state["settings"].get("automatic_policy"),
                "enabled": state["settings"].get("auto_process"),
                "selected": selected,
            }
        )
        return {
            "job_ids": job_ids,
            "count": len(selected),
            "max_calls": sum(j["max_calls"] for j in selected),
            "preview_token": token,
            "uses_original_fixed_models": True,
        }

    def resume(
        self, job_ids: list[str], *, preview_token: str, allow_model_calls: bool = False
    ) -> dict[str, Any]:
        if allow_model_calls is not True:
            raise ContextError("processing_authorization_required", "恢复积压须明确确认此批原任务调用上限。")
        self.resume_preview(job_ids)  # Validate selection before entering the writer transaction.

        def change(state):
            preview = self._resume_preview(state, job_ids)
            if preview["preview_token"] != preview_token:
                raise ContextError("resume_preview_changed", "待处理批次或开关已变化，请重新查看数量和额度。")
            if state["settings"].get("auto_process") is not True:
                raise ContextError("automatic_processing_paused", "请先启用新自动策略；旧积压仍需单独恢复。")
            state["settings"]["automatic_resumed_jobs"] = sorted(
                set(state["settings"].get("automatic_resumed_jobs", []) + job_ids)
            )
            return preview

        return self.store.transact(change)

    def history(
        self,
        input_ids: list[str],
        *,
        idempotency_key: str,
        max_calls: int,
        allow_model_calls: bool = False,
    ) -> dict[str, Any]:
        if allow_model_calls is not True:
            raise ContextError("processing_authorization_required", "历史补处理须明确选择资料并确认额度。")
        self._selection(input_ids)
        if type(max_calls) is not int or not 0 <= max_calls <= 1000:
            raise ContextError("invalid_budget", "历史每条调用上限须在0至1000之间。")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ContextError("invalid_argument", "历史批次需要长度受限的稳定幂等键。")
        batch_id = "b_" + digest(["local_owner", idempotency_key])[:32]
        payloads = [self.workflow.prepare_plan(identity) for identity in input_ids]
        for payload in payloads:
            payload["dispatch"] = {"mode": "history", "batch_id": batch_id}
        fingerprint = digest({"payloads": payloads, "max_calls": max_calls})
        mutations = [
            self.workflow.executor.jobs._submission(
                "process",
                payload,
                idempotency_key=f"{batch_id}_{index}",
                max_calls=max_calls,
            )
            for index, payload in enumerate(payloads)
        ]

        def change(state):
            batches = state.setdefault("history_batches", {})
            previous = batches.get(batch_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ContextError("idempotency_conflict", "此历史批次键已用于不同资料、配置或额度。")
                return previous
            jobs = [mutation(state) for mutation in mutations]
            batch = {
                "id": batch_id,
                "principal": "local_owner",
                "fingerprint": fingerprint,
                "created_at": utc_now(),
                "job_ids": [j["id"] for j in jobs],
                "max_calls_per_task": max_calls,
                "max_calls_total": max_calls * len(jobs),
            }
            batches[batch_id] = batch
            return batch

        return self.store.transact(change)

    def cancel_history(self, batch_id: str) -> dict[str, Any]:
        valid_id(batch_id)

        def change(state):
            batch = state.get("history_batches", {}).get(batch_id)
            if not batch or batch["principal"] != "local_owner":
                raise ContextError("not_found", "此历史批次不存在。")
            for ref in batch["job_ids"]:
                job = state["jobs"][ref]
                if job["state"] not in {"succeeded", "partial", "failed", "cancelled"}:
                    job.update(state="cancelled", cancel_requested=True, updated_at=utc_now())
                    for stage in job["stages"].values():
                        if stage["state"] == "running":
                            stage.update(state="cancelled", updated_at=utc_now())
            return {"batch_id": batch_id, "job_ids": batch["job_ids"], "cancel_requested": True}

        return self.store.transact(change)
