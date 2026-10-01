"""Own immutable media-input registry. No temporary path or decoder command becomes a job input."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes, digest, valid_id
from collection_context.infrastructure.media import (
    PROCESSOR_VERSION,
    AudioSegment,
    FrameCandidate,
    LocalMedia,
    MediaPolicy,
    PreparedFrame,
)
from collection_context.library.store import LibraryStore

MAX_TOTAL = 512_000_000
EXTENSIONS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/wav": "wav",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


class PreparedInputs:
    def __init__(self, store: LibraryStore):
        self.store = store

    @staticmethod
    def _path(ref: str, sha: str, mime: str) -> str:
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ContextError("invalid_input", "媒体哈希无效。")
        if not isinstance(mime, str) or mime not in EXTENSIONS:
            raise ContextError("unsupported_media", "尚未支持此媒体快照格式。")
        return f"content-vault/80_附件/抖音/{valid_id(ref)}/输入/{sha}.{EXTENSIONS[mime]}"

    def _blob(self, ref: str, data: bytes, mime: str) -> dict[str, Any]:
        if not isinstance(data, bytes) or not 0 < len(data) <= 128_000_000:
            raise ContextError("media_input_limit", "媒体输入为空或超过登记上限。")
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(ref, sha, mime)
        try:
            self.store.files.write(path, data)
        except ContextError as error:
            if error.code != "write_conflict":
                raise
            existing = self.store.files.read(path, max_bytes=128_000_000)
            if existing != data:
                raise ContextError("media_changed", "既有快照已被修改；未覆盖或上传。") from None
        return {"path": path, "sha256": sha, "mime_type": mime, "bytes": len(data)}

    def _read_blob(self, ref: str, blob: dict[str, Any]) -> bytes:
        if not isinstance(blob, dict) or set(blob) != {"path", "sha256", "mime_type", "bytes"}:
            raise ContextError("invalid_input", "媒体快照描述无效。")
        if blob["path"] != self._path(ref, blob["sha256"], blob["mime_type"]):
            raise ContextError("forbidden_path", "媒体指针不在该资料的受控位置。")
        if type(blob["bytes"]) is not int or not 0 < blob["bytes"] <= 128_000_000:
            raise ContextError("media_input_limit", "媒体快照大小无效。")
        data = self.store.files.read(blob["path"], max_bytes=blob["bytes"])
        if len(data) != blob["bytes"] or hashlib.sha256(data).hexdigest() != blob["sha256"]:
            raise ContextError("media_changed", "登记的媒体快照已改变；未发模型请求。")
        return data

    def save(
        self,
        ref: str,
        *,
        content_hash: str,
        originals: list[tuple[bytes, str]],
        audio: list[AudioSegment],
        frames: list[PreparedFrame],
        coverage: dict[str, Any],
        processor_version: str,
        strategy_hash: str,
        kind: str,
    ) -> str:
        valid_id(ref)
        if (
            kind not in {"video", "image"}
            or not originals
            or len(originals) > 240
            or len(audio) > 64
            or not 0 < len(frames) <= 240
        ):
            raise ContextError("media_input_limit", "原始媒体、音频段或画面数量超出本批登记上限；未截断。")
        if kind == "image" and (audio or len(originals) != len(frames)):
            raise ContextError("invalid_input", "图文须完整保留有序原图，不含音频段。")
        if (
            not isinstance(coverage, dict)
            or not isinstance(processor_version, str)
            or not 1 <= len(processor_version) <= 200
        ):
            raise ContextError("invalid_input", "媒体策略或覆盖记录无效。")
        if kind == "video" and (
            (not audio and coverage.get("has_audio") is not False)
            or (audio and coverage.get("has_audio") is False)
        ):
            raise ContextError(
                "audio_presence_unknown", "尚未确认无音轨，或音轨记录矛盾；不能把缺失音频当作不适用。"
            )
        if not isinstance(strategy_hash, str) or not 1 <= len(strategy_hash) <= 200:
            raise ContextError("invalid_input", "媒体准备策略指纹无效。")
        total = (
            sum(len(data) for data, _ in originals)
            + sum(len(seg.data) for seg in audio)
            + sum(len(frame.data) for frame in frames)
        )
        if total > MAX_TOTAL:
            raise ContextError("media_input_limit", "媒体快照总大小超出本批登记上限；未静默裁剪。")
        payload: dict[str, Any] = {
            "schema_version": 1,
            "material_ref": ref,
            "content_hash": content_hash,
            "kind": kind,
            "processor_version": processor_version,
            "strategy_hash": strategy_hash,
            "coverage": coverage,
            "originals": [],
            "audio": [],
            "frames": [],
        }
        for data, mime in originals:
            if (
                kind == "video"
                and mime not in {"video/mp4", "video/webm"}
                or kind == "image"
                and not mime.startswith("image/")
            ):
                raise ContextError("unsupported_media", "原始媒体类型与资料类型不匹配。")
            payload["originals"].append(self._blob(ref, data, mime))
        for segment in audio:
            if segment.sha256 != hashlib.sha256(segment.data).hexdigest():
                raise ContextError("media_changed", "音频段哈希与输入不同。")
            metadata = {key: value for key, value in asdict(segment).items() if key not in {"data", "sha256"}}
            payload["audio"].append({**metadata, "blob": self._blob(ref, segment.data, "audio/wav")})
        for index, frame in enumerate(frames):
            if frame.sha256 != hashlib.sha256(frame.data).hexdigest():
                raise ContextError("media_changed", "画面哈希与输入不同。")
            payload["frames"].append(
                {
                    "candidate": asdict(frame.candidate),
                    "page_index": index if kind == "image" else None,
                    "blob": self._blob(ref, frame.data, frame.mime_type),
                }
            )
        # Normalize tuples into JSON before hashing; no byte data or temporary path appears in manifest.
        payload = json.loads(canonical_bytes(payload))
        self._validate(payload)
        identity = "u_" + digest(payload)
        body = canonical_bytes(payload)
        path = f".context/输入/{identity}.json"
        if len(body) > 2_000_000:
            raise ContextError("invalid_input", "输入清单超过上限。")

        def save(state):
            item = state["items"].get(ref)
            if not item or item["excluded"]:
                raise ContextError("not_found", "资料不存在或已排除。")
            if item["content_hash"] != content_hash or item["media_type"] != kind:
                raise ContextError("version_changed", "准备期间原资料发生变化，未提交旧输入。")
            registry = state.setdefault("prepared_inputs", {})
            if identity not in registry:
                try:
                    self.store.files.write(path, body)
                except ContextError as error:
                    if error.code != "write_conflict":
                        raise
                    if self.store.files.read(path, max_bytes=2_000_000) != body:
                        raise ContextError("media_changed", "未提交输入清单已被修改；未覆盖。") from None
                registry[identity] = {
                    "path": path,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "material_ref": ref,
                }
            elif self.store.files.read(path, max_bytes=2_000_000) != body:
                raise ContextError("media_changed", "既有输入清单已被修改；未重复登记为成功。")
            if item.get("prepared_input") != identity:
                for artifact_kind, artifact in item["artifacts"].items():
                    if artifact_kind not in {"original", "user_note"}:
                        artifact["state"] = "stale"
            item["prepared_input"] = identity
            item["media_preparation"] = {"input_hash": content_hash, "kind": kind, "has_audio": bool(audio)}
            return identity

        return self.store.transact(save)

    @staticmethod
    def _validate(payload: dict[str, Any]) -> None:
        try:
            if (
                set(payload)
                != {
                    "schema_version",
                    "material_ref",
                    "content_hash",
                    "kind",
                    "processor_version",
                    "strategy_hash",
                    "coverage",
                    "originals",
                    "audio",
                    "frames",
                }
                or payload["schema_version"] != 1
            ):
                raise ValueError
            valid_id(payload["material_ref"])
            if payload["kind"] not in {"video", "image"} or not isinstance(payload["coverage"], dict):
                raise ValueError
            if (
                not 1 <= len(payload["originals"]) <= 240
                or not 0 <= len(payload["audio"]) <= 64
                or not 1 <= len(payload["frames"]) <= 240
            ):
                raise ValueError
            if payload["kind"] == "image" and (
                payload["audio"] or len(payload["originals"]) != len(payload["frames"])
            ):
                raise ValueError
            if payload["kind"] == "video" and (
                (not payload["audio"] and payload["coverage"].get("has_audio") is not False)
                or (payload["audio"] and payload["coverage"].get("has_audio") is False)
            ):
                raise ValueError
            seen: set[str] = set()
            for segment in payload["audio"]:
                if set(segment) != {
                    "evidence_id",
                    "start_seconds",
                    "end_seconds",
                    "decoded_seconds",
                    "overlaps_previous",
                    "blob",
                }:
                    raise ValueError
                valid_id(segment["evidence_id"])
                if segment["evidence_id"] in seen or type(segment["overlaps_previous"]) is not bool:
                    raise ValueError
                seen.add(segment["evidence_id"])
                for key in ("start_seconds", "end_seconds", "decoded_seconds"):
                    if (
                        type(segment[key]) not in {int, float}
                        or not math.isfinite(segment[key])
                        or segment[key] < 0
                    ):
                        raise ValueError
                if (
                    segment["end_seconds"] <= segment["start_seconds"]
                    or segment["decoded_seconds"] <= 0
                    or segment["blob"]["mime_type"] != "audio/wav"
                ):
                    raise ValueError
            for index, frame in enumerate(payload["frames"]):
                if set(frame) != {"candidate", "page_index", "blob"}:
                    raise ValueError
                candidate = frame["candidate"]
                if set(candidate) != {
                    "evidence_id",
                    "sample_index",
                    "nominal_seconds",
                    "reasons",
                    "change_score",
                }:
                    raise ValueError
                valid_id(candidate["evidence_id"])
                if candidate["evidence_id"] in seen:
                    raise ValueError
                seen.add(candidate["evidence_id"])
                if type(candidate["sample_index"]) is not int or candidate["sample_index"] < 0:
                    raise ValueError
                for key in ("nominal_seconds", "change_score"):
                    if (
                        type(candidate[key]) not in {int, float}
                        or not math.isfinite(candidate[key])
                        or candidate[key] < 0
                    ):
                        raise ValueError
                if (
                    not isinstance(candidate["reasons"], list)
                    or len(candidate["reasons"]) > 20
                    or any(
                        not isinstance(reason, str) or len(reason) > 100 for reason in candidate["reasons"]
                    )
                ):
                    raise ValueError
                if not frame["blob"]["mime_type"].startswith("image/") or frame["page_index"] != (
                    index if payload["kind"] == "image" else None
                ):
                    raise ValueError
            blobs = (
                payload["originals"]
                + [s["blob"] for s in payload["audio"]]
                + [f["blob"] for f in payload["frames"]]
            )
            if sum(blob["bytes"] for blob in blobs) > MAX_TOTAL:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ContextError("invalid_input", "输入清单结构、范围或证据身份无效。") from None

    def manifest(self, identity: str) -> dict[str, Any]:
        """Validate the registered immutable manifest without loading retained media.

        Callers must verify each blob they actually use. Extraction and reuse continue
        to use load(), which also verifies every retained original.
        """
        valid_id(identity)
        registered = self.store.snapshot().get("prepared_inputs", {}).get(identity)
        if not registered:
            raise ContextError("input_missing", "任务固定的媒体输入未登记。")
        path = f".context/输入/{identity}.json"
        if registered["path"] != path:
            raise ContextError("forbidden_path", "输入清单位置无效。")
        body = self.store.files.read(path, max_bytes=2_000_000)
        if hashlib.sha256(body).hexdigest() != registered["sha256"]:
            raise ContextError("media_changed", "媒体输入清单校验失败。")
        try:
            payload = json.loads(body)
            self._validate(payload)
            if identity != "u_" + digest(payload) or registered["material_ref"] != payload["material_ref"]:
                raise ValueError
        except (TypeError, ValueError):
            raise ContextError("invalid_input", "输入清单身份或结构无效。") from None
        return payload

    def load(self, identity: str) -> dict[str, Any]:
        payload = self.manifest(identity)
        for blob in payload["originals"]:
            self._read_blob(
                payload["material_ref"], blob
            )  # Verify retained originals, not used as a fallback.
        return payload

    def reusable_source(self, ref: str, source_asset_hash: str) -> str | None:
        """Reuse only an explicitly bound source snapshot; verify every retained blob without decoding."""
        item = self.store.get(ref)
        binding = item.get("source_input_binding")
        identity = item.get("prepared_input")
        if not binding or not identity:
            return None
        if not isinstance(binding, dict) or set(binding) != {"input_id", "content_hash", "source_asset_hash"}:
            raise ContextError("invalid_input", "来源媒体绑定记录损坏。")
        if binding != {
            "input_id": identity,
            "content_hash": item["content_hash"],
            "source_asset_hash": source_asset_hash,
        }:
            return None
        payload = self.load(identity)
        if (
            payload["material_ref"] != ref
            or payload["content_hash"] != item["content_hash"]
            or payload["kind"] != item["media_type"]
        ):
            raise ContextError("input_superseded", "来源快照与当前资料不一致。")
        version = PROCESSOR_VERSION if payload["kind"] == "video" else "original_image_pages_v1"
        if payload["processor_version"] != version:
            return None
        # load() verifies originals. Verify audio and frames one at a time, not full lists of byte arrays.
        for segment in payload["audio"]:
            self._read_blob(ref, segment["blob"])
        for frame in payload["frames"]:
            self._read_blob(ref, frame["blob"])
        current = self.store.get(ref)
        if (
            current.get("prepared_input") != identity
            or current["content_hash"] != item["content_hash"]
            or current.get("source_input_binding") != binding
            or current.get("source_asset_hash") != source_asset_hash
        ):
            raise ContextError("version_changed", "来源媒体在核对期间变化，未复用旧快照。")
        return identity

    def bind_source(self, ref: str, identity: str, source_asset_hash: str, content_hash: str) -> None:
        payload = self.load(identity)
        if payload["material_ref"] != ref or payload["content_hash"] != content_hash:
            raise ContextError("input_superseded", "不能将其他资料或旧正文快照绑定到当前来源。")

        def change(state):
            item = state["items"].get(ref)
            if (
                not item
                or item["excluded"]
                or item.get("prepared_input") != identity
                or item["content_hash"] != content_hash
                or item.get("source_asset_hash") != source_asset_hash
            ):
                raise ContextError("version_changed", "来源媒体在准备期间变化，未绑定过期输入。")
            item["source_input_binding"] = {
                "input_id": identity,
                "content_hash": content_hash,
                "source_asset_hash": source_asset_hash,
            }

        self.store.transact(change)

    def audio(self, payload: dict[str, Any]) -> list[AudioSegment]:
        return [
            AudioSegment(
                segment["evidence_id"],
                segment["start_seconds"],
                segment["end_seconds"],
                segment["decoded_seconds"],
                self._read_blob(payload["material_ref"], segment["blob"]),
                segment["blob"]["sha256"],
                segment["overlaps_previous"],
            )
            for segment in payload["audio"]
        ]

    def frames(self, payload: dict[str, Any]) -> list[PreparedFrame]:
        return [
            PreparedFrame(
                FrameCandidate(**{**frame["candidate"], "reasons": tuple(frame["candidate"]["reasons"])}),
                self._read_blob(payload["material_ref"], frame["blob"]),
                frame["blob"]["sha256"],
                frame["blob"]["mime_type"],
                frame["page_index"],
            )
            for frame in payload["frames"]
        ]

    def prepare_video(
        self,
        ref: str,
        data: bytes,
        *,
        mime_type: str = "video/mp4",
        policy: MediaPolicy | None = None,
        expected_content_hash: str | None = None,
    ) -> str:
        item = self.store.get(ref)
        if expected_content_hash is not None and item["content_hash"] != expected_content_hash:
            raise ContextError("version_changed", "准备前原资料已变化，未登记旧媒体。")
        with LocalMedia(data, policy=policy) as media:
            segments = list(media.audio_segments())
            candidates, coverage = media.scan_frames()
            frames: list[PreparedFrame] = []
            media.frames(candidates, frames.append)
            return self.save(
                ref,
                content_hash=item["content_hash"],
                originals=[(data, mime_type)],
                audio=segments,
                frames=frames,
                coverage={**coverage, "has_audio": media.info.has_audio, "media_kind": "video"},
                processor_version=PROCESSOR_VERSION,
                strategy_hash=media.strategy_hash,
                kind="video",
            )

    def prepare_images(
        self, ref: str, images: list[tuple[bytes, str]], *, expected_content_hash: str | None = None
    ) -> str:
        item = self.store.get(ref)
        if expected_content_hash is not None and item["content_hash"] != expected_content_hash:
            raise ContextError("version_changed", "准备前原资料已变化，未登记旧图片。")
        frames = [
            PreparedFrame(
                FrameCandidate(f"f_{index:06}", index, 0, ("image_page",), 0),
                data,
                hashlib.sha256(data).hexdigest(),
                mime,
                index,
            )
            for index, (data, mime) in enumerate(images)
        ]
        return self.save(
            ref,
            content_hash=item["content_hash"],
            originals=images,
            audio=[],
            frames=frames,
            coverage={
                "complete": False,
                "media_kind": "image",
                "has_audio": False,
                "ordered_pages": len(images),
                "accuracy": "not_verified",
                "selection": "all_original_pages",
            },
            processor_version="original_image_pages_v1",
            strategy_hash=digest(["all_original_pages_v1", len(images)]),
            kind="image",
        )
