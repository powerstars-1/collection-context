"""Owner-only, bounded library management; no arbitrary paths or permanent deletion."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from collections.abc import Callable
from typing import Any

from collection_context.application.contracts import (
    ARTIFACT_KINDS,
    ContextError,
    canonical_bytes,
    digest,
    valid_id,
)
from collection_context.application.service import bounded_integer, public_item
from collection_context.library.index import artifact_bytes, library_version, original_text
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs

MAX_EXPORT_BYTES = 64_000_000
MAX_EXPORT_FILES = 1000
MAX_INVENTORY_FILES = 100_000


class LibraryManagement:
    """Authorization is supplied by a trusted owner entry point, never request arguments.

    HTTP may send only typed refs, scope choices and preview tokens. Export bytes are
    delivered by the entry point as a download; no client filesystem path is accepted.
    """

    def __init__(self, store: LibraryStore, *, authorize: Callable[[], None]):
        self.store = store
        self.authorize = authorize

    @staticmethod
    def _item(state: dict[str, Any], ref: str) -> dict[str, Any]:
        item = state["items"].get(valid_id(ref))
        if item is None:
            raise ContextError("not_found", "资料不存在。")
        return item

    def _media_manifest(self, state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
        identity = item.get("prepared_input")
        if not identity:
            return None
        valid_id(identity)
        record = state.get("prepared_inputs", {}).get(identity)
        expected = f".context/输入/{identity}.json"
        if not record or record.get("path") != expected or record.get("material_ref") != item["id"]:
            raise ContextError("forbidden_path", "输入清单不在此资料受控范围。")
        body = self.store.files.read(expected, max_bytes=2_000_000)
        if hashlib.sha256(body).hexdigest() != record.get("sha256"):
            raise ContextError("media_changed", "媒体输入清单已变化。")
        try:
            payload = json.loads(body)
            PreparedInputs._validate(payload)
            if payload["material_ref"] != item["id"] or identity != "u_" + digest(payload):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ContextError("invalid_input", "媒体清单身份或结构无效。") from None
        blobs = [
            *payload["originals"],
            *(segment["blob"] for segment in payload["audio"]),
            *(frame["blob"] for frame in payload["frames"]),
        ]
        for blob in blobs:
            if blob["path"] != PreparedInputs._path(item["id"], blob["sha256"], blob["mime_type"]):
                raise ContextError("forbidden_path", "媒体指针不在此资料受控范围。")
        return payload

    def _media(self, state: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._media_manifest(state, item)
        if payload is None:
            return []
        blobs = [
            *payload["originals"],
            *(segment["blob"] for segment in payload["audio"]),
            *(frame["blob"] for frame in payload["frames"]),
        ]
        return list({blob["path"]: blob for blob in blobs}.values())

    def _size(self, path: str) -> int:
        # Same backend contract on each platform; no POSIX descriptor access or
        # whole-video reads in this application consumer.
        return self.store.files.file_size(path)

    @staticmethod
    def _artifact_path(item: dict[str, Any], kind: str) -> str:
        if kind not in ARTIFACT_KINDS:
            raise ContextError("invalid_artifact", "未知产物类型。")
        names = dict(
            zip(
                ("original", "audio", "screen", "summary", "readable", "image", "user_note"),
                ("原文", "音频转写", "画面文字", "内容总结", "可读内容", "图片提取", "用户备注"),
                strict=True,
            )
        )
        value = item["artifacts"][kind]
        expected = f"content-vault/80_附件/抖音/{item['id']}/{valid_id(value['version'])}/{names[kind]}.md"
        if value["path"] != expected:
            raise ContextError("forbidden_path", "产物指针不在此资料受控范围。")
        return expected

    def overview(self) -> dict[str, Any]:
        self.authorize()
        state = self.store.snapshot()
        if len(state["items"]) > MAX_INVENTORY_FILES:
            raise ContextError("inventory_limit", "资料计量超过当前安全上限。")
        totals = {"text_bytes": 0, "media_bytes": 0, "excluded_bytes": 0, "file_count": 0}
        paths: set[str] = set()
        gaps: list[dict[str, str]] = []
        for item in state["items"].values():
            try:
                records = [(self._artifact_path(item, kind), "text_bytes") for kind in item["artifacts"]]
                records.extend((blob["path"], "media_bytes") for blob in self._media(state, item))
                for path, category in records:
                    if path in paths:
                        continue
                    if len(paths) >= MAX_INVENTORY_FILES:
                        raise ContextError("inventory_limit", "资料计量超过当前安全上限。")
                    paths.add(path)
                    size = self._size(path)
                    totals[category] += size
                    totals["file_count"] += 1
                    if item["excluded"]:
                        totals["excluded_bytes"] += size
            except ContextError as error:
                if error.code == "inventory_limit":
                    raise
                if len(gaps) < 100:
                    gaps.append({"material_ref": item["id"], "code": error.code})
        self.store.files.check_root()
        try:
            disk = shutil.disk_usage(self.store.files.root)
        except OSError:
            raise ContextError("storage_unavailable", "资料盘已离线或无法查询容量。") from None
        self.store.files.check_root()
        self.authorize()
        return {
            "visible_items": sum(not item["excluded"] for item in state["items"].values()),
            "excluded_items": sum(item["excluded"] for item in state["items"].values()),
            "version": library_version(state),
            "storage": {**totals, "disk_free_bytes": disk.free, "disk_total_bytes": disk.total},
            "storage_scope": "registered_current_artifacts_and_media_only",
            "storage_excludes": [
                "original_metadata",
                "old_generations",
                "unregistered_files",
                "cache",
                "credentials",
            ],
            "integrity_gaps": gaps,
            "accuracy": "safe_file_sizes_not_content_hash_verification",
            "automatic_video_cleanup": False,
            "permanent_delete_supported": False,
        }

    def excluded_items(
        self, *, offset: int = 0, limit: int = 20, version: str | None = None
    ) -> dict[str, Any]:
        self.authorize()
        bounded_integer(offset, "offset", 0, 100_000)
        bounded_integer(limit, "limit", 1, 20)
        state = self.store.snapshot()
        current = library_version(state)
        if offset and version is None:
            raise ContextError("version_required", "继续列表需要版本。")
        if version is not None and version != current:
            raise ContextError("version_changed", "资料列表已变化，请重新获取。")
        items = sorted(
            (item for item in state["items"].values() if item["excluded"]), key=lambda item: item["id"]
        )
        if offset > len(items):
            raise ContextError("invalid_argument", "列表位置超出范围。")
        rows = [{**public_item(item), "excluded": True} for item in items[offset : offset + limit]]
        self.authorize()
        return {
            "items": rows,
            "total_items": len(items),
            "version": current,
            "next_offset": offset + len(rows) if offset + len(rows) < len(items) else None,
        }

    @staticmethod
    def _pending_jobs(state: dict[str, Any]) -> int:
        # Conservative: sync can change any item, and processing payloads can pin old inputs.
        return sum(job["state"] in {"queued", "running"} for job in state["jobs"].values())

    def _exclusion_preview(self, state: dict[str, Any], ref: str, excluded: bool) -> dict[str, Any]:
        item = self._item(state, ref)
        if type(excluded) is not bool:
            raise ContextError("invalid_argument", "排除设置必须为布尔值。")
        return {
            "material_ref": ref,
            "title": item["title"],
            "currently_excluded": item["excluded"],
            "excluded": excluded,
            "pending_jobs": self._pending_jobs(state),
            "preview_token": digest([self.store.workspace_id, state["generation"], item, excluded]),
            "effect": "hidden_from_search_read_and_preference_evidence"
            if excluded
            else "restored_and_reindexed",
            "files_deleted": False,
            "model_requests": 0,
        }

    def preview_exclusion(self, ref: str, *, excluded: bool) -> dict[str, Any]:
        self.authorize()
        return self._exclusion_preview(self.store.snapshot(), ref, excluded)

    def _edit_preview(self, state: dict[str, Any], ref: str, kind: str) -> tuple[dict[str, Any], bytes]:
        item = self._item(state, ref)
        if item["excluded"]:
            raise ContextError("not_found", "请先恢复已排除资料。")
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise ContextError("invalid_artifact", "未知产物类型。")
        previous = item["artifacts"].get(kind)
        if previous is None:
            raise ContextError("artifact_missing", "只能核对已经登记的正文文件。")
        path = self._artifact_path(item, kind)
        body = self.store.files.read(path, max_bytes=2_000_000)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ContextError("invalid_artifact", "修改文件不是有效 UTF-8 文本。") from None
        if not text.strip() or len(text) > 500_000:
            raise ContextError("invalid_artifact", "修改正文为空或超过长度上限。")
        sha = hashlib.sha256(body).hexdigest()
        dependencies = (
            {"summary", "readable"}
            if kind in {"original", "audio", "screen", "image"}
            else {"readable"}
            if kind == "summary"
            else set()
        )
        return {
            "material_ref": ref,
            "artifact": kind,
            "title": item["title"],
            "changed": sha != previous["sha256"],
            "previous_version": previous["version"],
            "previous_sha256": previous["sha256"],
            "edited_sha256": sha,
            "text_preview": text[:2000],
            "preview_truncated": len(text) > 2000,
            "bytes": len(body),
            "previous_text_available": False,
            "invalidated_artifacts": sorted(dependencies & item["artifacts"].keys()),
            "pending_jobs": self._pending_jobs(state),
            "preview_token": digest([self.store.workspace_id, state["generation"], item, kind, sha]),
            "source_metadata_changed": False,
            "model_requests": 0,
            "accuracy": "owner_edit_not_verified",
            "automatic_overwrite_allowed": False,
        }, body

    def preview_edit(self, ref: str, *, artifact: str) -> dict[str, Any]:
        self.authorize()
        preview, _ = self._edit_preview(self.store.snapshot(), ref, artifact)
        self.authorize()
        return preview

    def accept_edit(self, ref: str, *, artifact: str, preview_token: str, confirmed: bool) -> dict[str, Any]:
        self.authorize()
        if confirmed is not True:
            raise ContextError("confirmation_required", "请先核对修改正文和过期影响，再确认接纳。")
        accepted: dict[str, Any] = {}

        def check_commit():
            self.authorize()
            if self.store.files.read(accepted["path"], max_bytes=2_000_000) != accepted["body"]:
                raise ContextError("version_changed", "预览后的修改文件又发生变化，请重新核对。")

        def change(state):
            self.authorize()
            preview, body = self._edit_preview(state, ref, artifact)
            if preview_token != preview["preview_token"]:
                raise ContextError("version_changed", "正文或资料状态已变化，请重新预览。")
            if preview["pending_jobs"]:
                raise ContextError("library_busy", "请先完成或取消待处理任务，再接纳人工修改。")
            if not preview["changed"]:
                raise ContextError("edit_unchanged", "正文与登记版本一致，不创建重复版本。")
            item = state["items"][ref]
            previous = item["artifacts"][artifact]
            accepted.update(path=previous["path"], body=body)
            updated = self.store._write_artifacts(
                item,
                {
                    artifact: {
                        "text": body.decode("utf-8"),
                        "processor_version": "owner-edit-v1",
                        "coverage": {
                            **previous["coverage"],
                            "accuracy": "owner_edit_not_verified",
                            "owner_modified": True,
                        },
                    }
                },
                expected_content_hash=previous["input_hash"],
            )[artifact]
            # A correction is not evidence that an obsolete source/media version is current.
            updated["state"] = previous["state"]
            updated.pop("prepared_input", None)
            if "prepared_input" in previous:
                updated["prepared_input"] = previous["prepared_input"]
            if "source_artifacts" in previous:
                updated["source_artifacts"] = previous["source_artifacts"]
            updated["owner_edit"] = {
                "previous_version": previous["version"],
                "previous_sha256": previous["sha256"],
                "accepted_sha256": preview["edited_sha256"],
            }
            for kind in preview["invalidated_artifacts"]:
                item["artifacts"][kind]["state"] = "stale"
            self.authorize()
            return {
                "material_ref": ref,
                "artifact": artifact,
                "version": updated["version"],
                "invalidated_artifacts": preview["invalidated_artifacts"],
                "model_requests": 0,
                "edited_file_preserved": True,
                "source_metadata_changed": False,
            }

        return self.store.transact(change, before_commit=check_commit)

    def set_exclusion(
        self, ref: str, *, excluded: bool, preview_token: str, confirmed: bool
    ) -> dict[str, Any]:
        self.authorize()
        if confirmed is not True:
            raise ContextError("confirmation_required", "请确认预览影响后再操作。")

        def change(state):
            self.authorize()
            preview = self._exclusion_preview(state, ref, excluded)
            if preview_token != preview["preview_token"]:
                raise ContextError("version_changed", "资料或任务状态已变化，请重新预览。")
            if preview["pending_jobs"]:
                raise ContextError("library_busy", "请先取消或完成待处理任务后再排除或恢复。")
            state["items"][ref]["excluded"] = excluded
            return {"material_ref": ref, "excluded": excluded, "files_deleted": False, "model_requests": 0}

        return self.store.transact(change)

    def _summary_preview(self, ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        from collection_context.processing.profiles import ModelCatalog
        from collection_context.processing.summary_refresh import capture, prepare

        payload = prepare(self.store, ref)
        snapshot = capture(self.store, ref)
        profile = ModelCatalog(self.store).get(payload["extraction"]["model_profiles"]["summary"])
        return {
            "material_ref": ref,
            "model": profile["model"],
            "preview_token": digest([self.store.workspace_id, payload]),
            "source_artifacts": [source["kind"] for source in snapshot["sources"]],
            "missing_or_stale": snapshot["coverage"]["missing_or_stale"],
            "max_model_requests": 1,
            "model_requests": 0,
            "estimated_cost": "unknown",
            "raw_media_reprocessed": False,
            "pending_jobs": self._pending_jobs(self.store.snapshot()),
        }, payload

    def preview_summary(self, ref: str) -> dict[str, Any]:
        self.authorize()
        preview, _ = self._summary_preview(ref)
        self.authorize()
        return preview

    def submit_summary(
        self, ref: str, *, preview_token: str, idempotency_key: str, fee_confirmed: bool
    ) -> dict[str, Any]:
        from collection_context.processing.summary_refresh import verify
        from collection_context.workflows.jobs import JobManager

        self.authorize()
        if fee_confirmed is not True:
            raise ContextError(
                "processing_authorization_required", "只更新总结也会调用模型，请确认一次请求的费用。"
            )
        preview, payload = self._summary_preview(ref)
        if preview_token != preview["preview_token"]:
            raise ContextError("version_changed", "正文或总结模型配置已变化，请重新预览。")

        def authorize_commit():
            self.authorize()
            verify(self.store, payload["extraction"])

        def admit(state, _job):
            authorize_commit()
            if self._pending_jobs(state):
                raise ContextError("library_busy", "请先完成或取消待处理任务，再更新总结。")

        mutation = JobManager(self.store)._submission(
            "process",
            payload,
            idempotency_key=idempotency_key,
            max_calls=1,
            admission=admit,
        )
        job = self.store.transact(mutation, before_commit=authorize_commit)
        return {
            "job_id": job["id"],
            "state": job["state"],
            "max_model_requests": 1,
            "model_requests": 0,
            "raw_media_reprocessed": False,
            "execution": "separately_authorized_worker",
        }

    def _export_files(self, state: dict[str, Any], ref: str, media_scope: str) -> dict[str, bytes]:
        if media_scope not in {"all", "none"}:
            raise ContextError("invalid_argument", "导出媒体范围须为 all 或 none。")
        item = self._item(state, ref)
        if item["excluded"]:
            raise ContextError("not_found", "请先恢复已排除资料，再导出。")
        files = {
            "original.md": original_text(item).encode(),
            "material.json": canonical_bytes(
                {
                    **public_item(item),
                    "content_version": item["content_hash"],
                    "content_untrusted": True,
                    "accuracy": "not_verified",
                    "artifact_states": {
                        kind: {"state": value["state"], "coverage": value["coverage"]}
                        for kind, value in item["artifacts"].items()
                    },
                }
            ),
        }
        for kind in sorted(item["artifacts"]):
            files[f"artifacts/{kind}.md"] = artifact_bytes(self.store, item, kind)
        if media_scope == "all":
            blobs = self._media(state, item)
            if sum(blob["bytes"] for blob in blobs) + sum(map(len, files.values())) > MAX_EXPORT_BYTES:
                raise ContextError("export_size_limit", "单资料包超过 64MB，请选择不含媒体导出。")
            inputs = PreparedInputs(self.store)
            for blob in blobs:
                extension = blob["path"].rsplit(".", 1)[-1]
                files[f"media/{blob['sha256']}.{extension}"] = inputs._read_blob(ref, blob)
            payload = self._media_manifest(state, item)
            if payload is not None:

                def exported_blob(blob):
                    extension = blob["path"].rsplit(".", 1)[-1]
                    return {
                        "name": f"media/{blob['sha256']}.{extension}",
                        "sha256": blob["sha256"],
                        "bytes": blob["bytes"],
                        "mime_type": blob["mime_type"],
                    }

                files["media-evidence.json"] = canonical_bytes(
                    {
                        "material_ref": ref,
                        "input_id": item["prepared_input"],
                        "content_hash": payload["content_hash"],
                        "kind": payload["kind"],
                        "coverage": payload["coverage"],
                        "originals": [exported_blob(blob) for blob in payload["originals"]],
                        "audio": [
                            {
                                **{key: value for key, value in segment.items() if key != "blob"},
                                "blob": exported_blob(segment["blob"]),
                            }
                            for segment in payload["audio"]
                        ],
                        "frames": [
                            {
                                "candidate": frame["candidate"],
                                "page_index": frame["page_index"],
                                "blob": exported_blob(frame["blob"]),
                            }
                            for frame in payload["frames"]
                        ],
                    }
                )
        if len(files) > MAX_EXPORT_FILES or sum(map(len, files.values())) > MAX_EXPORT_BYTES:
            raise ContextError("export_size_limit", "单资料包超过文件数或容量上限。")
        return files

    def _export_preview(
        self, state: dict[str, Any], ref: str, media_scope: str, files: dict[str, bytes]
    ) -> dict[str, Any]:
        records = [
            {"name": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in sorted(files.items())
        ]
        omitted = self._media(state, self._item(state, ref)) if media_scope == "none" else []
        return {
            "material_ref": ref,
            "media_scope": media_scope,
            "files": records,
            "total_bytes": sum(len(body) for body in files.values()),
            "omitted_media_files": len(omitted),
            "omitted_media_bytes": sum(blob["bytes"] for blob in omitted),
            "preview_token": digest(
                [self.store.workspace_id, state["generation"], ref, media_scope, records]
            ),
            "credentials_included": False,
            "not_a_full_backup": True,
            "model_requests": 0,
        }

    def export_preview(self, ref: str, *, media_scope: str = "none") -> dict[str, Any]:
        self.authorize()
        with self.store.writer():
            state = self.store.snapshot()
            files = self._export_files(state, ref, media_scope)
            self.authorize()
            return self._export_preview(state, ref, media_scope, files)

    def export_archive(self, ref: str, *, media_scope: str, preview_token: str, confirmed: bool) -> bytes:
        self.authorize()
        if confirmed is not True:
            raise ContextError("confirmation_required", "请确认资料导出范围。")
        with self.store.writer():
            state = self.store.snapshot()
            files = self._export_files(state, ref, media_scope)
            preview = self._export_preview(state, ref, media_scope, files)
            if preview_token != preview["preview_token"]:
                raise ContextError("version_changed", "导出内容已变化，请重新预览。")
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("export-manifest.json", canonical_bytes(preview))
                for name, body in sorted(files.items()):
                    archive.writestr(name, body)
            self.store.files.check_root()
            self.authorize()
            return output.getvalue()
