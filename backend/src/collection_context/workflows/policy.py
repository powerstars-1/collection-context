"""Immutable automatic-processing authority; no imports of executors or model clients."""

from __future__ import annotations

from typing import Any

from collection_context.application.contracts import ContextError, digest, valid_id, validate_time


def automatic_policy(state: dict[str, Any], identity: str) -> dict[str, Any]:
    valid_id(identity)
    policy = state.get("automatic_policies", {}).get(identity)
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {
            "schema_version",
            "boundary_generation",
            "created_at",
            "model_profiles",
            "max_calls",
            "max_new_tasks",
        }
        or policy.get("schema_version") != 1
        or identity != "a_" + digest(policy)
    ):
        raise ContextError("invalid_processing_policy", "自动策略损坏或版本不兼容，未沿用旧计费权限。")
    if (
        type(policy["boundary_generation"]) is not int
        or policy["boundary_generation"] < 0
        or type(policy["max_calls"]) is not int
        or not 0 <= policy["max_calls"] <= 1000
        or type(policy["max_new_tasks"]) is not int
        or not 1 <= policy["max_new_tasks"] <= 1000
        or not isinstance(policy["model_profiles"], dict)
        or set(policy["model_profiles"]) != {"audio", "vision", "summary"}
    ):
        raise ContextError("invalid_processing_policy", "自动策略额度或固定角色无效。")
    validate_time(policy["created_at"])
    for profile in policy["model_profiles"].values():
        valid_id(profile)
    return policy


def execution_allowed(state: dict[str, Any], job: dict[str, Any]) -> bool:
    dispatch = job["payload"].get("dispatch")
    if dispatch is None:
        return (
            True  # Existing locally submitted explicit jobs remain manual, never reclassified as automatic.
        )
    if not isinstance(dispatch, dict):
        raise ContextError("invalid_dispatch_policy", "任务调度权限结构无效。")
    mode = dispatch.get("mode")
    if mode == "model_retry":
        if set(dispatch) != {"mode", "authorization_id"}:
            raise ContextError("invalid_dispatch_policy", "恢复任务没有固定的主人授权。")
        identity = valid_id(dispatch["authorization_id"])
        admission = state.get("model_retries", {}).get(identity)
        recovery = job.get("recovery")
        if (
            not isinstance(admission, dict)
            or not isinstance(recovery, dict)
            or recovery.get("schema_version") != 1
            or job["kind"] != "process"
            or job["principal"] != "local_owner"
            or "extraction" not in job["payload"]
            or admission.get("job_id") != job["id"]
            or admission.get("payload_hash") != digest(job["payload"])
            or admission.get("recovery_hash") != digest(recovery)
            or admission.get("max_calls") != job["budget"]["max_calls"]
        ):
            raise ContextError("invalid_dispatch_policy", "新尝试与已核对的阶段、未知请求或额度不一致。")
        parent = state["jobs"].get(recovery.get("parent_job_id"))
        if not parent or parent["principal"] != "local_owner" or parent["kind"] != "process":
            raise ContextError("invalid_dispatch_policy", "新尝试缺少同一主人的原任务记录。")
        return True
    if mode == "scheduled_sync":
        if set(dispatch) != {"mode", "schedule_id"}:
            raise ContextError("invalid_dispatch_policy", "来源定时任务没有固定授权。")
        identity = dispatch["schedule_id"]
        policy = sync_schedule_policy(state, identity)
        if (
            job["kind"] != "sync"
            or job["principal"] != "local_owner"
            or job["budget"]["max_calls"] != 0
            or job["calls"]
            or set(job["payload"]) != {"sync", "dispatch"}
            or job["payload"].get("sync") != {"config_id": policy["config_id"]}
            or state.get("sync_schedule_admissions", {}).get(job["id"]) != identity
        ):
            raise ContextError("invalid_dispatch_policy", "来源任务与已登记的定时范围不一致。")
        current = state["settings"].get("sync_schedules", {}).get(policy["scope_id"], {})
        return (
            state["settings"].get("auto_sync") is True
            and current.get("enabled") is True
            and current.get("schedule_id") == identity
            and not current.get("blocked_job_id")
        )
    if mode == "history" and set(dispatch) == {"mode", "batch_id"}:
        valid_id(dispatch["batch_id"])
        batch = state.get("history_batches", {}).get(dispatch["batch_id"])
        if not batch or job["id"] not in batch["job_ids"] or batch["principal"] != job["principal"]:
            raise ContextError("invalid_dispatch_policy", "历史任务没有对应已确认批次。")
        return True
    if mode != "automatic" or set(dispatch) != {"mode", "policy_id"}:
        raise ContextError("invalid_dispatch_policy", "未注册的调度模式，不接受资料中的权限指令。")
    identity = dispatch["policy_id"]
    policy = automatic_policy(state, identity)
    context = job["payload"].get("extraction", {})
    profiles = context.get("model_profiles", {})
    if (
        job["principal"] != "local_owner"
        or state.get("automatic_admissions", {}).get(job["id"]) != identity
        or job["budget"]["max_calls"] != policy["max_calls"]
        or not profiles
        or any(policy["model_profiles"].get(role) != ref for role, ref in profiles.items())
    ):
        raise ContextError("invalid_dispatch_policy", "自动任务与固定授权/模型配置不一致。")
    settings = state["settings"]
    return settings.get("auto_process") is True and (
        settings.get("automatic_policy") == identity
        or job["id"] in settings.get("automatic_resumed_jobs", [])
    )


def sync_schedule_policy(state: dict[str, Any], identity: str) -> dict[str, Any]:
    valid_id(identity)
    policy = state.get("sync_schedule_policies", {}).get(identity)
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {"schema_version", "config_id", "scope_id", "interval_minutes", "created_at", "generation"}
        or policy.get("schema_version") != 1
        or identity != "z_" + digest(policy)
        or type(policy.get("interval_minutes")) is not int
        or not 60 <= policy["interval_minutes"] <= 1440
        or type(policy.get("generation")) is not int
        or policy["generation"] < 0
    ):
        raise ContextError("invalid_sync_schedule", "定时同步策略损坏或范围/间隔不符。")
    valid_id(policy["config_id"])
    valid_id(policy["scope_id"])
    validate_time(policy["created_at"])
    plan = state.get("sync_scope_configs", {}).get(policy["config_id"])
    if (
        not isinstance(plan, dict)
        or policy["config_id"] != "y_" + digest(plan)
        or plan.get("scope_id") != policy["scope_id"]
    ):
        raise ContextError("invalid_sync_schedule", "定时同步没有对应不可变来源配置。")
    return policy
