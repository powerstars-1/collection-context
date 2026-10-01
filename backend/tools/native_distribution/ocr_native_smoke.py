"""Frozen OCR install and six original pages with fresh HOME and empty PATH.

Developer fixture creation uses an explicitly supplied local font, not a product
dependency or redistribution. Only the frozen executable installs/runs OCR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from media_runtime_smoke import fixture_helpers
from PIL import Image, ImageDraw, ImageFont


def exercise(binary: Path, output: Path, font: Path) -> dict:
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or any(p.is_symlink() for p in (binary, *binary.parents))
    ):
        raise ValueError("Expected an explicit ordinary frozen executable")
    if not font.is_absolute() or not font.is_file() or font.is_symlink():
        raise ValueError("An explicit ordinary developer fixture font is required")
    helper = fixture_helpers()
    stage = helper.checked_stage(output)
    home, pages = stage / "fresh-home", stage / "original-pages"
    home.mkdir(mode=0o700)
    pages.mkdir(mode=0o700)
    for size in (40, 28, 20):
        for tone, background, foreground in (("light", "white", "black"), ("dark", "#111827", "#f8fafc")):
            image = Image.new("RGB", (900, 650), background)
            draw = ImageDraw.Draw(image)
            face = ImageFont.truetype(str(font), size)
            for index, text in enumerate(
                (
                    "原创 OCR 测试页",
                    "画布 390 x 844",
                    "React + Tailwind",
                    "逐页功能与构图参数",
                    "禁止额外文字",
                )
            ):
                draw.text((36, 36 + index * 85), text, font=face, fill=foreground)
            image.save(pages / f"page-{size}-{tone}.png", format="PNG")
    environment = {"PATH": "", "HOME": str(home), "LANG": "C", "LC_ALL": "C"}
    runtime, library = stage / "new-runtime", stage / "absent-library"
    common = ["--workspace", str(library), "--runtime-dir", str(runtime)]
    report = {
        "state": "failed",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "fresh_home": True,
        "empty_path": True,
        "host_python_or_node_used_by_product": False,
        "public_release_authorized": False,
        "verification_scope": "frozen_original_pages_cpu_ocr_only",
    }
    try:
        options = helper.run(binary, [*common, "runtime-options"], stage=stage, environment=environment)
        item = next(p for p in options["artifacts"] if p["id"].startswith("ocr-ppocrv6-"))
        assert item["state"] == "available" and item["download_bytes"] == 0
        report["installation"] = helper.run(
            binary,
            [*common, "install-runtime", "--artifact", item["id"], "--confirm-install"],
            stage=stage,
            environment=environment,
            timeout=120,
        )
        receipt = runtime / "runtime-dependencies.json"
        before = hashlib.sha256(receipt.read_bytes()).hexdigest()
        probe = helper.run(
            binary,
            [*common, "probe-ocr-runtime", "--probe-dir", str(stage / "probe"), "--image-dir", str(pages)],
            stage=stage,
            environment=environment,
            timeout=120,
        )
        assert probe["state"] == "verified" and probe["functional_verified"] is True
        assert len(probe["pages"]) == 6 and all(not p["missing_tokens"] for p in probe["pages"])
        assert probe["network_attempts"] == probe["platform_requests"] == probe["model_requests"] == 0
        assert not library.exists() and before == hashlib.sha256(receipt.read_bytes()).hexdigest()
        report.update(state="passed", probe=probe, library_created=False, receipt_unchanged=True)
    finally:
        (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.binary, args.output, args.font), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
