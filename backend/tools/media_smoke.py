"""Original synthetic video preparation/CPU OCR benchmark, never private media or cloud ASR."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import collection_context
from collection_context.infrastructure.media import PROCESSOR_VERSION, LocalMedia, MediaPolicy
from collection_context.infrastructure.ocr import CpuOcr


def normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def make_video(root: Path, case: int, font_path: Path) -> tuple[Path, list[dict], bool]:
    """All source images, code, prompts and audio tones are generated here."""
    folder = root / f"case-{case:02}"
    folder.mkdir()
    size = (540, 900) if case == 1 else (900, 540)
    font_size = 20 if case == 2 else 28
    font = ImageFont.truetype(str(font_path), font_size)
    background, foreground = ("#151b26", "#f5f6f8") if case == 4 else ("#faf9f6", "#222222")
    if case == 3:
        background, foreground = "#eeeeee", "#999999"
    manifest, concat = [], []
    cursor = 0.0
    if case == 8:
        intro = Image.new("RGB", size, background)
        ImageDraw.Draw(intro).text((30, 40), "原创讲解前段：后面的提示词需保留", font=font, fill=foreground)
        intro.save(folder / "intro.png")
        concat.extend(["file 'intro.png'", "duration 20.0"])
        cursor = 20.0
    for page in range(5):
        code = str(701001 + case * 100 + page)
        duration = 0.6 if case == 7 else 1.5
        image = Image.new("RGB", size, background)
        draw = ImageDraw.Draw(image)
        draw.text((30, 25), "原创合成教程：非已收藏作品", font=font, fill=foreground)
        y = 340 if case == 5 else (85 + page * 20 if case == 6 else 100)
        lines = [f"页面编号 {code}", "React + Tailwind", "画布 390 x 844", "禁止额外文字"]
        if case == 2:
            lines = [
                f"// page {code}",
                "const width = 390;",
                "const height = 844;",
                'export const tool = "React";',
            ]
        for offset, line in enumerate(lines):
            draw.text((35, y + offset * (font_size + 12)), line, font=font, fill=foreground)
        if case == 9:
            draw.text((30, size[1] - 55), f"字幕变化 {page + 1}：保持页面正文", font=font, fill=foreground)
        filename = f"page-{page}.png"
        image.save(folder / filename)
        concat.extend([f"file '{filename}'", f"duration {duration}"])
        manifest.append({"page": page, "code": code, "start": cursor, "end": cursor + duration})
        cursor += duration
    concat.append("file 'page-4.png'")
    source_list = folder / "fixture.txt"
    source_list.write_text("\n".join(concat) + "\n", encoding="utf-8")
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("Install an explicit FFmpeg dependency before this smoke")
    output = folder / "original.mp4"
    command = [
        executable,
        "-v",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        str(source_list),
    ]
    has_audio = case in {1, 4}
    if has_audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"])
    command.extend(["-t", str(cursor), "-r", "20", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"])
    command.extend(["-c:a", "aac"] if has_audio else ["-an"])
    command.append(str(output))
    subprocess.run(command, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output, manifest, has_audio


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    installed = Path(collection_context.__file__).resolve()
    if args.require_installed and not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Smoke must use the installed package from outside the repository")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="media-smoke-", dir=args.output.absolute()))
    started = time.monotonic()
    ocr = CpuOcr(args.models)
    initialization = time.monotonic() - started
    cases = []
    for case in range(10):
        source, expected, has_audio = make_video(root, case, args.font)
        case_started = time.monotonic()
        with LocalMedia(
            source.read_bytes(), policy=MediaPolicy(audio_segment_seconds=3, audio_overlap_seconds=0.25)
        ) as media:
            assert media.info.has_audio == has_audio
            segments = []
            for segment in media.audio_segments():
                output = source.parent / (segment.evidence_id + ".wav")
                output.write_bytes(segment.data)
                segments.append(
                    {
                        "id": segment.evidence_id,
                        "start": segment.start_seconds,
                        "end": segment.end_seconds,
                        "decoded_seconds": segment.decoded_seconds,
                        "sha256": segment.sha256,
                        "bytes": len(segment.data),
                        "overlaps_previous": segment.overlaps_previous,
                    }
                )
            candidates, coverage = media.scan_frames()
            evidence = []

            def observe(frame):
                candidate = frame.candidate
                output = source.parent / (candidate.evidence_id + ".png")
                output.write_bytes(frame.data)
                recognized = ocr.recognize(frame.data)
                matches = [page["code"] for page in expected if page["code"] in normalized(recognized.text)]
                evidence.append(
                    {
                        "candidate": dataclasses.asdict(candidate),
                        "frame_sha256": frame.sha256,
                        "recognized_codes": matches,
                        "ocr_seconds": recognized.elapsed_seconds,
                        "ocr_text": recognized.text,
                        "ocr_state": recognized.state,
                    }
                )

            media.frames(candidates, observe)
            sampled_pages = [
                page["code"]
                for page in expected
                if any(
                    page["start"] <= index / media.policy.sample_fps < page["end"]
                    for index in range(coverage["sampled_frames"])
                )
            ]
            selected_pages = [
                page["code"]
                for page in expected
                if any(page["start"] <= candidate.nominal_seconds < page["end"] for candidate in candidates)
            ]
            recognized_codes = {code for value in evidence for code in value["recognized_codes"]}
            missing = [page["code"] for page in expected if page["code"] not in recognized_codes]
            cases.append(
                {
                    "case": case,
                    "source": str(source),
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "media": dataclasses.asdict(media.info),
                    "decoder_versions": media.decoder_versions,
                    "audio_state": "prepared" if has_audio else "not_applicable",
                    "segments": segments,
                    "coverage": coverage,
                    "expected": expected,
                    "sampled_pages": sampled_pages,
                    "selected_pages": selected_pages,
                    "missing_recognized_codes": missing,
                    "evidence": evidence,
                    "elapsed_seconds": time.monotonic() - case_started,
                }
            )
        print(
            json.dumps(
                {"case": case, "selected_frames": len(candidates), "missing_codes": missing},
                ensure_ascii=False,
            ),
            flush=True,
        )
    sample_misses = sum(5 - len(case["sampled_pages"]) for case in cases)
    selection_misses = sum(5 - len(case["selected_pages"]) for case in cases)
    ocr_misses = sum(len(case["missing_recognized_codes"]) for case in cases)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "scope": "10 original synthetic videos / 50 manually defined pages; no private sync, no cloud or ASR quality test",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": PROCESSOR_VERSION,
        "module": str(installed),
        "ocr_version": ocr.engine_version,
        "model_hashes": ocr.model_hashes,
        "initialization_seconds": initialization,
        "elapsed_seconds": time.monotonic() - started,
        "sample_misses": sample_misses,
        "selection_misses": selection_misses,
        "ocr_code_misses": ocr_misses,
        "selected_frames": sum(case["coverage"]["selected_frames"] for case in cases),
        "peak_parent_rss_bytes": peak if platform.system() == "Darwin" else peak * 1024,
        "peak_scope": "Python/OCR parent process lifetime high-water mark; not combined decoder memory",
        "cloud_requests": 0,
        "cases": cases,
        "limitations": [
            "Selected candidates are not refined using OCR yet.",
            "Fixtures do not validate arbitrary scrolling, animations, flash frames or cloud transcription accuracy.",
            "Small fixed test coverage is not a promise of no omissions in user videos.",
            "Sample grid and frame seeks use nominal times, not exact presentation timestamps.",
        ],
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(root / "report.json"),
                "sample_misses": sample_misses,
                "selection_misses": selection_misses,
                "ocr_code_misses": ocr_misses,
                "cloud_requests": 0,
            }
        )
    )
    return 0 if sample_misses == selection_misses == ocr_misses == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
