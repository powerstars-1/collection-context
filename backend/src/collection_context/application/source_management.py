"""Owner page source planning; never opens a browser, reads login data or invokes models."""

from __future__ import annotations

from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.library.store import LibraryStore
from collection_context.workflows.connection import ConnectionCatalog
from collection_context.workflows.source_schedule import SourceSchedule
from collection_context.workflows.synchronization import SynchronizationWorkflow


class SourceManagement:
    def __init__(self, store: LibraryStore):
        self.store = store
        self.workflow = SynchronizationWorkflow(store)
        self.schedule = SourceSchedule(self.workflow)

    def overview(self, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ContextError("invalid_argument", "同步范围分页位置无效。")
        state = self.store.snapshot()
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
            scopes.append(
                {
                    "scope_id": scope_id,
                    "config_id": config_id,
                    "kind": plan["kind"],
                    "creator_url": plan["creator_url"],
                    "collection_id": plan["collection_id"],
                    "limit": plan["limit"],
                    "download": plan["download"],
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
