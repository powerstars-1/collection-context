"""Local OCR-assisted frame selection. Never removes distinct visuals based on text alone."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, replace
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.media import MediaPolicy, PreparedFrame
from collection_context.infrastructure.ocr import CpuOcr, OcrResult

OCR_SELECTION_VERSION = "ocr_pixel_selection_v1"


class OcrFrameSelection:
    def __init__(self, engine: CpuOcr, policy: MediaPolicy, *, max_bytes: int):
        self.engine, self.policy, self.max_bytes = engine, policy, max_bytes
        self.frames: list[PreparedFrame] = []
        self.records: list[dict[str, Any]] = []
        self.retained_bytes = 0
        self.previous_pixels: str | None = None
        self.previous_result: OcrResult | None = None
        self.previous_selected: str | None = None
        self.calls = 0

    def feed(self, frame: PreparedFrame) -> None:
        if len(self.records) >= self.policy.max_ocr_frames:
            raise ContextError("ocr_candidate_limit", "本地OCR候选数量超出上限，未静默删页。")
        if hashlib.sha256(frame.data).hexdigest() != frame.sha256:
            raise ContextError("media_changed", "OCR画面输入哈希已改变。")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(frame.data)) as image:
                if image.width * image.height > self.policy.max_pixels or not image.width or not image.height:
                    raise ValueError
                pixels = hashlib.sha256(
                    str(image.size).encode() + image.convert("RGBA").tobytes()
                ).hexdigest()
        except Exception:
            raise ContextError("ocr_image_invalid", "本地OCR候选图片无法安全解码，未当作空文字。") from None
        reused = pixels == self.previous_pixels and self.previous_result is not None
        error_code = None
        try:
            if reused:
                result = self.previous_result
                assert result is not None
            else:
                self.calls += 1
                result = self.engine.recognize(frame.data)
            if (
                result.state not in {"ready", "no_text"}
                or len(result.lines) > 500
                or len(result.text) > 20_000
            ):
                raise ContextError("ocr_result_limit", "本地OCR结果异常或超过登记上限，未截断后标成功。")
        except ContextError as error:
            error_code = error.code
            result = None
        anchor = (
            bool(set(frame.candidate.reasons) & {"first", "last", "coverage_anchor"})
            or not self.frames
            or frame.candidate.nominal_seconds - self.frames[-1].candidate.nominal_seconds
            >= self.policy.anchor_seconds
        )
        # Equal OCR strings can hide changed diagrams, code glyphs, or images.
        # Only identical full RGBA pixels and a valid local result allow dedup.
        duplicate = bool(reused and result is not None and not anchor)
        reason = (
            "ocr_pixel_duplicate"
            if duplicate
            else "ocr_error"
            if error_code
            else "ocr_no_text"
            if result and result.state == "no_text"
            else "ocr_text"
        )
        if not duplicate:
            if len(self.frames) >= self.policy.max_selected_frames:
                raise ContextError("frame_limit", "OCR选页仍超过云端画面上限；请分段，未无声删页。")
            if self.retained_bytes + len(frame.data) > self.max_bytes:
                raise ContextError("media_input_limit", "OCR保留画面超过本批字节上限，未静默裁剪。")
            frame = replace(
                frame, candidate=replace(frame.candidate, reasons=(*frame.candidate.reasons, reason))
            )
            self.frames.append(frame)
            self.retained_bytes += len(frame.data)
            self.previous_selected = frame.candidate.evidence_id
        self.records.append(
            {
                "evidence_id": frame.candidate.evidence_id,
                "nominal_seconds": frame.candidate.nominal_seconds,
                "image_sha256": frame.sha256,
                "pixel_sha256": pixels,
                "selected": not duplicate,
                "duplicate_of": self.previous_selected if duplicate else None,
                "reason": reason,
                "state": "failed" if error_code else result.state if result else "failed",
                "error_code": error_code,
                "ocr_reused": bool(reused and result),
                "lines": [asdict(line) for line in result.lines] if result else [],
            }
        )
        self.previous_pixels, self.previous_result = pixels, result

    def coverage(self) -> dict[str, Any]:
        failures = [p["evidence_id"] for p in self.records if p["state"] == "failed"]
        return {
            "ocr_state": "not_applied" if not self.records else "partial" if failures else "ready",
            "ocr_selection_version": OCR_SELECTION_VERSION,
            "ocr_engine_version": self.engine.engine_version,
            "ocr_model_hashes": self.engine.model_hashes,
            "ocr_candidates": len(self.records),
            "ocr_calls": self.calls,
            "ocr_failures": failures,
            "ocr_duplicate_frames": len(self.records) - len(self.frames),
            "selected_frames": len(self.frames),
            "ocr_records": self.records,
            "complete": False,
            "near_duplicate_filter": "not_enabled_without_quality_validation",
        }
