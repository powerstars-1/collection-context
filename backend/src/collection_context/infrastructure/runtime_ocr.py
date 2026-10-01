"""Fixed CPU OCR weights loaded from verified, non-executable runtime snapshots.

Installation and loading are different authorities. This module never downloads,
loads an account, executes a model file, or substitutes an engine/weight version.
"""

from __future__ import annotations

import hashlib
import tempfile
from importlib import metadata
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ocr import CpuOcr
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies

OCR_VERSIONS = {"rapidocr": "3.9.2", "onnxruntime": "1.30.0"}
OCR_BUILD = "rapidocr-3.9.2-ppocrv6-small-1"
OCR_SOURCE = "https://pypi.org/project/rapidocr/3.9.2/"
OCR_MODELS = {
    "ocr_det": (
        "PP-OCRv6_det_small.onnx",
        9_929_594,
        "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    ),
    "ocr_cls": (
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        585_532,
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    ),
    "ocr_rec": (
        "PP-OCRv6_rec_small.onnx",
        21_234_383,
        "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    ),
}


def ocr_engine_state() -> str:
    for name, expected in OCR_VERSIONS.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            return "ocr_runtime_missing"
        if actual != expected:
            return "ocr_runtime_version_mismatch"
    return "available_not_functionally_verified"


def load_runtime_ocr(runtime_dir: Path, *, library_dir: Path) -> CpuOcr:
    if ocr_engine_state() != "available_not_functionally_verified":
        raise ContextError("ocr_runtime_unavailable", "固定CPU OCR运行库缺失或版本不符；未替换引擎。")
    registry = RuntimeDependencies(runtime_dir, library_dir=library_dir)
    receipt = registry._receipt()
    weights = {}
    for role, (filename, size, digest) in OCR_MODELS.items():
        tool = registry.resolve(role)
        if (
            tool.path.name != filename
            or tool.bytes != size
            or tool.sha256 != digest
            or tool.version != OCR_VERSIONS["rapidocr"]
            or tool.build_version != OCR_BUILD
            or tool.source_url != OCR_SOURCE
        ):
            raise ContextError("ocr_weight_identity", "OCR权重不属于已验证固定组合；未加载或下载。")
        body = registry.read_model(role)
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise ContextError("ocr_weight_identity", "OCR权重读取快照不符；未加载或下载。")
        weights[filename] = body
    # An installation change cannot mix two generations in one OCR engine.
    if registry._receipt() != receipt:
        raise ContextError("ocr_installation_changed", "读取期间组件版本已改变，请重新检查；未初始化。")
    # The explicit runtime is already checked disjoint from the library. An OS
    # default temp root might itself be the user's chosen library directory.
    with tempfile.TemporaryDirectory(prefix=".ocr-snapshot-", dir=runtime_dir) as directory:
        root = Path(directory)
        with SafeFiles(root) as files:
            for filename, body in weights.items():
                files.write(filename, body)
        # ONNX sessions eagerly consume their files. Temporary copies are removed
        # after initialization; the returned engine retains its in-memory sessions.
        return CpuOcr(root)
