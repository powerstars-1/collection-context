"""Consistent, manifest-verified library backups; never exports credentials or login state."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from collection_context.application.contracts import (
    SCHEMA_VERSION,
    ContextError,
    canonical_bytes,
    utc_now,
    valid_id,
)
from collection_context.infrastructure.files import SafeFiles
from collection_context.library.index import FileIndex
from collection_context.library.store import LEGACY_GUARD, WRITER_PROTOCOL, LibraryStore

BACKUP_VERSION = 1
MAX_FILES = 10_000
MAX_TOTAL_BYTES = 2_000_000_000
MAX_FILE_BYTES = 512_000_000
MANIFEST_NAME = "backup-manifest.json"
STATE_NAME = "library-state.json"
FILES_PREFIX = "files/"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1_048_576):
            result.update(block)
    return result.hexdigest()


def _external_path(store: LibraryStore, path: Path, *, kind: str) -> Path:
    value = path.absolute()
    if value.is_symlink() or value.resolve(strict=False).is_relative_to(store.files.root.resolve()):
        raise ContextError("backup_path_overlap", f"{kind}必须位于资料库之外。")
    return value


def _disabled_state(state: dict[str, Any], *, include_media: bool) -> dict[str, Any]:
    result = copy.deepcopy(state)
    settings = result.setdefault("settings", {})
    settings["auto_sync"] = False
    settings["auto_process"] = False
    settings["automatic_resumed_jobs"] = []
    for entry in settings.get("sync_schedules", {}).values():
        if isinstance(entry, dict):
            entry["enabled"] = False
    if include_media:
        return result
    result.pop("prepared_inputs", None)
    for item in result["items"].values():
        item.pop("prepared_input", None)
        item.pop("media_preparation", None)
        item.pop("source_input_binding", None)
        for artifact in item["artifacts"].values():
            artifact.pop("prepared_input", None)
    for job in result["jobs"].values():
        if job.get("kind") == "process" and job.get("state") in {"queued", "running"}:
            job["state"] = "blocked"
            job["error"] = ContextError(
                "backup_media_omitted",
                "此备份未包含媒体；原处理任务已阻止，不自动重发或计费。",
            ).as_dict()
            job["updated_at"] = utc_now()
    return result


def _add_file(files: dict[str, bytes], path: str, body: bytes) -> None:
    SafeFiles.parts(path)
    if len(body) > MAX_FILE_BYTES:
        raise ContextError("backup_file_limit", "单个备份文件超过当前验证上限。")
    previous = files.setdefault(path, body)
    if previous != body:
        raise ContextError("backup_path_conflict", "备份中同一路径对应不同内容。")


def _collect_files(
    store: LibraryStore, state: dict[str, Any], *, include_media: bool
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    files: dict[str, bytes] = {}
    omitted: list[dict[str, Any]] = []
    for ref, item in state["items"].items():
        _add_file(files, FileIndex.readable_path(ref), FileIndex._readable_markdown(item))
        for artifact in item["artifacts"].values():
            body = store.files.read(artifact["path"], max_bytes=MAX_FILE_BYTES)
            if _sha256(body) != artifact["sha256"]:
                raise ContextError("artifact_changed", "产物文件与提交清单不符；未生成备份。")
            _add_file(files, artifact["path"], body)
    for identity, record in state.get("prepared_inputs", {}).items():
        valid_id(identity)
        manifest_body = store.files.read(record["path"], max_bytes=2_000_000)
        if _sha256(manifest_body) != record["sha256"]:
            raise ContextError("media_changed", "媒体输入清单已改变；未生成备份。")
        try:
            payload = json.loads(manifest_body)
            if payload["material_ref"] != record["material_ref"]:
                raise ValueError
            blobs = [*payload["originals"]]
            blobs.extend(segment["blob"] for segment in payload["audio"])
            blobs.extend(frame["blob"] for frame in payload["frames"])
        except (ValueError, TypeError, KeyError):
            raise ContextError("invalid_input", "媒体输入清单损坏；未生成备份。") from None
        if include_media:
            _add_file(files, record["path"], manifest_body)
        for blob in blobs:
            body = store.files.read(blob["path"], max_bytes=MAX_FILE_BYTES)
            if len(body) != blob["bytes"] or _sha256(body) != blob["sha256"]:
                raise ContextError("media_changed", "媒体快照与清单不符；未生成备份。")
            if include_media:
                _add_file(files, blob["path"], body)
            else:
                omitted.append(
                    {
                        "path": blob["path"],
                        "bytes": len(body),
                        "sha256": blob["sha256"],
                        "reason": "media_scope_none",
                    }
                )
    if len(files) > MAX_FILES or sum(len(body) for body in files.values()) > MAX_TOTAL_BYTES:
        raise ContextError("backup_size_limit", "备份文件数或总大小超过当前验证上限。")
    return files, sorted(omitted, key=lambda value: value["path"])


def create_backup(
    store: LibraryStore, output: Path, *, media_scope: str
) -> dict[str, Any]:
    if media_scope not in {"all", "none"}:
        raise ContextError("invalid_argument", "媒体范围须明确为 all 或 none。")
    output = _external_path(store, output, kind="备份输出")
    if output.exists() or not output.parent.is_dir() or output.parent.is_symlink():
        raise ContextError("backup_destination_invalid", "备份目标须是库外已存在目录中的新文件。")
    temporary: Path | None = None
    try:
        with store.writer():
            state = store.snapshot()
            if state["settings"].get("auto_sync") is True or state["settings"].get("auto_process") is True:
                raise ContextError(
                    "backup_requires_pause", "请先关闭自动同步和自动处理，再创建一致备份。"
                )
            files, omitted = _collect_files(store, state, include_media=media_scope == "all")
            backup_state = _disabled_state(state, include_media=media_scope == "all")
            state_body = canonical_bytes(backup_state)
            records: list[dict[str, Any]] = [
                {"path": path, "bytes": len(body), "sha256": _sha256(body)}
                for path, body in sorted(files.items())
            ]
            manifest = {
                "backup_version": BACKUP_VERSION,
                "schema_version": SCHEMA_VERSION,
                "backup_id": "b_" + uuid.uuid4().hex,
                "workspace_id": store.workspace_id,
                "created_at": utc_now(),
                "media_scope": media_scope,
                "automation_restored": False,
                "state_sha256": _sha256(state_body),
                "file_count": len(records),
                "total_bytes": sum(int(record["bytes"]) for record in records),
                "files": records,
                "omitted_media": omitted,
                "excluded_members": [
                    ".context/访问规则.json",
                    ".context/索引/",
                    ".context/暂存/",
                    "平台登录目录",
                    "模型密钥目录",
                ],
            }
            handle, name = tempfile.mkstemp(prefix=".context-backup-", suffix=".zip", dir=output.parent)
            os.close(handle)
            temporary = Path(name)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(MANIFEST_NAME, canonical_bytes(manifest))
                archive.writestr(STATE_NAME, state_body)
                for path, body in sorted(files.items()):
                    archive.writestr(FILES_PREFIX + path, body)
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise ContextError(
                    "backup_destination_invalid", "备份目标已存在，未覆盖。"
                ) from None
            except OSError:
                raise ContextError(
                    "storage_unavailable", "备份无法原子发布到目标目录，未留下半成品。"
                ) from None
            temporary.unlink()
            temporary = None
            return {
                "backup_id": manifest["backup_id"],
                "workspace_id": store.workspace_id,
                "archive": str(output),
                "media_scope": media_scope,
                "file_count": len(records),
                "total_bytes": manifest["total_bytes"],
                "omitted_media": len(omitted),
                "sha256": _file_sha256(output),
                "automation_restored": False,
            }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _archive_data(archive_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    if not archive_path.is_file() or archive_path.is_symlink():
        raise ContextError("backup_invalid", "备份必须是普通 ZIP 文件。")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len(names) > MAX_FILES + 2:
                raise ValueError
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    entry.is_dir()
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or entry.flag_bits & 0x1
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                    or entry.file_size > MAX_FILE_BYTES
                ):
                    raise ValueError
            if MANIFEST_NAME not in names or STATE_NAME not in names:
                raise ValueError
            if sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES + 32_000_000:
                raise ValueError
            manifest_body = archive.read(MANIFEST_NAME)
            state_body = archive.read(STATE_NAME)
            manifest = json.loads(manifest_body)
            state = json.loads(state_body)
            paths = [record["path"] for record in manifest["files"]]
            if len(paths) != len(set(paths)) or manifest["media_scope"] not in {"all", "none"}:
                raise ValueError
            expected = {FILES_PREFIX + record["path"] for record in manifest["files"]}
            if set(names) != {MANIFEST_NAME, STATE_NAME, *expected}:
                raise ValueError
            if (
                manifest["backup_version"] != BACKUP_VERSION
                or manifest["schema_version"] != SCHEMA_VERSION
                or manifest["workspace_id"] != state["workspace_id"]
                or manifest["state_sha256"] != _sha256(state_body)
                or manifest["file_count"] != len(manifest["files"])
                or manifest["total_bytes"] != sum(record["bytes"] for record in manifest["files"])
            ):
                raise ValueError
            valid_id(manifest["backup_id"])
            valid_id(manifest["workspace_id"])
            restored: dict[str, bytes] = {}
            for record in manifest["files"]:
                SafeFiles.parts(record["path"])
                body = archive.read(FILES_PREFIX + record["path"])
                if len(body) != record["bytes"] or _sha256(body) != record["sha256"]:
                    raise ValueError
                _add_file(restored, record["path"], body)
            return manifest, state, restored
    except (
        OSError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ContextError,
    ):
        raise ContextError("backup_invalid", "备份结构、路径、大小或完整哈希校验失败。") from None


def _validate_state(state: dict[str, Any], workspace_id: str) -> None:
    try:
        if (
            state["schema_version"] != SCHEMA_VERSION
            or state["workspace_id"] != workspace_id
            or not isinstance(state["generation"], int)
            or not isinstance(state["items"], dict)
            or not isinstance(state["jobs"], dict)
            or not isinstance(state["settings"], dict)
            or state["settings"].get("auto_sync") is not False
            or state["settings"].get("auto_process") is not False
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ContextError("backup_invalid", "备份状态不兼容或自动任务未安全关闭。") from None


def _validate_references(state: dict[str, Any], files: dict[str, bytes]) -> None:
    filenames = {
        "original": "原文",
        "audio": "音频转写",
        "screen": "画面文字",
        "summary": "内容总结",
        "readable": "可读内容",
        "image": "图片提取",
        "user_note": "用户备注",
    }
    expected: set[str] = set()
    try:
        for ref, item in state["items"].items():
            valid_id(ref)
            expected.add(FileIndex.readable_path(ref))
            for kind, artifact in item["artifacts"].items():
                version = valid_id(artifact["version"])
                path = f"content-vault/80_附件/抖音/{ref}/{version}/{filenames[kind]}.md"
                if artifact["path"] != path:
                    raise ValueError
                body = files.get(path)
                if body is None or _sha256(body) != artifact["sha256"]:
                    raise ValueError
                expected.add(path)
        for identity, record in state.get("prepared_inputs", {}).items():
            valid_id(identity)
            path = f".context/输入/{identity}.json"
            body = files.get(path)
            if (
                record["path"] != path
                or body is None
                or _sha256(body) != record["sha256"]
            ):
                raise ValueError
            payload = json.loads(body)
            material_ref = valid_id(payload["material_ref"])
            if record["material_ref"] != material_ref:
                raise ValueError
            expected.add(path)
            blobs = [*payload["originals"]]
            blobs.extend(segment["blob"] for segment in payload["audio"])
            blobs.extend(frame["blob"] for frame in payload["frames"])
            for blob in blobs:
                blob_path = blob["path"]
                SafeFiles.parts(blob_path)
                if not blob_path.startswith(
                    f"content-vault/80_附件/抖音/{material_ref}/输入/"
                ):
                    raise ValueError
                blob_body = files.get(blob_path)
                if (
                    blob_body is None
                    or len(blob_body) != blob["bytes"]
                    or _sha256(blob_body) != blob["sha256"]
                ):
                    raise ValueError
                expected.add(blob_path)
        if expected != set(files):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ContextError):
        raise ContextError(
            "backup_invalid", "备份的资料引用、附件或受控路径不完整。"
        ) from None


def restore_backup(archive_path: Path, destination: Path) -> dict[str, Any]:
    destination = destination.absolute()
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise ContextError("restore_destination_not_empty", "只能恢复到新路径或空目录，不覆盖已有库。")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise ContextError("restore_destination_invalid", "恢复目标的上级目录必须已存在。")
    archive_path = archive_path.absolute()
    if archive_path.resolve(strict=False).is_relative_to(destination.resolve(strict=False)):
        raise ContextError("restore_destination_invalid", "备份文件不能位于恢复目标中。")
    manifest, state, files = _archive_data(archive_path)
    _validate_state(state, manifest["workspace_id"])
    _validate_references(state, files)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-restore-", dir=destination.parent)
    )
    published = False
    try:
        with SafeFiles(staging) as safe:
            for path, body in sorted(files.items()):
                safe.write(path, body)
            safe.write(LEGACY_GUARD, LibraryStore._guard_body(manifest["workspace_id"]))
            safe.write(
                "context-workspace.json",
                canonical_bytes(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "workspace_id": manifest["workspace_id"],
                        "vault_dir": "content-vault",
                        "created_at": utc_now(),
                        "writer_protocol": WRITER_PROTOCOL,
                        "restored_from": manifest["backup_id"],
                    }
                ),
            )
            LibraryStore._publish(safe, state)
        restored = LibraryStore(staging)
        try:
            restored_state = restored.snapshot()
            if restored_state != state:
                raise ContextError("restore_validation_failed", "恢复后提交状态与备份不一致。")
            index = FileIndex(restored).rebuild()
        finally:
            restored.close()
        if destination.exists():
            destination.rmdir()  # It was proven empty above; never removes user files.
        os.replace(staging, destination)
        published = True
        return {
            "backup_id": manifest["backup_id"],
            "workspace_id": manifest["workspace_id"],
            "destination": str(destination),
            "media_scope": manifest["media_scope"],
            "restored_files": len(files),
            "omitted_media": len(manifest["omitted_media"]),
            "indexed_items": index["indexed_items"],
            "auto_sync": False,
            "auto_process": False,
        }
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
