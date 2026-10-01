"""Real CPU OCR from a freshly installed fixed weight component; original pages only.

This developer source test does not prove frozen packaging or three-platform use.
No existing library, account, credentials, model API or implicit network access.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from collection_context.application.runtime_setup import CATALOG
from collection_context.infrastructure.runtime_dependencies import _absolute
from collection_context.infrastructure.runtime_installation import RuntimeInstaller
from collection_context.infrastructure.runtime_ocr import load_runtime_ocr
from collection_context.infrastructure.runtime_ocr_layout import OCR_ID


def run(package: Path, output: Path, font: Path) -> dict:
    output = _absolute(output)
    repository = Path(__file__).resolve().parents[3]
    if output.exists() or output == repository or repository in output.parents:
        raise ValueError("OCR verification needs a new repository-external stage")
    if not font.is_file() or font.is_symlink():
        raise ValueError("An explicit ordinary developer fixture font is required")
    network_attempts = []

    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            network_attempts.append(event)
            raise RuntimeError("No network allowed in local OCR verification")

    sys.addaudithook(audit)
    output.mkdir(mode=0o700)
    runtime, library = output / "runtime", output / "absent-library"
    report = {
        "state": "failed",
        "scope": "source_installed_weight_component_real_cpu_ocr_original_pages_only",
        "frozen_package_verified": False,
        "three_platforms_verified": False,
        "platform_requests": 0,
        "model_requests": 0,
    }
    try:
        installer = RuntimeInstaller(
            runtime, library_dir=library, catalog=CATALOG, _archive_source=lambda _: package
        )
        report["installation"] = installer.install(OCR_ID, installation_confirmed=True)
        before = (runtime / "runtime-dependencies.json").read_bytes()
        started = time.monotonic()
        engine = load_runtime_ocr(runtime, library_dir=library)
        report["initialization_seconds"] = round(time.monotonic() - started, 3)
        report["engine_version"] = engine.engine_version
        report["model_hashes"] = engine.model_hashes
        expected = ["390", "844", "React", "Tailwind", "禁止额外文字"]
        pages = []
        for size in (40, 28, 20):
            for background, foreground in (("white", "black"), ("#111827", "#f8fafc")):
                image = Image.new("RGB", (900, 650), background)
                draw = ImageDraw.Draw(image)
                face = ImageFont.truetype(str(font), size)
                for index, text in enumerate(
                    [
                        "原创 OCR 测试页",
                        "画布 390 x 844",
                        "React + Tailwind",
                        "逐页功能与构图参数",
                        "禁止额外文字",
                    ]
                ):
                    draw.text((36, 36 + index * 85), text, font=face, fill=foreground)
                data = io.BytesIO()
                image.save(data, format="PNG")
                result = engine.recognize(data.getvalue())
                pages.append(
                    {
                        "font_size": size,
                        "background": background,
                        "state": result.state,
                        "elapsed_seconds": round(result.elapsed_seconds, 3),
                        "missing_tokens": [token for token in expected if token not in result.text],
                        "text": result.text,
                    }
                )
        report["pages"] = pages
        report["installation_receipt_unchanged"] = (
            before == (runtime / "runtime-dependencies.json").read_bytes()
        )
        report["library_created"] = library.exists()
        report["network_attempts"] = len(network_attempts)
        report["system"] = platform.system()
        report["architecture"] = platform.machine()
        if (
            all(not p["missing_tokens"] and p["state"] == "ready" for p in pages)
            and report["installation_receipt_unchanged"]
            and not library.exists()
            and not network_attempts
        ):
            report["state"] = "passed"
        return report
    finally:
        report["network_attempts"] = len(network_attempts)
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.package, args.output, args.font)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
