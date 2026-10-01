"""Optional identity-bound AI addition. No downloads, batch commands or model authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from collection_context.application.contracts import ContextError, canonical_bytes, envelope, valid_id

if TYPE_CHECKING:
    from collection_context.workflows.addition import AdditionWorkflow

WRITE_TOOLS = frozenset({"add_collection", "get_job"})


class AgentAdditionGateway:
    def __init__(self, workflow: AdditionWorkflow, principal: str):
        valid_id(principal)
        if principal == "local_owner":
            raise ContextError("permission_denied", "AI 添加入口需要独立、可撤销的访问身份。")
        self.workflow, self.principal = workflow, principal

    @staticmethod
    def public_job(job: dict[str, Any]) -> dict[str, Any]:
        result = (job["stages"].get("link_import") or {}).get("result") or {}
        imported = result.get("result") or {}
        return {
            "job_id": job["id"],
            "state": job["state"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "material_ref": imported.get("material_ref"),
            "metadata": imported.get("metadata", "pending"),
            "download": "not_requested",
            "extraction": "not_requested",
            "max_calls": 0,
            "model_requests": 0,
            "error_code": (job.get("error") or {}).get("code"),
            "content_untrusted": True,
        }

    def dispatch(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.workflow.authorize(self.principal)
            required = {"url", "idempotency_key"} if action == "add_collection" else {"job_id"}
            if action not in WRITE_TOOLS:
                raise ContextError("permission_denied", "此授权仅允许单链接添加及查看自己的添加任务。")
            if (
                not isinstance(arguments, dict)
                or set(arguments) != required
                or len(canonical_bytes(arguments)) > 4096
            ):
                raise ContextError("invalid_argument", "字段不完整、包含越权字段或超过大小限制。")
            if action == "add_collection":
                job = self.workflow.submit(
                    url=arguments["url"],
                    download=False,
                    idempotency_key=arguments["idempotency_key"],
                    source_confirmed=True,
                    principal=self.principal,
                )
            else:
                job = self.workflow.jobs.get(arguments["job_id"], principal=self.principal)
            if not self.workflow.admitted(self.workflow.store.snapshot(), job):
                raise ContextError("not_found", "此身份没有可查看的添加任务。")
            return envelope(self.public_job(job))
        except ContextError as error:
            return envelope(error=error)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return envelope(error=ContextError("invalid_argument", "请求字段或类型无效。"))
