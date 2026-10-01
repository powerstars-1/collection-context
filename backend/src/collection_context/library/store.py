"""Small-library atomic commit store; no database or inferred Markdown task state."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from collection_context.application.contracts import (
    ARTIFACT_KINDS,
    SCHEMA_VERSION,
    ContextError,
    canonical_bytes,
    digest,
    relation_id,
    utc_now,
    valid_id,
    validate_source,
    validate_time,
)
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ownership import ExecutorLease, WriterLease

WRITER_PROTOCOL = "os_writer_v2"
LEGACY_GUARD = ".context/写锁.json"


class LibraryStore:
    """Transactions are single-writer; readers observe an entire immutable generation."""

    def __init__(self, root: Path, *, mutation_guard: Callable[[], None] | None = None):
        self._mutation_guard = mutation_guard
        self.files = SafeFiles(root)
        try:
            self.workspace_id = valid_id(self._configuration()["workspace_id"])
        except BaseException:
            self.files.close()
            raise

    def _configuration(self) -> dict[str, Any]:
        try:
            config = json.loads(self.files.read("context-workspace.json", max_bytes=65_536))
            if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            identity = valid_id(config["workspace_id"])
            if hasattr(self, "workspace_id") and identity != self.workspace_id:
                raise ValueError
            return config
        except (ValueError, TypeError, KeyError):
            raise ContextError("invalid_workspace", "库标识无效或已变化，不能直接写入。") from None

    @staticmethod
    def _guard_body(workspace_id: str) -> bytes:
        return canonical_bytes(
            {"protocol": WRITER_PROTOCOL, "workspace_id": workspace_id, "purpose": "legacy_write_guard"}
        )

    def _check_guard(self) -> None:
        try:
            if self.files.read(LEGACY_GUARD, max_bytes=65_536) != self._guard_body(self.workspace_id):
                raise ContextError("lock_changed", "旧版本写入保护已变化；停止写入，不自动重建保护。")
        except ContextError as error:
            if error.code == "not_found":
                raise ContextError("lock_changed", "旧版本写入保护缺失；停止写入。") from None
            raise

    def close(self) -> None:
        self.files.close()

    @classmethod
    def initialize(cls, root: Path) -> LibraryStore:
        root = root.absolute()
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise ContextError("workspace_not_empty", "只初始化空目录，不覆盖已有库。")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with SafeFiles(root) as files:
            workspace_id = "w_" + uuid.uuid4().hex
            # Permanent guard keeps old O_EXCL-only binaries from writing concurrently.
            # It is not an indication that the new OS-backed writer is busy.
            files.write(LEGACY_GUARD, cls._guard_body(workspace_id))
            files.write(
                "context-workspace.json",
                canonical_bytes(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "workspace_id": workspace_id,
                        "vault_dir": "content-vault",
                        "created_at": utc_now(),
                        "writer_protocol": WRITER_PROTOCOL,
                    }
                ),
            )
            state = {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "generation": 0,
                "items": {},
                "jobs": {},
                "idempotency": {},
                "scopes": {},
                "settings": {"auto_sync": False, "auto_process": False},
            }
            cls._publish(files, state)
        return cls(root)

    @staticmethod
    def _publish(
        files: SafeFiles, state: dict[str, Any], *, before_commit: Callable[[], None] | None = None
    ) -> None:
        version = "c_" + uuid.uuid4().hex
        body = canonical_bytes(state)
        if len(body) > 16_000_000:
            raise ContextError("workspace_limit", "资料清单超过当前已验证规模；未提交不可读取的版本。")
        files.write(f".context/提交/{version}.json", body)
        if before_commit is not None:
            before_commit()
        # This pointer is the ONLY visibility/commit boundary. Orphans are not committed.
        files.write(
            ".context/提交/CURRENT.json",
            canonical_bytes({"version": version, "sha256": hashlib.sha256(body).hexdigest()}),
            replace=True,
        )

    def snapshot(self) -> dict[str, Any]:
        try:
            pointer = json.loads(self.files.read(".context/提交/CURRENT.json", max_bytes=65_536))
            version = valid_id(pointer["version"])
            body = self.files.read(f".context/提交/{version}.json")
            if hashlib.sha256(body).hexdigest() != pointer["sha256"]:
                raise ValueError
            state = json.loads(body)
            if state["schema_version"] != SCHEMA_VERSION or state["workspace_id"] != self.workspace_id:
                raise ValueError
            return state
        except (ValueError, TypeError, KeyError):
            raise ContextError("corrupt_workspace", "库提交校验失败；请恢复备份，不自动重建内容。") from None

    @contextmanager
    def writer(self) -> Iterator[WriterLease]:
        with WriterLease(self.files.root) as owner:
            self._check_writer(owner)
            try:
                yield owner
            finally:
                # No unlink: removing a locked inode permits a second independent owner.
                self._check_writer(owner)

    def _check_writer(self, owner: WriterLease) -> None:
        owner.check()
        if self._mutation_guard is not None:
            self._mutation_guard()
        if self._configuration().get("writer_protocol") != WRITER_PROTOCOL:
            raise ContextError(
                "writer_upgrade_required",
                "此早期开发库需显式执行 upgrade-writer；不会自动接管旧写锁。",
            )
        self._check_guard()

    def upgrade_writer(self) -> dict[str, Any]:
        """Explicit, restartable protocol upgrade; never guesses whether a legacy lock is stale."""
        with ExecutorLease(self.files.root), WriterLease(self.files.root) as owner:
            config = self._configuration()
            protocol = config.get("writer_protocol")
            if protocol == WRITER_PROTOCOL:
                self._check_writer(owner)
                return {"upgraded": False, "writer_protocol": WRITER_PROTOCOL}
            if protocol not in {None, "os_writer_v1"}:
                raise ContextError("invalid_workspace", "未知写入协议；未覆盖或降级。")
            self.snapshot()  # Validate the committed library before altering its configuration.
            resumed = False
            # A v1 binary ignores automatic dispatch permissions. Change the guarded writer
            # protocol so its start()/begin_call() transactions fail BEFORE network dispatch.
            # Both OS leases are held, so an old executing task cannot be migrated underneath.
            if protocol == "os_writer_v1":
                previous = self.files.read(LEGACY_GUARD, max_bytes=65_536)
                old_guard = canonical_bytes(
                    {
                        "protocol": "os_writer_v1",
                        "workspace_id": self.workspace_id,
                        "purpose": "legacy_write_guard",
                    }
                )
                if previous == old_guard:
                    self.files.write(LEGACY_GUARD, self._guard_body(self.workspace_id), replace=True)
                elif previous == self._guard_body(self.workspace_id):
                    resumed = True  # A known guard-first interrupted upgrade can be continued.
                else:
                    raise ContextError("lock_changed", "旧协议保护标识不匹配；未清锁或覆盖。")
            try:
                self.files.write(LEGACY_GUARD, self._guard_body(self.workspace_id))
            except ContextError as error:
                if error.code != "write_conflict":
                    raise
                if self.files.read(LEGACY_GUARD, max_bytes=65_536) != self._guard_body(self.workspace_id):
                    raise ContextError(
                        "writer_busy",
                        "旧写锁仍在；请停用旧版本并核实恢复，不按时间或进程号自动清锁。",
                        retryable=True,
                    ) from None
                if protocol is None:
                    resumed = True  # A known upgrade guard can survive a configuration-write failure.
            owner.check()
            self._check_guard()
            config["writer_protocol"] = WRITER_PROTOCOL
            self.files.write("context-workspace.json", canonical_bytes(config), replace=True)
            self._check_writer(owner)
            return {"upgraded": True, "resumed": resumed, "writer_protocol": WRITER_PROTOCOL}

    def transact(self, mutation: Callable[[dict[str, Any]], Any]) -> Any:
        with self.writer() as owner:
            state = self.snapshot()
            result = mutation(state)
            self._check_writer(owner)
            state["generation"] += 1
            self._publish(self.files, state, before_commit=lambda: self._check_writer(owner))
            return copy.deepcopy(result)

    def upsert(
        self,
        source: dict[str, Any],
        *,
        kind: str,
        scope_id: str,
        action_at: str | None = None,
        action_basis: str | None = None,
        observer_principal: str = "local_owner",
    ) -> dict[str, Any]:
        normalized = validate_source(source)
        valid_id(observer_principal)
        rid = relation_id(normalized["id"], kind, scope_id)
        if action_at is not None:
            action_at = validate_time(action_at)
            if not action_basis or not isinstance(action_basis, str) or len(action_basis) > 500:
                raise ContextError("invalid_time", "真实操作时间需同时提供可靠来源依据。")
        now = utc_now()

        def save(state):
            existing = state["items"].get(normalized["id"])
            content_hash = digest(normalized)
            item = existing or {
                "id": normalized["id"],
                "first_observed_at": now,
                "first_observed_generation": state["generation"] + 1,
                "first_observed_principal": observer_principal,
                "relations": {},
                "artifacts": {},
                "excluded": False,
            }
            changed = existing is not None and item["content_hash"] != content_hash
            if changed:
                for artifact in item["artifacts"].values():
                    if artifact["kind"] != "user_note":
                        artifact["state"] = "stale"
            item.update(normalized)
            item.update(content_hash=content_hash, last_observed_at=now)
            relation = item["relations"].get(
                rid,
                {
                    "id": rid,
                    "kind": kind,
                    "scope_id": scope_id,
                    "first_observed_at": now,
                    "action_at": None,
                    "action_basis": None,
                },
            )
            relation["last_observed_at"] = now
            if action_at is not None:
                relation.update(action_at=action_at, action_basis=action_basis)
            item["relations"][rid] = relation
            state["items"][item["id"]] = item
            return {"item": item, "created": existing is None, "content_changed": changed}

        return self.transact(save)

    def get(self, ref: str, *, include_excluded: bool = False) -> dict[str, Any]:
        item = self.snapshot()["items"].get(valid_id(ref))
        if item is None or (item["excluded"] and not include_excluded):
            raise ContextError("not_found", "资料不存在或已被排除。")
        return item

    def exclude(self, ref: str, excluded: bool = True) -> dict[str, Any]:
        valid_id(ref)
        if not isinstance(excluded, bool):
            raise ContextError("invalid_argument", "排除设置须为布尔值。")

        def change(state):
            item = state["items"].get(ref)
            if item is None:
                raise ContextError("not_found", "资料不存在。")
            item["excluded"] = excluded
            return item

        return self.transact(change)

    def save_artifact(
        self,
        ref: str,
        kind: str,
        text: str,
        *,
        processor_version: str,
        expected_content_hash: str,
        coverage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.save_bundle(
            ref,
            {kind: {"text": text, "processor_version": processor_version, "coverage": coverage}},
            expected_content_hash=expected_content_hash,
        )[kind]

    def save_bundle(
        self,
        ref: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        expected_content_hash: str,
        expected_prepared_input: str | None = None,
    ) -> dict[str, Any]:
        """Publish all supplied text artifacts under one visibility boundary, retaining old files."""
        valid_id(ref)
        if not isinstance(artifacts, dict) or not artifacts or len(artifacts) > len(ARTIFACT_KINDS):
            raise ContextError("invalid_artifact", "产物批次为空或无效。")
        for kind, value in artifacts.items():
            if not isinstance(value, dict):
                raise ContextError("invalid_artifact", "产物描述无效。")
            text = value.get("text")
            version = value.get("processor_version")
            if (
                kind not in ARTIFACT_KINDS
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 500_000
                or not isinstance(version, str)
                or not 1 <= len(version) <= 500
                or (value.get("coverage") is not None and not isinstance(value["coverage"], dict))
            ):
                raise ContextError("invalid_artifact", "产物类型、正文或处理版本无效。")

        def save(state):
            item = state["items"].get(ref)
            if not item or item["excluded"]:
                raise ContextError("not_found", "资料不存在或已排除。")
            if item["content_hash"] != expected_content_hash:
                raise ContextError("version_changed", "原文已变更，未提交旧输入的产物。", retryable=True)
            if expected_prepared_input is not None and item.get("prepared_input") != expected_prepared_input:
                raise ContextError("input_superseded", "媒体输入已变化，未提交旧快照的提取结果。")
            filenames = {
                "original": "原文",
                "audio": "音频转写",
                "screen": "画面文字",
                "summary": "内容总结",
                "readable": "可读内容",
                "image": "图片提取",
                "user_note": "用户备注",
            }
            result = {}
            for kind, value in artifacts.items():
                version = "a_" + uuid.uuid4().hex
                path = f"content-vault/80_附件/抖音/{ref}/{version}/{filenames[kind]}.md"
                body = value["text"].encode("utf-8")
                self.files.write(path, body)
                artifact = {
                    "kind": kind,
                    "version": version,
                    "path": path,
                    "state": "ready",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "input_hash": expected_content_hash,
                    "processor_version": value["processor_version"],
                    "created_at": utc_now(),
                    "coverage": value.get("coverage") or {"accuracy": "not_verified"},
                }
                if kind not in {"original", "user_note"} and item.get("prepared_input"):
                    artifact["prepared_input"] = item["prepared_input"]
                item["artifacts"][kind] = artifact
                result[kind] = artifact
            return result

        return self.transact(save)
