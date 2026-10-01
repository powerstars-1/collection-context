"""Own bounded source jobs, immutable scope plans and per-work recovery checkpoints."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.douyin import source_asset_identity
from collection_context.sources.links import parse_link
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.jobs import JobManager
from collection_context.workflows.policy import execution_allowed


def sync_plan(
    kind: str,
    *,
    account_ref: str | None = None,
    collection_id: str | None = None,
    creator_url: str | None = None,
    limit: int = 5,
    download: bool = False,
) -> dict[str, Any]:
    if kind not in {"liked", "saved", "collection", "creator"}:
        raise ContextError("invalid_source_scope", "同步范围只支持喜欢、收藏、指定收藏夹或博主作品。")
    if type(limit) is not int or not 1 <= limit <= 20 or type(download) is not bool:
        raise ContextError("invalid_source_scope", "每轮同步数量1至20，下载开关须明确。")
    if kind == "creator":
        link = parse_link(creator_url or "")
        if link.kind != "creator" or account_ref is not None or collection_id is not None:
            raise ContextError("invalid_source_scope", "博主作品范围只接受完整主页，不附带本人收藏范围。")
        creator_url = link.url
        scope_id = "s_" + digest(["douyin_creator", link.identity])[:32]
    else:
        if not account_ref or not valid_id(account_ref).startswith("s_") or creator_url is not None:
            raise ContextError("invalid_source_scope", "本人列表须绑定已确认的账号引用，不接受他人主页。")
        if kind == "collection":
            if not isinstance(collection_id, str) or not re.fullmatch(r"[0-9]{1,32}", collection_id):
                raise ContextError("invalid_source_scope", "指定收藏夹需要稳定数字身份。")
        elif collection_id is not None:
            raise ContextError("invalid_source_scope", "喜欢和全收藏不能附带收藏夹参数。")
        scope_id = "s_" + digest(["douyin_self", account_ref, kind, collection_id])[:32]
    return {
        "schema_version": 1,
        "scope_id": scope_id,
        "kind": kind,
        "account_ref": account_ref,
        "collection_id": collection_id,
        "creator_url": creator_url,
        "limit": limit,
        "download": download,
    }


class SynchronizationWorkflow:
    def __init__(
        self,
        store: LibraryStore,
        source_factory: Callable[[], AbstractContextManager[DouyinBrowserSource]] | None = None,
        *,
        runtime_dir: Path | None = None,
    ):
        self.store, self.source_factory = store, source_factory
        self.runtime_dir = runtime_dir
        self.jobs = JobManager(store)

    def configure(
        self,
        kind: str,
        *,
        create_only: bool = False,
        admission: Callable[[dict], None] | None = None,
        **arguments,
    ) -> dict[str, Any]:
        if type(create_only) is not bool:
            raise ContextError("invalid_argument", "范围创建策略须为布尔值。")
        plan = sync_plan(kind, **arguments)
        config_id = "y_" + digest(plan)

        def change(state):
            if admission is not None:
                admission(state)
            current = state.get("sync_current_scopes", {}).get(plan["scope_id"])
            if create_only and current not in {None, config_id}:
                raise ContextError("source_scope_exists", "此范围已有不同配置；未覆盖已有任务或定时规则。")
            state.setdefault("sync_scope_configs", {})[config_id] = plan
            state.setdefault("sync_current_scopes", {})[plan["scope_id"]] = config_id
            state["scopes"].setdefault(
                plan["scope_id"], {"kind": kind, "status": "not_started", "complete": False}
            )

        self.store.transact(change)
        return {"config_id": config_id, **plan, "model_requests": 0}

    def plan(self, config_id: str) -> dict[str, Any]:
        return self.plan_from_state(self.store.snapshot(), config_id)

    @staticmethod
    def plan_from_state(state: dict[str, Any], config_id: str) -> dict[str, Any]:
        value = state.get("sync_scope_configs", {}).get(valid_id(config_id))
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "scope_id",
            "kind",
            "account_ref",
            "collection_id",
            "creator_url",
            "limit",
            "download",
        }:
            raise ContextError("invalid_sync_plan", "同步范围未注册或结构损坏。")
        rebuilt = sync_plan(
            value["kind"],
            **{k: value[k] for k in ("account_ref", "collection_id", "creator_url", "limit", "download")},
        )
        if value != rebuilt or config_id != "y_" + digest(value):
            raise ContextError("invalid_sync_plan", "同步范围版本或身份不符，未猜测执行。")
        return value

    def submit(
        self,
        config_id: str,
        *,
        idempotency_key: str,
        current_only: bool = False,
    ) -> dict[str, Any]:
        self.plan(config_id)
        if type(current_only) is not bool:
            raise ContextError("invalid_argument", "范围提交策略须为布尔值。")

        def admission(state, _):
            if not current_only:
                return
            plan = self.plan_from_state(state, config_id)
            if state.get("sync_current_scopes", {}).get(plan["scope_id"]) != config_id:
                raise ContextError("source_plan_changed", "同步配置已改变，请刷新范围后确认。")
            for job in state["jobs"].values():
                if (
                    job["principal"] != "local_owner"
                    or job["kind"] != "sync"
                    or job["state"] not in {"queued", "running"}
                ):
                    continue
                pending = state.get("sync_scope_configs", {}).get(
                    job["payload"].get("sync", {}).get("config_id"), {}
                )
                if pending.get("scope_id") == plan["scope_id"]:
                    raise ContextError(
                        "source_sync_busy", "此范围已有未完成批次，请先完成或取消；未重复排队。"
                    )

        return self.jobs.submit(
            "sync",
            {"sync": {"config_id": config_id}},
            idempotency_key=idempotency_key,
            max_calls=0,
            admission=admission,
        )

    def _running(self, ref: str) -> bool:
        job = self.jobs.get(ref)
        return job["state"] == "running" and not job["cancel_requested"]

    def _stopped(self, ref: str, plan: dict[str, Any], imported: dict) -> dict[str, Any]:
        def change(state):
            scope = state["scopes"][plan["scope_id"]]
            if scope.get("last_job_id") == ref:
                scope.update(status="cancelled", complete=False, committed_count=len(imported))

        self.store.transact(change)
        return self.jobs.get(ref)

    def _paused(self, ref: str, plan: dict, imported: dict, executor: ExecutorLease) -> dict | None:
        state = self.store.snapshot()
        job = state["jobs"][ref]
        if job["state"] == "running" and not execution_allowed(state, job):
            paused = self.jobs.suspend_source(ref, lease=executor)
            if paused["state"] != "running":

                def change(current):
                    scope = current["scopes"][plan["scope_id"]]
                    if scope.get("last_job_id") == ref:
                        scope.update(status="paused", complete=False, committed_count=len(imported))

                self.store.transact(change)
                return paused
        return None

    def run(self, ref: str) -> dict[str, Any]:
        if self.source_factory is None:
            raise ContextError("source_setup_required", "执行同步需要明确的独立来源连接。")
        original = self.jobs.get(ref)
        if (
            original["kind"] != "sync"
            or set(original["payload"]) not in ({"sync"}, {"sync", "dispatch"})
            or not isinstance(original["payload"]["sync"], dict)
            or set(original["payload"]["sync"]) != {"config_id"}
        ):
            raise ContextError("invalid_sync_plan", "同步任务没有固定注册范围。")
        config_id = original["payload"]["sync"]["config_id"]
        plan = self.plan(config_id)
        if original["budget"]["max_calls"] != 0 or original["calls"]:
            raise ContextError("invalid_sync_plan", "来源同步不能携带模型调用或计费预算。")
        with ExecutorLease(self.store.files.root) as executor:
            executor.check()
            self.jobs.start(ref)
            previous = self.jobs.get(ref)["stages"].get("source_sync", {}).get("result") or {}
            if (
                not isinstance(previous, dict)
                or previous
                and set(previous) != {"imported", "coverage"}
                or not isinstance(previous.get("imported", {}), dict)
                or len(previous.get("imported", {})) > plan["limit"]
                or any(
                    not isinstance(native, str)
                    or not re.fullmatch(r"[0-9]{1,32}", native)
                    or not isinstance(result, dict)
                    or not isinstance(result.get("source_hash"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", result["source_hash"])
                    for native, result in previous.get("imported", {}).items()
                )
            ):
                error = ContextError("invalid_sync_checkpoint", "同步检查点损坏或数量不符，未猜测续跑。")
                return self.jobs.finish(ref, "blocked", error=error)
            imported = dict(previous.get("imported", {}))
            stage_result: dict[str, Any] = {"imported": imported, "coverage": None}
            self.jobs.set_stage(
                ref, "source_sync", state_name="running", input_hash=config_id, result=stage_result
            )

            def attempted(state):
                state["scopes"][plan["scope_id"]].update(
                    status="running", complete=False, last_attempt_at=utc_now(), last_job_id=ref
                )
                state["scopes"][plan["scope_id"]].pop("error", None)

            self.store.transact(attempted)
            try:
                paused = self._paused(ref, plan, imported, executor)
                if paused:
                    return paused
                with self.source_factory() as source:
                    if not self._running(ref):
                        return self._stopped(ref, plan, imported)
                    batch = (
                        source.fetch_creator(plan["creator_url"], limit=plan["limit"])
                        if plan["kind"] == "creator"
                        else source.fetch_self(
                            plan["kind"],
                            expected_account_ref=plan["account_ref"],
                            collection_id=plan["collection_id"],
                            limit=plan["limit"],
                        )
                    )
                    if batch.limit != plan["limit"] or len(batch.items) > plan["limit"]:
                        raise ContextError("source_limit", "来源返回范围超过固定任务数量，未扩大同步。")
                    ingestion = IngestionWorkflow(self.store, source, runtime_dir=self.runtime_dir)
                    for native_id, item in batch.items.items():
                        executor.check()
                        if not self._running(ref):
                            return self._stopped(ref, plan, imported)
                        paused = self._paused(ref, plan, imported, executor)
                        if paused:
                            return paused
                        if native_id in imported:
                            if imported[native_id].get("source_hash") != digest(
                                [item.source, source_asset_identity(item)]
                            ):
                                raise ContextError(
                                    "source_snapshot_changed",
                                    "恢复观察中已提交作品内容改变，未覆盖固定同步检查点。",
                                )
                            continue
                        if len(imported) >= plan["limit"]:
                            break
                        imported[native_id] = ingestion.import_item(
                            item, kind=plan["kind"], scope_id=plan["scope_id"], download=plan["download"]
                        )
                        imported[native_id]["source_hash"] = digest(
                            [item.source, source_asset_identity(item)]
                        )
                        if not self._running(ref):
                            return self._stopped(ref, plan, imported)
                        self.jobs.set_stage(
                            ref,
                            "source_sync",
                            state_name="running",
                            input_hash=config_id,
                            result=stage_result,
                        )
                        paused = self._paused(ref, plan, imported, executor)
                        if paused:
                            return paused
                if not self._running(ref):
                    return self._stopped(ref, plan, imported)
                paused = self._paused(ref, plan, imported, executor)
                if paused:
                    return paused
                coverage = batch.coverage()
                coverage["committed_count"] = len(imported)
                coverage["reobserved_after_restart"] = bool(previous)
                stage_result["coverage"] = coverage
                partial = not batch.done or any(
                    result["download"] == "blocked" for result in imported.values()
                )
                self.jobs.set_stage(
                    ref,
                    "source_sync",
                    state_name="partial" if partial else "ready",
                    input_hash=config_id,
                    result=stage_result,
                )

                def finished(state):
                    state["scopes"][plan["scope_id"]].update(
                        **coverage, status="partial" if partial else "ready", last_sync_at=utc_now()
                    )

                self.store.transact(finished)
                return self.jobs.finish(ref, "partial" if partial else "succeeded")
            except ContextError as error:
                if not self._running(ref):
                    return self._stopped(ref, plan, imported)
                # No platform/body/cookies in diagnostics; retain successfully imported work references.
                failure = error.as_dict()

                def failed(state):
                    state["scopes"][plan["scope_id"]].update(
                        status="blocked", complete=False, error=failure, observed_count=len(imported)
                    )

                self.store.transact(failed)
                return self.jobs.finish(ref, "blocked", error=error)
