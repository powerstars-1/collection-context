"""Durable single-work admission and execution; no legacy project or model authority."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError, digest
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.douyin import source_asset_identity
from collection_context.sources.links import parse_link
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.jobs import JobManager


def link_plan(url: str, download: bool) -> dict[str, Any]:
    if type(download) is not bool:
        raise ContextError("invalid_argument", "下载选择须为明确的布尔值。")
    link = parse_link(url)
    if link.kind == "creator":
        raise ContextError("source_scope_mismatch", "这里只添加一条作品；博主主页请用作品同步范围。")
    return {"schema_version": 1, "url": link.url, "download": download}


class AdditionWorkflow:
    def __init__(
        self,
        store: LibraryStore,
        source_factory: Callable[[], AbstractContextManager[DouyinBrowserSource]] | None = None,
        *,
        agent_authority: Callable[[str], None] | None = None,
        runtime_dir: Path | None = None,
    ):
        self.store, self.source_factory = store, source_factory
        self.agent_authority = agent_authority
        self.runtime_dir = runtime_dir
        self.jobs = JobManager(store)

    def authorize(self, principal: str) -> None:
        if principal != "local_owner":
            if self.agent_authority is None:
                raise ContextError("permission_denied", "未授权此 AI 的单链接添加。")
            self.agent_authority(principal)

    @staticmethod
    def admitted(state: dict[str, Any], job: dict[str, Any]) -> bool:
        """A payload alone is not a credential-authorized admission."""
        if job["principal"] == "local_owner":
            return False
        try:
            plan = AdditionWorkflow.plan(job)
        except (ContextError, KeyError, TypeError):
            return False
        return not plan["download"] and state.get("agent_link_admissions", {}).get(job["id"]) == {
            "schema_version": 1,
            "principal": job["principal"],
            "plan_hash": digest(plan),
        }

    @contextmanager
    def ingestion_store(self, principal: str):
        if principal == "local_owner":
            yield self.store
        else:
            # ACL revocation and every source write serialize on the same kernel writer lease.
            guarded = LibraryStore(self.store.files.root, mutation_guard=lambda: self.authorize(principal))
            try:
                yield guarded
            finally:
                guarded.close()

    def submit(
        self,
        *,
        url: str,
        download: bool,
        idempotency_key: str,
        source_confirmed: bool,
        principal: str = "local_owner",
    ) -> dict[str, Any]:
        if source_confirmed is not True:
            raise ContextError("source_authorization_required", "请明确确认仅访问这条作品及所选下载范围。")
        plan = link_plan(url, download)
        self.authorize(principal)
        if principal != "local_owner" and download:
            raise ContextError("permission_denied", "AI 添加仅保存作品原文，不授权媒体下载或计费提取。")

        def admission(state, submitted):
            # Different transport retries cannot start two pending copies of the same work.
            for job in state["jobs"].values():
                if (
                    job["principal"] == principal
                    and job["kind"] == "add"
                    and job["state"] in {"queued", "running"}
                    and job["payload"].get("link", {}).get("url") == plan["url"]
                ):
                    raise ContextError("link_job_pending", "此作品已有待执行任务，请查看或取消原任务。")
            if principal != "local_owner":
                if (
                    sum(
                        j["principal"] == principal and j["state"] in {"queued", "running"}
                        for j in state["jobs"].values()
                    )
                    >= 5
                ):
                    raise ContextError("agent_queue_limited", "此 AI 已有五个未完成任务，请先查看进度。")
                state.setdefault("agent_link_admissions", {})[submitted["id"]] = {
                    "schema_version": 1,
                    "principal": principal,
                    "plan_hash": digest(plan),
                }

        submission = self.jobs._submission(
            kind="add",
            payload={"link": plan},
            idempotency_key=idempotency_key,
            max_calls=0,
            admission=admission,
            principal=principal,
        )

        def authorized_submission(state):
            self.authorize(principal)  # Also covers idempotent retries, inside the admission transaction.
            return submission(state)

        return self.store.transact(authorized_submission)

    @staticmethod
    def plan(job: dict[str, Any]) -> dict[str, Any]:
        raw = job["payload"].get("link")
        if (
            job["kind"] != "add"
            or set(job["payload"]) != {"link"}
            or not isinstance(raw, dict)
            or set(raw) != {"schema_version", "url", "download"}
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or link_plan(raw["url"], raw["download"]) != raw
            or job["budget"]["max_calls"] != 0
            or job["calls"]
        ):
            raise ContextError("invalid_link_plan", "单链接任务范围或权限无效，未访问来源。")
        return raw

    def _running(self, ref: str, principal: str = "local_owner") -> bool:
        self.authorize(principal)
        current = self.jobs.get(ref, principal=principal)
        return current["state"] == "running" and not current["cancel_requested"]

    def _checkpoint(self, job: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any] | None:
        stage = job["stages"].get("link_import")
        if stage is None:
            return None
        value = stage.get("result")
        if (
            stage.get("input_hash") != digest(plan)
            or not isinstance(value, dict)
            or set(value) != {"source_hash", "content_hash", "asset_hash", "result"}
            or not isinstance(value["source_hash"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_hash"])
            or any(
                not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                for k in ("content_hash", "asset_hash")
            )
            or not isinstance(value["result"], dict)
        ):
            raise ContextError("invalid_link_checkpoint", "单链接检查点无效，未猜测续跑。")
        result = value["result"]
        required = {
            "material_ref",
            "native_id",
            "source_url",
            "created",
            "content_changed",
            "source_assets_changed",
            "metadata",
            "download",
            "extraction",
            "model_requests",
        }
        if (
            required - result.keys()
            or result.keys() - (required | {"input_id", "download_reused", "error"})
            or result["metadata"] != "ready"
            or result["download"] not in {"not_requested", "ready", "blocked"}
            or result["extraction"] != "not_requested"
            or type(result["model_requests"]) is not int
            or result["model_requests"] != 0
            or any(
                type(result[k]) is not bool for k in ("created", "content_changed", "source_assets_changed")
            )
            or not plan["download"]
            and result["download"] != "not_requested"
            or result["download"] == "ready"
            and (
                not isinstance(result.get("input_id"), str) or type(result.get("download_reused")) is not bool
            )
        ):
            raise ContextError("invalid_link_checkpoint", "单链接结果结构无效，未猜测续跑。")
        item = self.store.get(result["material_ref"])
        if (
            item["excluded"]
            or item["native_id"] != result["native_id"]
            or item["source_url"] != result["source_url"]
            or item["content_hash"] != value["content_hash"]
            or item.get("source_asset_hash") != value["asset_hash"]
            or "s_link" not in {relation["scope_id"] for relation in item["relations"].values()}
        ):
            raise ContextError("source_snapshot_changed", "已入库作品已变化或排除，未恢复原单链接任务。")
        # Do not preserve transient signed download URLs in a checkpoint.
        return value

    def run(self, ref: str, *, principal: str = "local_owner") -> dict[str, Any]:
        job = self.jobs.get(ref, principal=principal)
        if principal != "local_owner" and not self.admitted(self.store.snapshot(), job):
            raise ContextError("permission_denied", "任务没有对应 AI 添加授权记录。")
        self.authorize(principal)
        plan = self.plan(job)
        if self.source_factory is None:
            raise ContextError("source_setup_required", "执行单链接需显式配置本产品独立浏览器。")
        with self.store.storage.executor(
            self.store.files.root, expected_identity=self.store.files.identity
        ) as executor:
            executor.check()
            self.jobs.start(ref, principal=principal)
            try:
                previous = self._checkpoint(self.jobs.get(ref, principal=principal), plan)
                if previous and (not plan["download"] or previous["result"]["download"] == "blocked"):
                    self.jobs.set_stage(
                        ref,
                        "link_import",
                        state_name="partial" if plan["download"] else "ready",
                        input_hash=digest(plan),
                        result=previous,
                        principal=principal,
                    )
                    return self.jobs.finish(
                        ref,
                        "partial" if plan["download"] else "succeeded",
                        principal=principal,
                        authorization=lambda: self.authorize(principal),
                    )
                if previous and previous["result"]["download"] == "ready":
                    item = self.store.get(previous["result"]["material_ref"])
                    reusable = PreparedInputs(self.store).reusable_source(
                        item["id"], item.get("source_asset_hash", "")
                    )
                    if reusable != previous["result"].get("input_id"):
                        raise ContextError("source_snapshot_changed", "已准备媒体改变，未隐式重新下载。")
                    self.jobs.set_stage(
                        ref,
                        "link_import",
                        state_name="ready",
                        input_hash=digest(plan),
                        result=previous,
                        principal=principal,
                    )
                    return self.jobs.finish(
                        ref, "succeeded", principal=principal, authorization=lambda: self.authorize(principal)
                    )
                with self.source_factory() as source, self.ingestion_store(principal) as ingestion_store:
                    if not self._running(ref, principal):
                        return self.jobs.get(ref, principal=principal)
                    observed = source.fetch_item(
                        plan["url"], cancelled=lambda: not self._running(ref, principal)
                    )
                    executor.check()
                    if not self._running(ref, principal):
                        return self.jobs.get(ref, principal=principal)
                    identity = digest([observed.source, source_asset_identity(observed)])
                    if previous and previous["source_hash"] != identity:
                        raise ContextError(
                            "source_snapshot_changed", "恢复观察中作品变化，未覆盖固定检查点。"
                        )
                    ingestion = IngestionWorkflow(ingestion_store, source, runtime_dir=self.runtime_dir)
                    result = (
                        previous["result"]
                        if previous
                        else ingestion.import_item(
                            observed, kind="link", scope_id="s_link", observer_principal=principal
                        )
                    )
                    item = self.store.get(result["material_ref"])
                    checkpoint = {
                        "source_hash": identity,
                        "content_hash": item["content_hash"],
                        "asset_hash": item["source_asset_hash"],
                        "result": result,
                    }
                    if not self._running(ref, principal):
                        return self.jobs.get(ref, principal=principal)
                    self.jobs.set_stage(
                        ref,
                        "link_import",
                        state_name="running",
                        input_hash=digest(plan),
                        result=checkpoint,
                        principal=principal,
                    )
                    if plan["download"]:
                        executor.check()
                        if not self._running(ref, principal):
                            return self.jobs.get(ref, principal=principal)
                        checkpoint["result"] = ingestion.prepare_download(observed, result)
                    if not self._running(ref, principal):
                        return self.jobs.get(ref, principal=principal)
                    partial = checkpoint["result"]["download"] == "blocked"
                    self.jobs.set_stage(
                        ref,
                        "link_import",
                        state_name="partial" if partial else "ready",
                        input_hash=digest(plan),
                        result=checkpoint,
                        principal=principal,
                    )
                return self.jobs.finish(
                    ref,
                    "partial" if partial else "succeeded",
                    principal=principal,
                    authorization=lambda: self.authorize(principal),
                )
            except ContextError as error:
                current = self.jobs.get(ref, principal=principal)
                if current["state"] != "running" or current["cancel_requested"]:
                    return current
                return self.jobs.finish(ref, "blocked", error=error, principal=principal)
