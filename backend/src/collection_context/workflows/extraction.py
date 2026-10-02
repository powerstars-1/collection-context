"""Reconstruct extraction only from our versioned input/model registry, never executable job JSON."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from collection_context.application.contracts import ContextError, digest
from collection_context.infrastructure.secrets import CredentialBackend
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog
from collection_context.processing.stages import (
    LEGACY_SUMMARY_VERSION,
    SUMMARY_VERSION,
    VERSION,
    audio_stage,
    checked_summary_version,
    publish_stage,
    summary_stage,
    vision_stage,
)
from collection_context.workflows.executor import DurableExecutor, Stage, StageOutcome, plan
from collection_context.workflows.policy import execution_allowed


class ExtractionWorkflow:
    def __init__(self, store: LibraryStore, resolve_secret: Callable[[str], str]):
        backend = getattr(resolve_secret, "__self__", None)
        if isinstance(backend, CredentialBackend):
            library, secrets = store.files.root.resolve(), backend.files.root.resolve()
            if library.is_relative_to(secrets) or secrets.is_relative_to(library):
                raise ContextError(
                    "secret_directory_overlap", "独立凭据目录不能与资料库重叠；备份不包含模型密钥。"
                )
        self.store = store
        self.resolve_secret = resolve_secret
        self.inputs, self.models, self.executor = (
            PreparedInputs(store),
            ModelCatalog(store),
            DurableExecutor(store),
        )

    def _build(self, context: dict[str, Any], *, planning: bool = False) -> list[Stage]:
        if isinstance(context, dict) and context.get("workflow") == "summary_refresh":
            from collection_context.processing.summary_refresh import build

            return build(self.store, self.resolve_secret, context, planning=planning)
        if not isinstance(context, dict) or set(context) - {"summary_prompt_version"} != {
            "schema_version",
            "workflow",
            "input_id",
            "model_profiles",
            "processor_version",
            "summary_input_chars",
        }:
            raise ContextError("invalid_extraction_plan", "提取计划结构不兼容，不运行任意JSON中的命令。")
        if (
            context["schema_version"] != 1
            or context["workflow"] != "extraction"
            or context["processor_version"] != VERSION
        ):
            raise ContextError("processor_version_changed", "任务处理策略版本不兼容，未自动替换为新策略。")
        summary_version = checked_summary_version(
            context.get("summary_prompt_version", LEGACY_SUMMARY_VERSION)
        )
        payload = self.inputs.load(context["input_id"])
        ref = payload["material_ref"]
        current = self.store.get(ref)
        if current["content_hash"] != payload["content_hash"]:
            raise ContextError("version_changed", "任务的原文输入版本已变化，未重新解释旧任务。")
        if current.get("prepared_input") != context["input_id"]:
            raise ContextError("input_superseded", "资料已登记新的媒体快照，旧任务不能覆盖新输入的产物。")
        if any(
            value.get("owner_edit")
            for kind, value in current["artifacts"].items()
            if kind in {"original", "audio", "screen", "image", "summary", "readable"}
        ):
            raise ContextError("owner_edit_conflict", "资料含已确认的人工修改，未覆盖或提交模型请求。")
        roles = {"vision", "summary"} | ({"audio"} if payload["audio"] else set())
        if not isinstance(context["model_profiles"], dict) or set(context["model_profiles"]) != roles:
            raise ContextError("model_config_missing", "任务模型角色不完整。")
        resolve = (lambda _: "planning-only-placeholder") if planning else self.resolve_secret
        clients = {}
        for role, identity in context["model_profiles"].items():
            if self.models.get(identity)["role"] != role:
                raise ContextError("model_config_changed", "任务固定的角色引用不同，未发请求。")
            clients[role] = self.models.client(identity, resolve)
        stages: list[Stage] = []
        segments = self.inputs.audio(payload)
        if segments:
            stages.extend(audio_stage(ref, segment, clients["audio"]) for segment in segments)
        else:
            stages.append(
                Stage(
                    "audio_not_applicable",
                    digest([ref, context["input_id"], "no_audio"]),
                    VERSION,
                    lambda _: StageOutcome(
                        {"kind": "audio", "applicable": False, "reason": "准备后的原始媒体没有音轨。"},
                        status="not_applicable",
                    ),
                )
            )
        stages.extend(vision_stage(ref, frame, clients["vision"]) for frame in self.inputs.frames(payload))
        evidence = tuple(stage.name for stage in stages)
        combined = summary_stage(
            ref,
            {"title": current["title"], "body": current["body"]},
            evidence,
            clients["summary"],
            source_coverage=payload["coverage"],
            max_input_chars=context["summary_input_chars"],
            prompt_version=summary_version,
        )
        stages.append(combined)
        stages.append(
            publish_stage(
                self.store,
                ref,
                current["content_hash"],
                (*evidence, combined.name),
                source_coverage=payload["coverage"],
                prepared_input=context["input_id"],
            )
        )
        return stages

    def submit(
        self,
        input_id: str,
        *,
        idempotency_key: str,
        max_calls: int,
        principal: str = "local_owner",
        summary_input_chars: int = 100_000,
    ) -> dict[str, Any]:
        return self.executor.jobs.submit(
            "process",
            self.prepare_plan(input_id, summary_input_chars=summary_input_chars),
            idempotency_key=idempotency_key,
            max_calls=max_calls,
            principal=principal,
        )

    def prepare_plan(
        self,
        input_id: str,
        *,
        summary_input_chars: int = 100_000,
        model_profiles: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Registered plan only, for atomic manual/history/automatic admission; no secrets or requests."""
        payload = self.inputs.load(input_id)
        roles = ("audio", "vision", "summary") if payload["audio"] else ("vision", "summary")
        if model_profiles is not None and (
            not isinstance(model_profiles, dict)
            or not set(roles) <= set(model_profiles)
            or set(model_profiles) - {"audio", "vision", "summary"}
        ):
            raise ContextError("model_config_missing", "固定模型角色不完整。")
        context = {
            "schema_version": 1,
            "workflow": "extraction",
            "input_id": input_id,
            "model_profiles": self.models.pin(roles)
            if model_profiles is None
            else {role: model_profiles[role] for role in roles},
            "processor_version": VERSION,
            "summary_input_chars": summary_input_chars,
            "summary_prompt_version": SUMMARY_VERSION,
        }
        stages = self._build(context, planning=True)  # No credential resolution or network at submission.
        return {"plan": plan(stages), "extraction": context}

    def run(self, job_id: str, *, principal: str = "local_owner") -> dict[str, Any]:
        job = self.executor.jobs.get(job_id, principal=principal)
        if job["state"] in {"succeeded", "partial", "failed", "cancelled", "blocked"}:
            return job
        if not execution_allowed(self.store.snapshot(), job):
            raise ContextError("automatic_processing_paused", "此自动任务已暂停，未解析凭据或发送请求。")
        context = job["payload"].get("extraction")
        if context is None:
            raise ContextError("invalid_extraction_plan", "此任务不是本项目注册的提取计划。")
        stages = self._build(context)
        return self.executor.run(job_id, stages, principal=principal)
