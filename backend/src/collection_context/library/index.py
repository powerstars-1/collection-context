"""Explicitly rebuilt, disposable file index; never a second source of truth."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes, digest, valid_id
from collection_context.library.store import LibraryStore

INDEX_VERSION = 2


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def library_version(state: dict[str, Any]) -> str:
    # Job progress does not invalidate a content index.
    return digest({"workspace_id": state["workspace_id"], "items": state["items"]})


def audio_not_applicable(item: dict[str, Any]) -> bool:
    prepared = item.get("media_preparation", {})
    return item["media_type"] == "image" or (
        prepared.get("input_hash") == item["content_hash"] and prepared.get("has_audio") is False
    )


def artifact_bytes(store: LibraryStore, item: dict[str, Any], kind: str) -> bytes:
    if kind == "audio" and audio_not_applicable(item):
        raise ContextError("artifact_not_applicable", "此资料没有音轨，转写不适用；未用图中文字代替音频。")
    artifact = item["artifacts"].get(kind)
    if artifact is None:
        raise ContextError("artifact_missing", "此类内容尚未提取。", next_action="在管理入口查看并手动处理。")
    # Do not permit an edited manifest to turn an artifact reference into an arbitrary file read.
    filename = {
        "original": "原文",
        "audio": "音频转写",
        "screen": "画面文字",
        "summary": "内容总结",
        "readable": "可读内容",
        "image": "图片提取",
        "user_note": "用户备注",
    }[kind]
    version = valid_id(artifact["version"])
    expected_path = f"content-vault/80_附件/抖音/{item['id']}/{version}/{filename}.md"
    if artifact["path"] != expected_path:
        raise ContextError("forbidden_path", "产物位置与受控引用不符。")
    body = store.files.read(expected_path, max_bytes=2_000_000)
    if hashlib.sha256(body).hexdigest() != artifact["sha256"]:
        raise ContextError("artifact_changed", "文件已在库外编辑；需显式核对后重新登记，不返回旧索引内容。")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise ContextError("invalid_artifact", "产物不是有效 UTF-8 文本。") from None
    return body


def original_text(item: dict[str, Any]) -> str:
    return "\n\n".join(part for part in (item["title"], item["body"]) if part)


def is_current(item: dict[str, Any], artifact: dict[str, Any]) -> bool:
    return (
        artifact["state"] == "ready"
        and (artifact["kind"] == "user_note" or artifact["input_hash"] == item["content_hash"])
        and (
            artifact["kind"] in {"original", "user_note"}
            or artifact.get("prepared_input") == item.get("prepared_input")
        )
    )


class FileIndex:
    def __init__(self, store: LibraryStore):
        self.store = store

    def rebuild(self) -> dict[str, Any]:
        """A maintenance action, never called implicitly by search/read/status."""
        with self.store.writer():
            state = self.store.snapshot()
            documents, gaps = {}, []
            for ref, item in state["items"].items():
                if item["excluded"]:
                    continue
                fields = {"metadata": "\n".join((item["title"], item["author"], item["body"]))}
                for kind, artifact in item["artifacts"].items():
                    if kind == "audio" and audio_not_applicable(item):
                        continue
                    if not is_current(item, artifact):
                        continue
                    try:
                        fields[kind] = artifact_bytes(self.store, item, kind).decode("utf-8")
                    except ContextError as error:
                        gaps.append({"material_ref": ref, "artifact": kind, "code": error.code})
                documents[ref] = fields
            data = {
                "schema_version": INDEX_VERSION,
                "workspace_id": self.store.workspace_id,
                "library_version": library_version(state),
                "documents": documents,
                "gaps": gaps,
            }
            body = canonical_bytes(data)
            if len(body) > 16_000_000:
                raise ContextError("index_limit", "索引超过当前验证规模；未覆盖已有可用索引。")
            version = "x_" + uuid.uuid4().hex
            self.store.files.write(f".context/索引/{version}.json", body)
            self.store.files.write(
                ".context/索引/CURRENT.json",
                canonical_bytes({"version": version, "sha256": hashlib.sha256(body).hexdigest()}),
                replace=True,
            )
            return {"library_version": data["library_version"], "indexed_items": len(documents), "gaps": gaps}

    def load(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            pointer = json.loads(self.store.files.read(".context/索引/CURRENT.json", max_bytes=65_536))
            version = valid_id(pointer["version"])
            body = self.store.files.read(f".context/索引/{version}.json")
            data = json.loads(body)
            if (
                hashlib.sha256(body).hexdigest() != pointer["sha256"]
                or data["schema_version"] != INDEX_VERSION
                or data["workspace_id"] != self.store.workspace_id
            ):
                raise ValueError
        except ContextError as error:
            if error.code in {"not_found", "forbidden_path"}:
                raise ContextError("index_unavailable", "尚无可用索引；请显式重建索引。") from None
            raise
        except (ValueError, TypeError, KeyError):
            raise ContextError("index_unavailable", "索引校验失败；可从原资料显式重建。") from None
        if data["library_version"] != library_version(state):
            raise ContextError("index_outdated", "资料清单已变化；请重建索引后再搜索。", retryable=True)
        return data
