"""Generate ORIGINAL synthetic pages and run the real CPU adapter. Not a video recall benchmark."""

from __future__ import annotations

import argparse
import io
import json
import platform
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from collection_context.infrastructure.ocr import CpuOcr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--font", required=True, type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    ocr = CpuOcr(args.models)
    initialized = time.monotonic() - started
    pages = []
    expected = ["390", "844", "React", "Tailwind", "禁止额外文字"]
    for size in (40, 28, 20):
        for background, foreground in (("white", "black"), ("#111827", "#f8fafc")):
            image = Image.new("RGB", (900, 650), background)
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(str(args.font), size)
            lines = [
                "原创 OCR 测试页",
                "画布 390 x 844",
                "React + Tailwind",
                "逐页功能与构图参数",
                "禁止额外文字",
            ]
            for i, line in enumerate(lines):
                draw.text((36, 36 + i * 85), line, font=font, fill=foreground)
            output = io.BytesIO()
            image.save(output, format="PNG")
            result = ocr.recognize(output.getvalue())
            missing = [token for token in expected if token not in result.text]
            pages.append(
                {
                    "font_size": size,
                    "background": background,
                    "state": result.state,
                    "missing_tokens": missing,
                    "elapsed_seconds": round(result.elapsed_seconds, 3),
                    "text": result.text,
                }
            )
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "engine_version": ocr.engine_version,
                "providers": ["CPUExecutionProvider"],
                "model_hashes": ocr.model_hashes,
                "initialization_seconds": round(initialized, 3),
                "pages": pages,
                "all_expected_tokens_found": all(not p["missing_tokens"] for p in pages),
                "scope": "six original synthetic still pages; not real-video recall or Windows/Linux validation",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
