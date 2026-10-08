"""Owner page source planning; never opens a browser, reads login data or invokes models."""

from __future__ import annotations

from typing import Any

from collection_context.application.contracts import ContextError, digest
from collection_context.library.store import LibraryStore
from collection_context.workflows.connection import ConnectionCatalog
from collection_context.workflows.source_schedule import SourceSchedule
from collection_context.workflows.synchronization import SynchronizationWorkflow, sync_plan


class SourceManagement:
    def __init__(self, store: LibraryStore):
        self.store = store
        self.workflow = SynchronizationWorkflow(store)
        self.schedule = SourceSchedule(self.workflow)

    def run_report(self, state: dict, job: dict) -> dict:
        plan = self.workflow.plan_from_state(state, job["payload"]["sync"]["config_id"])
        result = (job.get("stages", {}).get("source_sync", {}).get("result") or {})
        imported = result.get("imported", {})
        coverage = result.get("coverage") or {}
        errors = [row.get("error") for row in imported.values() if row.get("download") == "blocked" and row.get("error")]
        return {"job_id": job["id"], "state": job["state"], "created_at": job["created_at"], "updated_at": job["updated_at"],
            "limit": plan["limit"], "download": plan["download"],
            "committed_count": coverage.get("committed_count", len(imported)),
            "new_count": sum(row.get("created") is True for row in imported.values()),
            "existing_count": sum(row.get("created") is False for row in imported.values()),
            "saved_count": sum(row.get("download") == "ready" for row in imported.values()),
            "failed_count": sum(row.get("download") == "blocked" for row in imported.values()),
            "preparation_pending_count": sum(row.get("preparation") == "blocked" for row in imported.values()),
            "limit_reached": coverage.get("limit_reached", False),
            "error_code": (job.get("error") or {}).get("code"),
            "error_message": (job.get("error") or {}).get("message") or (errors[0]["message"] if errors else None)}

    def overview(self, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ContextError("invalid_argument", "同步范围分页位置无效。")
        state = self.store.snapshot()
        record = ConnectionCatalog.record_from_state(state)
        folders = {f["collection_id"]: f["name"] for f in record["folders"]} if record else {}
        timers = {s["scope_id"]: s for s in self.schedule.status_from_state(state)["scopes"]}
        current = sorted(state.get("sync_current_scopes", {}).items())
        scopes = []
        for scope_id, config_id in current[offset : offset + 20]:
            plan = self.workflow.plan_from_state(state, config_id)
            if scope_id != plan["scope_id"]:
                raise ContextError("invalid_sync_plan", "当前范围登记不符，未猜测继续。")
            observed = state["scopes"].get(scope_id, {})
            timer = timers.get(scope_id)
            jobs = sorted(
                (
                    j
                    for j in state["jobs"].values()
                    if j["principal"] == "local_owner"
                    and j["kind"] == "sync"
                    and state.get("sync_scope_configs", {})
                    .get(j["payload"].get("sync", {}).get("config_id"), {})
                    .get("scope_id")
                    == scope_id
                ),
                key=lambda j: (j["created_at"], j["id"]),
                reverse=True,
            )
            download_report = None
            last_report = self.run_report(state, jobs[0]) if jobs else None
            if jobs and last_report["download"] and jobs[0]["state"] not in {"queued", "running"}:
                imported = (jobs[0].get("stages", {}).get("source_sync", {}).get("result") or {}).get("imported", {})
                if imported:
                    errors = [row["error"] for row in imported.values() if row.get("download") == "blocked" and row.get("error")]
                    download_report = {
                        "saved_count": sum(row.get("download") == "ready" for row in imported.values()),
                        "failed_count": sum(row.get("download") == "blocked" for row in imported.values()),
                        "preparation_pending_count": sum(row.get("preparation") == "blocked" for row in imported.values()),
                        "error_message": (
                            errors[0]["message"] + (" 可在处理设置调整单个文件最大下载大小。" if errors[0]["code"] == "download_limit" else "")
                            if errors else None
                        ),
                    }
            scopes.append(
                {
                    "scope_id": scope_id,
                    "config_id": config_id,
                    "kind": plan["kind"],
                    "title": folders.get(plan["collection_id"], "收藏夹") if plan["kind"] == "collection"
                    else state["settings"].get("creator_names", {}).get(plan["creator_url"], plan["creator_url"]) if plan["kind"] == "creator"
                    else {"liked": "我的喜欢", "saved": "我的收藏"}.get(plan["kind"], plan["kind"]),
                    "creator_url": plan["creator_url"],
                    "collection_id": plan["collection_id"],
                    "limit": plan["limit"],
                    "download": plan["download"],
                    "download_report": download_report,
                    "latest_report": last_report,
                    "history_count": len(jobs),
                    "recent_runs": [self.run_report(state, job) for job in jobs[:5]],
                    "account_matches": plan["kind"] == "creator" or bool(record and record["account"] and plan["account_ref"] == record["account"]["account_ref"]),
                    "status": observed.get("status", "not_started"),
                    "complete": observed.get("complete") is True,
                    "committed_count": observed.get("committed_count"),
                    "last_sync_at": observed.get("last_sync_at"),
                    "pending_count": sum(j["state"] in {"queued", "running"} for j in jobs),
                    "latest_job": {
                        "job_id": jobs[0]["id"],
                        "state": jobs[0]["state"],
                        "error_code": (jobs[0]["error"] or {}).get("code"),
                    }
                    if jobs
                    else None,
                    "timer": timer,
                    "timer_matches_current": timer is None or timer["config_id"] == config_id,
                }
            )
        worker = self.store.storage.observe_worker(self.store.files)
        return {
            "scopes": scopes,
            "total_scopes": len(current),
            "next_offset": offset + 20 if offset + 20 < len(current) else None,
            "model_requests": 0,
            "worker_online": worker["online"],
            "worker": worker,
            "connection": "independent_login_required",
        }

    def create(self, *, creator_url: str, limit: int, download: bool) -> dict[str, Any]:
        value = self.workflow.configure(
            "creator", creator_url=creator_url, limit=limit, download=download, create_only=True
        )
        return {
            k: value[k]
            for k in ("config_id", "scope_id", "kind", "creator_url", "limit", "download", "model_requests")
        }

    def connection(self) -> dict:
        return ConnectionCatalog(self.store).status()

    def edit(self, *, config_id: str, limit: int, download: bool) -> dict:
        def change(state):
            plan = self.workflow.plan_from_state(state, config_id)
            scope = plan["scope_id"]
            if state.get("sync_current_scopes", {}).get(scope) != config_id:
                raise ContextError("source_plan_changed", "来源设置已变化，请刷新后重试。")
            if SourceSchedule._pending_scope(state, scope):
                raise ContextError("source_sync_busy", "同步尚未结束，请稍后再编辑。")
            updated = sync_plan(plan["kind"], account_ref=plan["account_ref"],
                collection_id=plan["collection_id"], creator_url=plan["creator_url"], limit=limit, download=download)
            identity = "y_" + digest(updated)
            state["sync_scope_configs"][identity] = updated
            state["sync_current_scopes"][scope] = identity
            entry = state["settings"].get("sync_schedules", {}).get(scope)
            was_enabled = bool(entry and entry["enabled"])
            if entry:
                entry["enabled"] = False
            state["settings"]["auto_sync"] = any(e["enabled"] for e in state["settings"].get("sync_schedules", {}).values())
            return {"config_id": identity, "scope_id": scope, "kind": updated["kind"], "collection_id": updated["collection_id"],
                "creator_url": updated["creator_url"], "limit": updated["limit"], "download": updated["download"], "automatic_paused": was_enabled}
        updated = self.store.transact(change)
        return {**self.overview(), "updated_source": updated}

    def history(self, *, scope_id: str, offset: int = 0) -> dict:
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ContextError("invalid_argument", "记录分页位置无效。")
        state = self.store.snapshot()
        if scope_id not in state.get("sync_current_scopes", {}):
            raise ContextError("not_found", "同步来源不存在。")
        jobs = sorted((job for job in state["jobs"].values() if job["principal"] == "local_owner" and job["kind"] == "sync"
            and state.get("sync_scope_configs", {}).get(job["payload"].get("sync", {}).get("config_id"), {}).get("scope_id") == scope_id),
            key=lambda job: (job["created_at"], job["id"]), reverse=True)
        return {"runs": [self.run_report(state, job) for job in jobs[offset:offset+20]], "total": len(jobs),
            "next_offset": offset+20 if offset+20<len(jobs) else None, "model_requests": 0}

    def remove(self, *, config_id: str) -> dict:
        def change(state):
            plan = self.workflow.plan_from_state(state, config_id)
            scope = plan["scope_id"]
            if state.get("sync_current_scopes", {}).get(scope) != config_id:
                raise ContextError("source_plan_changed", "来源设置已变化，请刷新后重试。")
            if SourceSchedule._pending_scope(state, scope):
                raise ContextError("source_sync_busy", "请先完成或取消同步任务，再移除来源。")
            del state["sync_current_scopes"][scope]
            entry = state["settings"].get("sync_schedules", {}).get(scope)
            if entry:
                entry["enabled"] = False
            state["settings"]["auto_sync"] = any(e["enabled"] for e in state["settings"].get("sync_schedules", {}).values())
        self.store.transact(change)
        return self.overview()

    def create_self(
        self,
        *,
        kind: str,
        collection_id: str | None,
        connection_version: str,
        limit: int,
        download: bool,
        source_confirmed: bool,
    ) -> dict:
        self.authorize(source_confirmed)
        account_ref = ConnectionCatalog.authorize(
            self.store.snapshot(), connection_version, kind, collection_id
        )

        def admission(state):
            ConnectionCatalog.authorize(state, connection_version, kind, collection_id)

        value = self.workflow.configure(
            kind,
            account_ref=account_ref,
            collection_id=collection_id,
            limit=limit,
            download=download,
            create_only=True,
            admission=admission,
        )
        return {
            k: value[k]
            for k in ("config_id", "scope_id", "kind", "collection_id", "limit", "download", "model_requests")
        }

    @staticmethod
    def authorize(value: bool) -> None:
        if value is not True:
            raise ContextError("source_authorization_required", "请确认访问此固定来源及已选下载范围。")

    def submit(self, *, config_id: str, idempotency_key: str, source_confirmed: bool) -> dict[str, Any]:
        self.authorize(source_confirmed)
        job = self.workflow.submit(config_id, idempotency_key=idempotency_key, current_only=True)
        return {"job_id": job["id"], "state": job["state"], "model_requests": 0}

    def timer(
        self,
        *,
        config_id: str,
        enabled: bool,
        interval_minutes: int,
        reset_blocked: bool,
        source_confirmed: bool,
    ) -> dict[str, Any]:
        if type(source_confirmed) is not bool:
            raise ContextError("invalid_argument", "来源授权须为明确的布尔值。")
        # Pause the pinned timer, even if another CLI changed the current plan.
        # Enabling is an explicit authorization of the immutable displayed config.
        value = self.schedule.configure(
            config_id,
            enabled=enabled,
            interval_minutes=interval_minutes,
            reset_blocked=reset_blocked,
            allow_source_sync=source_confirmed,
        )
        return {"enabled_count": value["enabled_count"], "model_requests": 0}
