"""Portable CPU OCR adapter; explicit local models, no implicit downloads or Mac OCR."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError

MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: list[list[float]]


@dataclass(frozen=True)
class OcrResult:
    lines: list[OcrLine]
    elapsed_seconds: float
    state: str
    engine_version: str

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class CpuOcr:
    def __init__(self, model_dir: Path | None = None, *, engine: Any = None):
        self.model_hashes: dict[str, str] = {}
        if engine is not None:
            self.engine, self.engine_version = engine, "injected-test-engine"
            return
        if model_dir is None:
            raise ContextError("ocr_models_required", "请先安装并核对本地 OCR 模型；不自动下载。")
        paths = {name: model_dir / filename for name, filename in MODEL_FILES.items()}
        if any(not p.is_file() or p.is_symlink() for p in paths.values()):
            raise ContextError("ocr_models_required", "OCR 模型不完整；未将缺失引擎视为空文字成功。")
        try:
            from rapidocr import RapidOCR

            self.engine_version = version("rapidocr")
            params: dict[str, Any] = {
                "Global.log_level": "error",
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "EngineConfig.onnxruntime.use_cuda": False,
                "EngineConfig.onnxruntime.use_coreml": False,
                "EngineConfig.onnxruntime.use_dml": False,
                "EngineConfig.onnxruntime.use_cann": False,
            }
            for name, path in paths.items():
                params[f"{name}.model_path"] = str(path.absolute())
                self.model_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.engine = RapidOCR(params=params)
            for component in (self.engine.text_det, self.engine.text_cls, self.engine.text_rec):
                if component.session.session.get_providers() != ["CPUExecutionProvider"]:
                    raise ContextError("ocr_provider_mismatch", "本基线只验收 CPU 推理，未自动改用 GPU。")
        except ImportError:
            raise ContextError("ocr_unavailable", "未安装跨平台 OCR 依赖；请通过依赖检查修复。") from None
        except ContextError:
            raise
        except Exception:
            raise ContextError("ocr_initialization_failed", "OCR 初始化失败，请核对模型与运行库。") from None

    def recognize(self, image: bytes) -> OcrResult:
        if not isinstance(image, bytes) or not image or len(image) > 32_000_000:
            raise ContextError("ocr_input_limit", "OCR 图片为空或过大。")
        started = time.monotonic()
        try:
            result = self.engine(image)
            if result.txts is None and result.boxes is None and result.scores is None:
                return OcrResult([], time.monotonic() - started, "no_text", self.engine_version)
            if result.txts is None or result.boxes is None or result.scores is None:
                raise ValueError
            if not len(result.txts) == len(result.boxes) == len(result.scores):
                raise ValueError
            lines = []
            for text, box, score in zip(result.txts, result.boxes, result.scores):
                confidence = float(score)
                points = [[float(x), float(y)] for x, y in box]
                if (
                    not isinstance(text, str)
                    or len(points) != 4
                    or not 0 <= confidence <= 1
                    or not math.isfinite(confidence)
                    or any(not math.isfinite(n) for p in points for n in p)
                ):
                    raise ValueError
                lines.append(OcrLine(text, confidence, points))
            return OcrResult(
                lines, time.monotonic() - started, "ready" if lines else "no_text", self.engine_version
            )
        except ContextError:
            raise
        except Exception:
            raise ContextError("ocr_failed", "本页 OCR 失败，已记录缺口，未当作空文字成功。") from None
