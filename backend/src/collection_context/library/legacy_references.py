"""Owner-confirmed aliases, never a filesystem read from a decoded legacy ref."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from collection_context.application.contracts import ContextError, digest, item_id, valid_id
from collection_context.infrastructure.files import SafeFiles
from collection_context.library.store import LibraryStore

MAX_ALIASES = 10_000


def legacy_path(reference: str) -> str:
    """Recognize canonical old Douyin card names without opening their paths."""
    try:
        if type(reference) is not str or not reference.startswith("m1:") or len(reference) > 1400:
            raise ValueError
        encoded = reference[3:]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        path = raw.decode("utf-8")
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            raise ValueError
        parts = SafeFiles.parts(path)
        if (
            len(parts) < 3
            or parts[:2] != ["00_素材收件箱", "抖音"]
            or not parts[-1].endswith(".md")
            or any(any(ord(c) < 32 or ord(c) == 127 for c in part) for part in parts)
        ):
            raise ValueError
        return path
    except (ValueError, binascii.Error, UnicodeError, ContextError):
        raise ContextError("invalid_reference", "旧引用须为原入口返回的抖音素材卡引用。") from None


def _binding(state: dict[str, Any], reference: str) -> dict[str, str] | None:
    aliases = state.get("legacy_references", {})
    if not isinstance(aliases, dict) or len(aliases) > MAX_ALIASES:
        raise ContextError("corrupt_workspace", "旧引用映射清单无效；未猜测目标资料。")
    if reference not in aliases:
        return None
    entry = aliases[reference]
    try:
        if not isinstance(entry, dict) or set(entry) != {"material_ref", "native_id"}:
            raise ValueError
        target = valid_id(entry["material_ref"])
        item = state["items"][target]
        if (
            item["id"] != target
            or item["platform"] != "douyin"
            or item["native_id"] != entry["native_id"]
            or item_id("douyin", entry["native_id"]) != target
        ):
            raise ValueError
        return entry
    except (ValueError, TypeError, KeyError, ContextError):
        raise ContextError("corrupt_workspace", "旧引用映射与目标资料身份不符。") from None


def resolve_reference(state: dict[str, Any], reference: str) -> str:
    if type(reference) is str and reference.startswith("m1:"):
        legacy_path(reference)
        entry = _binding(state, reference)
        if entry is None:
            raise ContextError(
                "legacy_reference_unmapped",
                "此旧引用尚未关联新资料；不会按解码路径读取旧库。",
                next_action="由库主人核对已入库资料后，用 bind-legacy-ref 预览并确认映射。",
            )
        return entry["material_ref"]
    return valid_id(reference)


def validate_legacy_references(state: dict[str, Any]) -> None:
    aliases = state.get("legacy_references", {})
    if not isinstance(aliases, dict) or len(aliases) > MAX_ALIASES:
        raise ContextError("corrupt_workspace", "旧引用映射清单无效。")
    for reference in aliases:
        legacy_path(reference)
        _binding(state, reference)


class LegacyReferences:
    """Local owner maintenance only; not exposed as a read/add AI tool."""

    def __init__(self, store: LibraryStore):
        self.store = store

    def _preview(self, state: dict[str, Any], legacy_ref: str, target: str) -> dict[str, Any]:
        legacy_path(legacy_ref)
        valid_id(target)
        item = state["items"].get(target)
        if item is None or item["excluded"]:
            raise ContextError("not_found", "目标资料不存在或已排除；未创建映射。")
        if item_id(item["platform"], item["native_id"]) != target:
            raise ContextError("corrupt_workspace", "目标资料身份无效；未创建映射。")
        existing = _binding(state, legacy_ref)
        if existing is not None and existing["material_ref"] != target:
            raise ContextError("legacy_reference_conflict", "旧引用已有不同目标；不覆盖既有引用。")
        binding = {"material_ref": target, "native_id": item["native_id"]}
        return {
            "legacy_ref": legacy_ref,
            "material_ref": target,
            "title": item["title"],
            "source_url": item["source_url"],
            "already_bound": existing is not None,
            "preview_token": digest(
                {
                    "workspace_id": self.store.workspace_id,
                    "legacy_ref": legacy_ref,
                    "binding": binding,
                    "existing": existing,
                    "content_hash": item["content_hash"],
                }
            ),
            "model_requests": 0,
            "legacy_files_read": False,
            "warning": "请自行核对旧引用对应这条作品。映射不导入旧卡正文、附件、登录态或密钥。",
        }

    def preview(self, legacy_ref: str, target: str) -> dict[str, Any]:
        return self._preview(self.store.snapshot(), legacy_ref, target)

    def bind(self, legacy_ref: str, target: str, *, preview_token: str) -> dict[str, Any]:
        if type(preview_token) is not str or len(preview_token) != 64:
            raise ContextError("confirmation_required", "请先预览，再携带同一映射的预览标识确认。")

        def change(state: dict[str, Any]) -> dict[str, Any]:
            preview = self._preview(state, legacy_ref, target)
            if preview["preview_token"] != preview_token:
                raise ContextError("version_changed", "映射或目标正文已变化，请重新预览。")
            aliases = state.setdefault("legacy_references", {})
            if legacy_ref not in aliases and len(aliases) >= MAX_ALIASES:
                raise ContextError("workspace_limit", "旧引用映射超过本版本已验证的数量上限。")
            aliases[legacy_ref] = {"material_ref": target, "native_id": state["items"][target]["native_id"]}
            return {**preview, "bound": True}

        return self.store.transact(change)
