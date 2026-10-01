"""Own durable source timer: one bounded batch per scope, no catch-up or implicit fees."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id, validate_time
from collection_context.workflows.policy import sync_schedule_policy
from collection_context.workflows.synchronization import SynchronizationWorkflow


def moment(value: str) -> datetime:
    return datetime.fromisoformat(validate_time(value).replace("Z", "+00:00"))


def later(value: str, minutes: int) -> str:
    return (moment(value) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class SourceSchedule:
    def __init__(self, workflow: SynchronizationWorkflow, *, clock: Callable[[], str] = utc_now):
        self.workflow, self.store, self.clock = workflow, workflow.store, clock

    @staticmethod
    def _entry(state: dict[str, Any], scope: str) -> tuple[dict[str, Any], dict[str, Any]]:
        entry = state["settings"].get("sync_schedules", {}).get(scope)
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"schedule_id", "enabled", "next_due_at", "last_job_id", "blocked_job_id", "sequence"}
            or type(entry["enabled"]) is not bool
            or type(entry["sequence"]) is not int
            or entry["sequence"] < 0
        ):
            raise ContextError("invalid_sync_schedule", "定时同步状态不完整，未猜测继续。")
        policy = sync_schedule_policy(state, entry["schedule_id"])
        if policy["scope_id"] != scope:
            raise ContextError("invalid_sync_schedule", "定时同步范围身份不匹配。")
        moment(entry["next_due_at"])
        for field in ("last_job_id", "blocked_job_id"):
            ref = entry[field]
            if ref is not None:
                valid_id(ref)
                job = state["jobs"].get(ref)
                if (
                    not job
                    or state.get("sync_schedule_admissions", {}).get(ref) != entry["schedule_id"]
                    or job["principal"] != "local_owner"
                    or job["kind"] != "sync"
                    or job["payload"].get("sync") != {"config_id": policy["config_id"]}
                    or job["payload"].get("dispatch")
                    != {"mode": "scheduled_sync", "schedule_id": entry["schedule_id"]}
                    or job["budget"]["max_calls"] != 0
                    or job["calls"]
                ):
                    raise ContextError("invalid_sync_schedule", "定时同步任务登记不符。")
        return entry, policy

    @staticmethod
    def _pending_scope(state: dict[str, Any], scope: str) -> bool:
        return any(
            j["principal"] == "local_owner"
            and j["kind"] == "sync"
            and j["state"] in {"queued", "running"}
            and state.get("sync_scope_configs", {})
            .get(j["payload"].get("sync", {}).get("config_id"), {})
            .get("scope_id")
            == scope
            for j in state["jobs"].values()
        )

    @staticmethod
    def _due(state: dict[str, Any], entry: dict, policy: dict) -> str:
        value = entry["next_due_at"]
        job = state["jobs"].get(entry["last_job_id"])
        if job and job["state"] == "succeeded":
            completed_due = later(job["updated_at"], policy["interval_minutes"])
            if moment(completed_due) > moment(value):
                value = completed_due
        return value

    def status(self) -> dict[str, Any]:
        return self.status_from_state(self.store.snapshot())

    def status_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        scopes = []
        for scope in sorted(state["settings"].get("sync_schedules", {})):
            entry, policy = self._entry(state, scope)
            last = state["jobs"].get(entry["last_job_id"])
            blocked = entry["blocked_job_id"] or (
                last["id"]
                if last and last["state"] in {"blocked", "failed", "partial", "cancelled"}
                else None
            )
            scopes.append(
                {
                    "scope_id": scope,
                    "config_id": policy["config_id"],
                    "schedule_id": entry["schedule_id"],
                    "enabled": entry["enabled"],
                    "interval_minutes": policy["interval_minutes"],
                    "next_due_at": self._due(state, entry, policy),
                    "last_job_id": entry["last_job_id"],
                    "last_state": last["state"] if last else None,
                    "blocked_job_id": blocked,
                    "error_code": (last.get("error") or {}).get("code") if last else None,
                    "awaiting_worker": bool(
                        state["settings"].get("auto_sync") is True and entry["enabled"] and not blocked
                    ),
                }
            )
        return {"scopes": scopes, "enabled_count": sum(s["enabled"] for s in scopes), "model_requests": 0}

    def configure(
        self,
        config_id: str,
        *,
        enabled: bool,
        allow_source_sync: bool = False,
        interval_minutes: int = 60,
        reset_blocked: bool = False,
    ) -> dict[str, Any]:
        if type(enabled) is not bool or type(reset_blocked) is not bool:
            raise ContextError("invalid_argument", "定时同步及失败确认开关须为布尔值。")
        if enabled and allow_source_sync is not True:
            raise ContextError("source_authorization_required", "启用定时同步须明确允许访问已选来源。")
        if type(interval_minutes) is not int or not 60 <= interval_minutes <= 1440:
            raise ContextError("invalid_argument", "定时同步间隔须为60至1440分钟。")
        if reset_blocked and not enabled:
            raise ContextError("invalid_argument", "重新尝试须同时明确启用已选来源。")
        plan = self.workflow.plan(config_id)
        now = validate_time(self.clock())

        def change(state):
            entries = state["settings"].setdefault("sync_schedules", {})
            scope = plan["scope_id"]
            if scope not in entries and len(entries) >= 64:
                raise ContextError("source_scope_limit", "首版最多登记64个定时范围。")
            old = self._entry(state, scope) if scope in entries else None
            # Disabling can never silently replace the pinned configuration or interval.
            if old and not enabled:
                old[0]["enabled"] = False
            elif old and old[1]["config_id"] == config_id and old[1]["interval_minutes"] == interval_minutes:
                entry, _ = old
                entry["enabled"] = enabled
                last = state["jobs"].get(entry["last_job_id"])
                if reset_blocked:
                    if last and last["state"] in {"queued", "running"}:
                        raise ContextError("source_sync_busy", "当前批次仍未结束，不能确认重试。")
                    entry.update(blocked_job_id=None, last_job_id=None, next_due_at=now)
            else:
                if old and enabled:
                    last = state["jobs"].get(old[0]["last_job_id"])
                    if last and last["state"] in {"queued", "running"}:
                        raise ContextError("source_sync_busy", "先完成或取消固定批次，再更改定时范围。")
                    if last and last["state"] != "succeeded" and not reset_blocked:
                        raise ContextError(
                            "source_retry_confirmation_required", "失败批次需显式确认后才能更改并重试。"
                        )
                policy = {
                    "schema_version": 1,
                    "config_id": config_id,
                    "scope_id": scope,
                    "interval_minutes": interval_minutes,
                    "created_at": now,
                    "generation": state["generation"],
                }
                identity = "z_" + digest(policy)
                state.setdefault("sync_schedule_policies", {})[identity] = policy
                entries[scope] = {
                    "schedule_id": identity,
                    "enabled": enabled,
                    "next_due_at": now,
                    "last_job_id": None,
                    "blocked_job_id": None,
                    "sequence": 0,
                }
            state["settings"]["auto_sync"] = any(e["enabled"] for e in entries.values())

        self.store.transact(change)
        return self.status()

    def admit_due(self, *, limit: int = 5) -> list[str]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ContextError("invalid_argument", "每轮最多登记20个来源批次。")
        now = validate_time(self.clock())
        state = self.store.snapshot()
        if state["settings"].get("auto_sync") is not True:
            return []
        candidates = []
        for scope in sorted(state["settings"].get("sync_schedules", {})):
            entry, policy = self._entry(state, scope)
            last = state["jobs"].get(entry["last_job_id"])
            if (
                not entry["enabled"]
                or entry["blocked_job_id"]
                or last
                and last["state"] in {"queued", "running"}
            ):
                continue
            if (
                last
                and last["state"] != "succeeded"
                or moment(self._due(state, entry, policy)) <= moment(now)
            ):
                if last and last["state"] != "succeeded" or not self._pending_scope(state, scope):
                    candidates.append(scope)
        if not candidates:
            return []  # Idle polling is read-only; no manifest generations or hidden platform requests.

        def change(current):
            admitted: list[str] = []
            if current["settings"].get("auto_sync") is not True:
                return admitted
            for scope in candidates[:limit]:
                entry, policy = self._entry(current, scope)
                last = current["jobs"].get(entry["last_job_id"])
                if (
                    not entry["enabled"]
                    or entry["blocked_job_id"]
                    or last
                    and last["state"] in {"queued", "running"}
                ):
                    continue
                if last and last["state"] != "succeeded":
                    entry["blocked_job_id"] = last["id"]
                    continue  # Terminal source failures never become timer retries.
                if moment(self._due(current, entry, policy)) > moment(now):
                    continue
                if self._pending_scope(current, scope):
                    continue  # A manual or recovered batch for this same scope takes precedence.
                payload = {
                    "sync": {"config_id": policy["config_id"]},
                    "dispatch": {"mode": "scheduled_sync", "schedule_id": entry["schedule_id"]},
                }
                mutation = self.workflow.jobs._submission(
                    "sync",
                    payload,
                    idempotency_key="source-timer-" + digest([entry["schedule_id"], entry["sequence"]]),
                    max_calls=0,
                )
                job = mutation(current)
                current.setdefault("sync_schedule_admissions", {})[job["id"]] = entry["schedule_id"]
                entry.update(
                    last_job_id=job["id"],
                    next_due_at=later(now, policy["interval_minutes"]),
                    sequence=entry["sequence"] + 1,
                )
                admitted.append(job["id"])
            return admitted

        return self.store.transact(change)
