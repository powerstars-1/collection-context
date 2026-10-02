"""Bounded, version-bound raster evidence reads; never public media directories."""

from __future__ import annotations

from typing import Any

from collection_context.application.contracts import ContextError, valid_id
from collection_context.application.service import ContextService
from collection_context.infrastructure.media import validate_raster
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs

MAX_IMAGE_BYTES = 8_000_000


def raster_mime(data: bytes) -> str:
    return validate_raster(data, max_bytes=MAX_IMAGE_BYTES).mime_type


class MediaEvidence:
    def __init__(self, store: LibraryStore):
        self.store = store
        self.inputs = PreparedInputs(store)

    def _current(self, ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        item = ContextService._item(self.store.snapshot(), ref)
        identity = item.get("prepared_input")
        if identity is None:
            raise ContextError("artifact_missing", "尚未准备原图或关键帧；查看画面不会自动下载或调用模型。")
        payload = self.inputs.manifest(identity)
        if (
            payload["material_ref"] != item["id"]
            or payload["content_hash"] != item["content_hash"]
            or payload["kind"] != item["media_type"]
        ):
            raise ContextError("version_changed", "媒体与当前正文不一致，请在处理页核对。")
        return item, payload

    def listing(self, ref: str) -> dict[str, Any]:
        item, payload = self._current(ref)
        ref = item["id"]
        identity = item["prepared_input"]
        return {
            "material_ref": ref,
            "input_id": identity,
            "frames": [
                {
                    "frame_id": frame["candidate"]["evidence_id"],
                    "seconds": frame["candidate"]["nominal_seconds"],
                    "page_number": frame["page_index"] + 1 if frame["page_index"] is not None else None,
                    "url": f"/v1/collections/{ref}/frames/{identity}/{frame['candidate']['evidence_id']}",
                }
                for frame in payload["frames"]
            ],
            "evidence_kind": "original_pages" if payload["kind"] == "image" else "selected_frames",
            "accuracy": "not_verified",
            "content_untrusted": True,
            "model_requests": 0,
        }

    def image(self, ref: str, identity: str, frame_id: str) -> tuple[bytes, str]:
        valid_id(identity)
        valid_id(frame_id)
        item, payload = self._current(ref)
        if item["prepared_input"] != identity:
            raise ContextError("version_changed", "画面输入已经变化，请刷新资料后重新选择。")
        frame = next((f for f in payload["frames"] if f["candidate"]["evidence_id"] == frame_id), None)
        if frame is None:
            raise ContextError("not_found", "这张图不属于当前资料。")
        blob = frame["blob"]
        if type(blob["bytes"]) is not int or not 0 < blob["bytes"] <= MAX_IMAGE_BYTES:
            raise ContextError("media_input_limit", "图片超过本页读取上限，请在本机查看原文件。")
        data = self.inputs._read_blob(item["id"], blob)
        mime = raster_mime(data)
        if mime != blob["mime_type"]:
            raise ContextError("unsupported_media", "图片实际格式与登记类型不一致。")
        current = ContextService._item(self.store.snapshot(), ref)
        if current["prepared_input"] != identity or current["content_hash"] != item["content_hash"]:
            raise ContextError("version_changed", "读取时资料已经变化，未返回旧画面。")
        return data, mime
