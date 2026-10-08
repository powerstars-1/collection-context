"""Owner use cases: register durable work; catalog reads resolve credentials, never infer."""

from __future__ import annotations

from typing import Any

from collection_context.application.connection_runner import ConnectionRunner
from collection_context.application.contracts import ContextError, envelope
from collection_context.application.model_recovery import ModelRecovery
from collection_context.application.model_setup import ModelSetup
from collection_context.application.model_services import ModelServices
from collection_context.application.source_management import SourceManagement
from collection_context.infrastructure.secrets import CredentialBackend
from collection_context.infrastructure.download_limits import DEFAULT_DOWNLOAD_MB, MAX_DOWNLOAD_MB, validate_download_mb
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs, preparation_error
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.jobs import JobManager
from collection_context.workflows.media_preparation import MediaPreparation
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
        self.model_services = ModelServices(self.models)
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
            "jobs": [self._job_with_item(j, state) for j in jobs[offset : offset + 20]],
            "total_jobs": len(jobs),
            "next_offset": offset + 20 if offset + 20 < len(jobs) else None,
            "prepared": [self._prepared(value) for value in prepared[offset : offset + 20]],
            "total_prepared": len(prepared),
            "next_prepared_offset": offset + 20 if offset + 20 < len(prepared) else None,
            "execution": "separately_authorized_worker",
            "worker": self.store.storage.observe_worker(self.store.files),
        }

    def activity(self) -> dict[str, Any]:
        """One bounded progress read, without loading media or paging history."""
        from collection_context.application.contracts import digest
        state = self.store.snapshot()
        jobs = sorted((j for j in state["jobs"].values() if j["principal"] == "local_owner"),
                      key=lambda j: (j["created_at"], j["id"]), reverse=True)
        active = [j for j in jobs if j["state"] in {"queued", "running"}]
        visible = {j["id"]: j for j in jobs[:20]}
        visible.update({j["id"]: j for j in active[:40]})
        public = []
        for job in visible.values():
            value = self._job_with_item(job, state)
            value.pop("calls", None)
            public.append(value)
        items = [i for i in state["items"].values() if not i["excluded"]]
        connection_active = bool(self.connection_runner and self.connection_runner.status().get("active"))
        return {
            "jobs": public, "active_count": len(active), "connection_active": connection_active,
            "completion_revision": digest([(j["id"], j["state"]) for j in jobs]),
            "source_revision": digest([(j["id"], j["state"], j["updated_at"]) for j in jobs if j["kind"] == "sync"]),
            "library_revision": digest([(i["id"], i["content_hash"], i.get("prepared_input"),
                i.get("relations"), i.get("downloaded_media"),
                {k: (v.get("version"), v.get("state")) for k, v in i["artifacts"].items()}) for i in items]),
            "worker": self.store.storage.observe_worker(self.store.files),
            "model_requests": 0,
        }

    def _job_with_item(self, job: dict, state: dict) -> dict:
        value = self._public_job(job)
        input_id = job["payload"].get("extraction", {}).get("input_id")
        material_ref = state.get("prepared_inputs", {}).get(input_id, {}).get("material_ref")
        material_ref = material_ref or value.get("link_result", {}).get("material_ref") or job["payload"].get("extraction", {}).get("material_ref")
        item = state["items"].get(material_ref, {})
        if "media_preparation" in job["payload"]:
            material_ref = job["payload"]["media_preparation"]["material_ref"]
            item = state["items"].get(material_ref, {})
        value.update(material_ref=material_ref, title=item.get("title") or {"sync": "来源同步", "add": "链接入库", "process": "内容提取"}.get(job["kind"], "处理任务"),
            calls=[{"stage": c["stage"], "state": c["state"], "model": c.get("actual_model"),
                "usage": c.get("usage")} for c in job["calls"]])
        if job["payload"].get("extraction", {}).get("workflow") == "model_check":
            value["title"] = "模型连接检测"
            value["can_retry"] = False
        return value

    @staticmethod
    def _automatic(value: dict[str, Any]) -> dict[str, Any]:
        return {**value, "paused_job_ids": value["paused_job_ids"][:20]}

    def _prepared(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            media = PreparedInputs(self.store).load(value["input_id"])
            counts = {"audio": len(media["audio"]), "vision": 0 if media["coverage"].get("audio_only") else len(media["frames"])}
            reused = {"audio": 0, "vision": 0}
            try:
                from collection_context.application.contracts import digest
                from collection_context.processing.stages import media_stage_identity, model_identity, VERSION
                from collection_context.processing.profiles import ModelCatalog
                state = self.store.snapshot()
                signatures = {stage.get("signature") for job in state["jobs"].values()
                    if job["principal"] == "local_owner" for stage in job["stages"].values()
                    if stage["state"] in {"ready", "partial", "not_applicable"}}
                for role, entries in (("audio", media["audio"]), ("vision", media["frames"] if counts["vision"] else [])):
                    if not entries: continue
                    profile_id = state["settings"].get("model_roles", {}).get(role)
                    if not profile_id: continue
                    model = model_identity(ModelCatalog.from_state(state, profile_id))
                    for entry in entries:
                        name = "audio_" + entry["evidence_id"] if role == "audio" else "screen_" + entry["candidate"]["evidence_id"]
                        declared = media_stage_identity(media["material_ref"], role, entry, model)
                        if digest([name, digest({"declared_input": declared, "dependencies": {}}), VERSION]) in signatures:
                            reused[role] += 1
            except ContextError:
                pass  # Preparing media does not require model configuration.
            return {
                **value,
                "audio_segments": len(media["audio"]),
                "visual_frames": len(media["frames"]),
                "planned_calls_before_reuse": counts["audio"] + counts["vision"] + 1,
                "planned_new_calls": counts["audio"] + counts["vision"] - sum(reused.values()) + 1,
                "reusable_audio_segments": reused["audio"], "reusable_visual_frames": reused["vision"],
                "has_audio": media["coverage"].get("has_audio"),
                "audio_only": media["coverage"].get("audio_only", False),
                "selection_reduced": bool(media["coverage"].get("candidate_compactions") or media["coverage"].get("budget_reductions")),
                "preparation_issues": [
                    {"role": role, "code": code}
                    for role in ("audio", "vision")
                    if (code := preparation_error(media["coverage"], role))
                ],
            }
        except ContextError as error:
            return {**value, "planned_calls_before_reuse": None, "preparation_error": error.code}

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        output = {
            "job_id": job["id"],
            "state": job["state"],
            "kind": job["kind"],
            "input_id": job["payload"].get("extraction", {}).get("input_id"),
            "mode": job["payload"].get("dispatch", {}).get("mode", "manual"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "preparation_mode": job["payload"].get("media_preparation", {}).get("mode"),
            "planned_stages": [stage["name"] for stage in job["payload"].get("plan", [])],
            "max_calls": job["budget"]["max_calls"],
            "recorded_calls": len(job["calls"]),
            "unknown_calls": sum(c["state"] in {"intent", "unknown"} for c in job["calls"]),
            "usage_missing_calls": sum(
                c["state"] == "completed" and c.get("usage") is None for c in job["calls"]
            ),
            "stages": {name: stage["state"] for name, stage in job["stages"].items()},
            "stage_details": {
                name: {"state": stage["state"], "error_code": (stage.get("error") or {}).get("code"),
                       "error_message": (stage.get("error") or {}).get("message"),
                       "reused": stage.get("reused", False),
                       "progress": (stage.get("result") or {}).get("progress")}
                for name, stage in job["stages"].items()
            },
            "error_code": job["error"]["code"] if job["error"] else None,
            "error_message": job["error"]["message"] if job["error"] else None,
            "attempt": job.get("attempt", 1),
            "parent_job_id": job.get("recovery", {}).get("parent_job_id"),
            "can_retry": job["kind"] == "process"
            and "extraction" in job["payload"]
            and job["state"] in {"partial", "failed", "cancelled", "blocked"},
        }
        if job["kind"] == "add":
            output["link_url"] = job["payload"].get("link", {}).get("url")
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
                "creator-resolve": ({"creator_url", "source_confirmed"}, {"creator_url", "source_confirmed"}),
                "connection-cancel": ({"run_id"}, {"run_id"}),
                "connection-logout": (set(), set()),
                "connection": (set(), set()),
                "self-source-create": (
                    {"kind", "collection_id", "connection_version", "limit", "download", "source_confirmed"},
                    {"kind", "collection_id", "connection_version", "limit", "download", "source_confirmed"},
                ),
                "models": (set(), set()),
                "model-services": (set(), set()),
                "model-service-save": (
                    {"service_id", "name", "provider", "base_url", "api_key", "expected_revision"},
                    {"service_id", "name", "provider", "base_url", "api_key", "expected_revision", "api"}),
                "model-assignments": ({"assignments", "expected_profiles"}, {"assignments", "expected_profiles"}),
                "model-service-remove": ({"service_id"}, {"service_id"}),
                "model-list": ({"service_id"}, {"service_id"}),
                "model-check": ({"role", "expected_profile_id", "idempotency_key", "fee_confirmed"},
                    {"role", "expected_profile_id", "idempotency_key", "fee_confirmed"}),
                "model-check-status": ({"job_id"}, {"job_id"}),
                "preferences": (set(), set()),
                "preferences-save": ({"download_media"}, {"download_media", "max_download_mb"}),
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
                "source-history": ({"scope_id"}, {"scope_id", "offset"}),
                "source-create": ({"creator_url", "limit", "download"}, {"creator_url", "limit", "download"}),
                "source-edit": ({"config_id", "limit", "download"}, {"config_id", "limit", "download"}),
                "source-remove": ({"config_id"}, {"config_id"}),
                "note-save": ({"material_ref", "text", "expected_version"}, {"material_ref", "text", "expected_version"}),
                "process-preview": ({"material_ref"}, {"material_ref"}),
                "media-prepare": ({"material_ref", "mode", "idempotency_key"}, {"material_ref", "mode", "idempotency_key"}),
                "source-submit": (
                    {"config_id", "idempotency_key", "source_confirmed"},
                    {"config_id", "idempotency_key", "source_confirmed"},
                ),
                "source-timer": (
                    {"config_id", "enabled", "interval_minutes", "reset_blocked", "source_confirmed"},
                    {"config_id", "enabled", "interval_minutes", "reset_blocked", "source_confirmed"},
                ),
                "overview": (set(), {"offset"}),
                "activity": (set(), set()),
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
            if action == "media-prepare":
                result = self._public_job(MediaPreparation(self.store).submit(**args))
            elif action == "link-submit":
                result = self._public_job(AdditionWorkflow(self.store).submit(**args))
            elif action == "link-status":
                job = JobManager(self.store).get(args["job_id"])
                if job["kind"] != "add":
                    raise ContextError("invalid_argument", "此入口只能读取单链接任务。")
                result = self._public_job(job)
            elif action in {"connection-run", "connection-start", "connection-cancel", "connection-logout", "creator-resolve"}:
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
                elif action == "connection-logout":
                    result = self.connection_runner.start(mode="logout", source_confirmed=True)
                elif action == "creator-resolve":
                    result = self.connection_runner.start(mode="creator", **args)
                else:
                    result = self.connection_runner.cancel(**args)
            elif action == "connection":
                result = self.sources.connection()
            elif action == "self-source-create":
                result = self.sources.create_self(**args)
            elif action == "models":
                result = self.models.settings()
            elif action == "model-services":
                result = self.model_services.settings()
            elif action == "model-service-save":
                result = self.model_services.save(**args)
            elif action == "model-assignments":
                result = self.model_services.assign(**args)
            elif action == "model-service-remove":
                result = self.model_services.remove(**args)
            elif action == "model-list":
                result = self.model_services.model_list(**args)
            elif action == "model-check":
                from collection_context.processing.model_check import submit
                worker = self.store.storage.observe_worker(self.store.files)
                if not worker["online"] or not worker.get("capabilities", {}).get("model_calls"):
                    raise ContextError("model_execution_disabled", "当前后台未开启云模型执行。")
                result = self._public_job(submit(self.store, **args))
            elif action == "model-check-status":
                job = JobManager(self.store).get(args["job_id"])
                if job["payload"].get("extraction", {}).get("workflow") != "model_check":
                    raise ContextError("invalid_argument", "这不是模型检测任务。")
                result = self._job_with_item(job, self.store.snapshot())
                saved = job["stages"].get("model_check", {}).get("result")
                result["probe_result"] = JobManager(self.store).read_result(saved)["output"] if saved else None
            elif action == "preferences":
                settings = self.store.snapshot()["settings"]
                result = {"download_media": settings.get("download_media_default", True),
                    "max_download_mb": settings.get("max_download_mb", DEFAULT_DOWNLOAD_MB),
                    "max_download_mb_allowed": MAX_DOWNLOAD_MB,
                    "workspace_path": str(self.store.files.root)}
            elif action == "preferences-save":
                if type(args["download_media"]) is not bool:
                    raise ContextError("invalid_argument", "请选择是否保存原媒体。")
                if "max_download_mb" in args:
                    validate_download_mb(args["max_download_mb"])
                def save_preferences(state):
                    state["settings"]["download_media_default"] = args["download_media"]
                    if "max_download_mb" in args:
                        state["settings"]["max_download_mb"] = args["max_download_mb"]
                    return state["settings"].get("max_download_mb", DEFAULT_DOWNLOAD_MB)
                maximum = self.store.transact(save_preferences)
                result = {"download_media": args["download_media"], "max_download_mb": maximum, "model_requests": 0}
            elif action == "model-save":
                result = self.models.save(**args)
            elif action == "sources":
                result = self.sources.overview(**args)
            elif action == "source-history":
                result = self.sources.history(**args)
            elif action == "source-create":
                result = self.sources.create(**args)
            elif action == "source-edit":
                result = self.sources.edit(**args)
            elif action == "source-remove":
                result = self.sources.remove(**args)
            elif action == "note-save":
                result = self.save_note(**args)
            elif action == "process-preview":
                current_state = self.store.snapshot()
                item = current_state["items"].get(args["material_ref"])
                if not item or item["excluded"]:
                    raise ContextError("not_found", "资料不存在。")
                if not item.get("prepared_input"):
                    saved = item.get("downloaded_media")
                    if not saved or saved.get("content_hash") != item["content_hash"] or saved.get("source_asset_hash") != item.get("source_asset_hash"):
                        raise ContextError("media_not_prepared", "这条资料尚未保存原媒体，可以只保存这一条的视频 / 图片。")
                    result = {"material_ref": item["id"], "title": item["title"], "input_id": None,
                        "media_saved": True, "audio_segments": 0, "visual_frames": 0,
                        "planned_calls_before_reuse": None, "preparation_issues": [], "has_audio": None}
                else:
                    result = self._prepared({"material_ref": item["id"], "title": item["title"], "input_id": item["prepared_input"]})
                    result["media_saved"] = True
                result["models"] = {role: value["model"] for role, value in self.models.catalog.public_settings().items()}
                worker = self.store.storage.observe_worker(self.store.files)
                result["model_execution_enabled"] = worker["online"] is True and worker.get("capabilities", {}).get("model_calls") is True
                from collection_context.processing.pi_client import node_runtime
                result["model_runtime_ready"] = node_runtime() is not None
                related = [j for j in current_state["jobs"].values()
                           if j["principal"] == "local_owner" and j["kind"] == "process"
                           and item.get("prepared_input")
                           and j["payload"].get("extraction", {}).get("input_id") == item.get("prepared_input")]
                active = [j for j in related if j["state"] in {"queued", "running"}]
                latest = max(active or related, key=lambda j: (j["created_at"], j["id"]), default=None)
                result["latest_job"] = self._public_job(latest) if latest else None
                preparations = [self._job_with_item(j, current_state) for j in current_state["jobs"].values()
                    if j["principal"] == "local_owner" and j["kind"] == "add" and j["state"] in {"queued", "running"}]
                result["media_job"] = next((j for j in preparations if j.get("material_ref") == item["id"]), None)
            elif action == "source-submit":
                result = self.sources.submit(**args)
            elif action == "source-timer":
                result = self.sources.timer(**args)
            elif action == "overview":
                result = self.overview(**args)
            elif action == "activity":
                result = self.activity()
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

    def save_note(self, *, material_ref: str, text: str, expected_version: str | None) -> dict:
        from collection_context.library.index import FileIndex
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ContextError("invalid_argument", "备注不能为空，最多 10 万字。")
        def change(state):
            item = state["items"].get(material_ref)
            if not item or item["excluded"]:
                raise ContextError("not_found", "资料不存在。")
            if item["artifacts"].get("user_note", {}).get("version") != expected_version:
                raise ContextError("version_changed", "备注已变化，请重新读取后编辑。")
            artifact = self.store._write_artifacts(item, {"user_note": {"text": text,
                "processor_version": "owner-note-v1", "coverage": {"origin": "owner"}}},
                expected_content_hash=item["content_hash"])["user_note"]
            artifact["owner_edit"] = True
            return artifact
        saved = self.store.transact(change)
        FileIndex(self.store).rebuild()
        return {"version": saved["version"], "model_requests": 0}
