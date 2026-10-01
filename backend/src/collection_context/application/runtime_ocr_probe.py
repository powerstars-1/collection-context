"""Bounded offline CPU OCR probe using six explicitly supplied original test pages."""

from __future__ import annotations

import hashlib
import io
import sys
import time
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.runtime_dependencies import _absolute
from collection_context.infrastructure.runtime_ocr import load_runtime_ocr

PAGE_NAMES = tuple(f"page-{size}-{tone}.png" for size in (40, 28, 20) for tone in ("light", "dark"))
EXPECTED = ("390", "844", "React", "Tailwind", "禁止额外文字")


def probe_ocr_runtime(
    runtime_dir: Path, *, library_dir: Path, probe_dir: Path, image_dir: Path
) -> dict[str, Any]:
    runtime, library, output, source = map(_absolute, (runtime_dir, library_dir, probe_dir, image_dir))
    directories = (runtime, library, output, source)
    for index, first in enumerate(directories):
        for second in directories[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ContextError("runtime_probe_unsafe", "OCR探测输入、输出、运行目录及资料库须完全分开。")
    if output.exists():
        raise ContextError("runtime_probe_output_exists", "OCR探测须使用不存在的新目录，不覆盖已有文件。")
    try:
        from PIL import Image
    except ImportError:
        raise ContextError("ocr_runtime_unavailable", "OCR图片运行库缺失，未创建探测结果。") from None
    images = {}
    with SafeFiles(source) as files:
        for name in PAGE_NAMES:
            data = files.read(name, max_bytes=1_000_000)
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != "PNG" or image.size != (900, 650):
                        raise ValueError
                    image.verify()
            except Exception:
                raise ContextError(
                    "ocr_probe_fixture_invalid", "须提供六张固定900×650原创PNG测试页。"
                ) from None
            images[name] = data
    output.mkdir(mode=0o700)
    report: dict[str, Any] = {
        "state": "failed",
        "verification_scope": "explicit_original_pages_local_cpu_ocr_only",
        "content_quality_verified": False,
        "three_platforms_verified": False,
        "platform_requests": 0,
        "model_requests": 0,
        "network_attempts": 0,
    }
    active = True

    def audit(event: str, args: tuple) -> None:
        if active and event in {"socket.connect", "socket.getaddrinfo"}:
            report["network_attempts"] += 1
            raise ContextError("ocr_probe_network_blocked", "本机OCR探测禁止联网。")

    sys.addaudithook(audit)
    with SafeFiles(output) as files:
        try:
            with SafeFiles(runtime) as runtime_files:
                before = runtime_files.read("runtime-dependencies.json", max_bytes=262_144)
            started = time.monotonic()
            engine = load_runtime_ocr(runtime, library_dir=library)
            report["initialization_seconds"] = round(time.monotonic() - started, 3)
            report["engine_version"] = engine.engine_version
            report["model_hashes"] = engine.model_hashes
            pages = []
            for name, data in images.items():
                result = engine.recognize(data)
                pages.append(
                    {
                        "name": name,
                        "input_sha256": hashlib.sha256(data).hexdigest(),
                        "state": result.state,
                        "text": result.text,
                        "elapsed_seconds": round(result.elapsed_seconds, 3),
                        "missing_tokens": [token for token in EXPECTED if token not in result.text],
                    }
                )
            report["pages"] = pages
            with SafeFiles(runtime) as runtime_files:
                unchanged = before == runtime_files.read("runtime-dependencies.json", max_bytes=262_144)
            if not unchanged or not all(p["state"] == "ready" and not p["missing_tokens"] for p in pages):
                raise ContextError("ocr_probe_failed", "OCR固定页未完整命中或收据改变，未标为通过。")
            report.update(state="verified", functional_verified=True, receipt_unchanged=True)
        except ContextError as error:
            report["error_code"] = error.code
            raise
        finally:
            active = False
            files.write("report.json", canonical_bytes(report))
    return report
