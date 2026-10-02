"""Owner management use cases: register durable work, never execute or resolve secrets."""

from __future__ import annotations

from typing import Any

from collection_context.application.connection_runner import ConnectionRunner
from collection_context.application.contracts import ContextError, envelope
from collection_context.application.model_recovery import ModelRecovery
from collection_context.application.model_setup import ModelSetup
from collection_context.application.source_management import SourceManagement
from collection_context.infrastructure.secrets import CredentialBackend
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.jobs import JobManager
from collection_context.workflows.scheduling import ProcessingSchedule


def no_secret_access(_: str) -> str:
    raise ContextError("permission_denied", "管理页面不能读取模型密钥或执行模型请求。")


class ManagementService:
    def __init__(
        self,
        store: LibraryStore,
        *,
        model_secrets: CredentialBackend | None = None,
        connection_runner: ConnectionRunner | None = None,
    ):
        self.store = store
        self.schedule = ProcessingSchedule(ExtractionWorkflow(store, no_secret_access))
        self.sources = SourceManagement(store)
        self.models = ModelSetup(store, model_secrets)
        self.connection_runner = connection_runner

    def overview(self, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ContextError("invalid_argument", "任务分页位置无效。")
        state = self.store.snapshot()
        jobs = sorted(
            (j for j in state["jobs"].values() if j["principal"] == "local_owner"),
            key=lambda j: (j["created_at"], j["id"]),
            reverse=True,
        )
        prepared = [
            {"material_ref": i["id"], "title": i["title"], "input_id": i["prepared_input"]}
            for i in sorted(state["items"].values(), key=lambda i: i["id"])
            if not i["excluded"]
            and i.get("prepared_input")
            and i.get("first_observed_principal") == "local_owner"
        ]
        return {
            "automatic": self._automatic(self.schedule.status()),
            "jobs": [self._public_job(j) for j in jobs[offset : offset + 20]],
            "total_jobs": len(jobs),
            "next_offset": offset + 20 if offset + 20 < len(jobs) else None,
            "prepared": [self._prepared(value) for value in prepared[offset : offset + 20]],
            "total_prepared": len(prepared),
            "next_prepared_offset": offset + 20 if offset + 20 < len(prepared) else None,
            "execution": "separately_authorized_worker",
            "worker": self.store.storage.observe_worker(self.store.files),
        }

    @staticmethod
    def _automatic(value: dict[str, Any]) -> dict[str, Any]:
        return {**value, "paused_job_ids": value["paused_job_ids"][:20]}

    def _prepared(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            media = PreparedInputs(self.store).load(value["input_id"])
            return {
                **value,
                "audio_segments": len(media["audio"]),
                "visual_frames": len(media["frames"]),
                "planned_calls_before_reuse": len(media["audio"]) + len(media["frames"]) + 1,
            }
        except ContextError as error:
            return {**value, "planned_calls_before_reuse": None, "preparation_error": error.code}

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        output = {
            "job_id": job["id"],
            "state": job["state"],
            "kind": job["kind"],
            "mode": job["payload"].get("dispatch", {}).get("mode", "manual"),
            "created_at": job["created_at"],
            "max_calls": job["budget"]["max_calls"],
            "recorded_calls": len(job["calls"]),
            "unknown_calls": sum(c["state"] in {"intent", "unknown"} for c in job["calls"]),
            "usage_missing_calls": sum(
                c["state"] == "completed" and c.get("usage") is None for c in job["calls"]
            ),
            "stages": {name: stage["state"] for name, stage in job["stages"].items()},
            "error_code": job["error"]["code"] if job["error"] else None,
            "attempt": job.get("attempt", 1),
            "parent_job_id": job.get("recovery", {}).get("parent_job_id"),
            "can_retry": job["kind"] == "process"
            and "extraction" in job["payload"]
            and job["state"] in {"partial", "failed", "cancelled", "blocked"},
        }
        if job["kind"] == "add":
            value = job["stages"].get("link_import", {}).get("result") or {}
            result = value.get("result", {})
            output["link_result"] = {
                "material_ref": result.get("material_ref"),
                "metadata": result.get("metadata", "not_started"),
                "download": result.get("download", "not_started"),
                "download_error_code": (result.get("error") or {}).get("code"),
                "extraction": "not_requested",
                "model_requests": 0,
            }
        return output

    def dispatch(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            fields = {
                "link-submit": (
                    {"url", "download", "idempotency_key", "source_confirmed"},
                    {"url", "download", "idempotency_key", "source_confirmed"},
                ),
                "link-status": ({"job_id"}, {"job_id"}),
                "connection-run": (set(), set()),
                "connection-start": ({"mode", "source_confirmed"}, {"mode", "source_confirmed"}),
                "connection-cancel": ({"run_id"}, {"run_id"}),
                "connection": (set(), set()),
                "self-source-create": (
                    {"kind", "collection_id", "connection_version", "limit", "download", "source_confirmed"},
                    {"kind", "collection_id", "connection_version", "limit", "download", "source_confirmed"},
                ),
                "models": (set(), set()),
                "model-save": (
                    {
                        "role",
                        "base_url",
                        "model",
                        "protocol",
                        "parameters",
                        "timeout",
                        "api_key",
                        "expected_profile_id",
                        "credential_confirmed",
                    },
                    {
                        "role",
                        "base_url",
                        "model",
                        "protocol",
                        "parameters",
                        "timeout",
                        "api_key",
                        "expected_profile_id",
                        "credential_confirmed",
                    },
                ),
                "sources": (set(), {"offset"}),
                "source-create": ({"creator_url", "limit", "download"}, {"creator_url", "limit", "download"}),
                "source-submit": (
                    {"config_id", "idempotency_key", "source_confirmed"},
                    {"config_id", "idempotency_key", "source_confirmed"},
                ),
                "source-timer": (
                    {"config_id", "enabled", "interval_minutes", "reset_blocked", "source_confirmed"},
                    {"config_id", "enabled", "interval_minutes", "reset_blocked", "source_confirmed"},
                ),
                "overview": (set(), {"offset"}),
                "automatic": (
                    {"enabled", "max_calls", "max_new_tasks", "fee_confirmed"},
                    {"enabled", "max_calls", "max_new_tasks", "fee_confirmed"},
                ),
                "history": (
                    {"input_ids", "idempotency_key", "max_calls", "fee_confirmed"},
                    {"input_ids", "idempotency_key", "max_calls", "fee_confirmed"},
                ),
                "cancel": ({"job_id"}, {"job_id"}),
                "retry-preview": ({"job_id"}, {"job_id", "stages"}),
                "retry": (
                    {
                        "job_id",
                        "stages",
                        "preview_token",
                        "idempotency_key",
                        "max_calls",
                        "fee_confirmed",
                        "reviewed_call_ids",
                        "duplicate_charge_confirmed",
                    },
                    {
                        "job_id",
                        "stages",
                        "preview_token",
                        "idempotency_key",
                        "max_calls",
                        "fee_confirmed",
                        "reviewed_call_ids",
                        "duplicate_charge_confirmed",
                    },
                ),
                "resume-preview": ({"job_ids"}, {"job_ids"}),
                "resume": (
                    {"job_ids", "preview_token", "fee_confirmed"},
                    {"job_ids", "preview_token", "fee_confirmed"},
                ),
            }
            if action not in fields:
                raise ContextError("permission_denied", "此管理动作未开放。")
            required, allowed = fields[action]
            if not isinstance(arguments, dict) or required - arguments.keys() or arguments.keys() - allowed:
                raise ContextError("invalid_argument", "缺少必填字段或包含不支持的管理参数。")
            args = dict(arguments)
            result: dict[str, Any]
            if "fee_confirmed" in args:
                if type(args["fee_confirmed"]) is not bool:
                    raise ContextError("invalid_argument", "费用授权必须为明确的布尔值。")
                args["allow_model_calls"] = args.pop("fee_confirmed")
            if action == "link-submit":
                result = self._public_job(AdditionWorkflow(self.store).submit(**args))
            elif action == "link-status":
                job = JobManager(self.store).get(args["job_id"])
                if job["kind"] != "add":
                    raise ContextError("invalid_argument", "此入口只能读取单链接任务。")
                result = self._public_job(job)
            elif action in {"connection-run", "connection-start", "connection-cancel"}:
                if self.connection_runner is None:
                    if action != "connection-run":
                        raise ContextError("connection_disabled", "后台尚未授权页面连接及独立浏览器目录。")
                    result = {
                        "enabled": False,
                        "active": False,
                        "state": "disabled",
                        "run_id": None,
                        "headless": None,
                        "model_requests": 0,
                    }
                elif action == "connection-run":
                    result = self.connection_runner.status()
                elif action == "connection-start":
                    result = self.connection_runner.start(**args)
                else:
                    result = self.connection_runner.cancel(**args)
            elif action == "connection":
                result = self.sources.connection()
            elif action == "self-source-create":
                result = self.sources.create_self(**args)
            elif action == "models":
                result = self.models.settings()
            elif action == "model-save":
                result = self.models.save(**args)
            elif action == "sources":
                result = self.sources.overview(**args)
            elif action == "source-create":
                result = self.sources.create(**args)
            elif action == "source-submit":
                result = self.sources.submit(**args)
            elif action == "source-timer":
                result = self.sources.timer(**args)
            elif action == "overview":
                result = self.overview(**args)
            elif action == "automatic":
                result = self._automatic(self.schedule.configure(**args))
            elif action == "history":
                result = self.schedule.history(**args)
                result = {
                    "batch_id": result["id"],
                    "job_ids": result["job_ids"],
                    "count": len(result["job_ids"]),
                    "max_calls": result["max_calls_total"],
                }
            elif action == "cancel":
                result = self._public_job(JobManager(self.store).cancel(args["job_id"]))
            elif action == "retry-preview":
                result = ModelRecovery(self.store).preview(**args)
            elif action == "retry":
                result = self._public_job(ModelRecovery(self.store).retry(**args))
            elif action == "resume-preview":
                result = self.schedule.resume_preview(**args)
            else:
                result = self.schedule.resume(**args)
            return envelope(result)
        except ContextError as error:
            return envelope(error=error)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return envelope(error=ContextError("invalid_argument", "管理请求字段类型无效。"))
