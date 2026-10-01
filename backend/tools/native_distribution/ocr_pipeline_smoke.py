"""Ten original videos / fifty scheduled pages through frozen prepare-video.

This is a reproducible authored fixture baseline, not an independent human
annotation or real-platform quality signoff. Developer PIL/font create fixtures;
the frozen product alone installs its fixed components and prepares videos.
No existing library/runtime, source login, credentials, cloud request or service.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import resource
import subprocess
import time
from contextlib import closing
from pathlib import Path

from media_runtime_smoke import fixture_helpers
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs

CASES = (
    ("light", 32, (900, 650), "white", "black"),
    ("dark", 32, (900, 650), "#111827", "#f8fafc"),
    ("small_code", 20, (900, 650), "white", "black"),
    ("low_contrast", 28, (900, 650), "#d0d0d0", "#808080"),
    ("portrait", 28, (650, 900), "white", "black"),
    ("mixed_language", 28, (900, 650), "white", "black"),
    ("scrolling_layout", 24, (900, 650), "white", "black"),
    ("subtitle_changes", 28, (900, 650), "white", "black"),
    ("short_pages", 28, (900, 650), "white", "black"),
    ("small_parameter_change", 20, (900, 650), "white", "black"),
)


def assess_pages(labels: list[dict], directory: Path, records: list[dict], images: dict[str, bytes]) -> tuple:
    """Match full pixels to authored pages independently of OCR and nominal timestamps.

    Lossy encoding can shift first-visible frames relative to the authored schedule.
    A timestamp inside a page interval does not prove that page's pixels were kept.
    Ambiguous or distant matches are not counted as correct pages.
    """
    originals = []
    for label in labels:
        with Image.open(directory / label["image"]) as image:
            originals.append(image.convert("RGB"))
    matched = []
    for record in records:
        identity = record["evidence_id"] if record["selected"] else record["duplicate_of"]
        with Image.open(io.BytesIO(images[identity])) as image:
            decoded = image.convert("RGB")
        distances = [
            sum(ImageStat.Stat(ImageChops.difference(decoded, original)).mean) / (3 * 255)
            if decoded.size == original.size
            else 1
            for original in originals
        ]
        order = sorted(range(len(distances)), key=distances.__getitem__)
        best, second = order[:2]
        margin = distances[second] - distances[best]
        matched.append(
            {
                **record,
                "authored_page_index": labels[best]["index"]
                if distances[best] <= 0.01 and margin > 0
                else None,
                "normalized_pixel_distance": distances[best],
                "runner_up_margin": margin,
            }
        )
    assessed = []
    for label in labels:
        candidates = [p for p in matched if p["authored_page_index"] == label["index"]]
        selected = [p for p in candidates if p["selected"]]
        tokens = [p for p in selected if any(label["expected_token"] in line["text"] for line in p["lines"])]
        assessed.append(
            {
                **label,
                "candidate_seen": bool(candidates),
                "selected": bool(selected),
                "ocr_expected_token_found": bool(tokens),
                "selected_evidence_ids": [p["evidence_id"] for p in selected],
            }
        )
    return assessed, [
        {
            k: p[k]
            for k in (
                "evidence_id",
                "nominal_seconds",
                "authored_page_index",
                "normalized_pixel_distance",
                "runner_up_margin",
            )
        }
        for p in matched
    ]


def fixture_pages(stage: Path, case: tuple, font: Path) -> list[dict]:
    name, size, dimensions, background, foreground = case
    directory = stage / name
    directory.mkdir(mode=0o700)
    face = ImageFont.truetype(str(font), size)
    durations = [16, 2, 2, 2, 2] if name != "short_pages" else [16.1, 0.2, 0.2, 5.5, 2]
    start = 0.0
    labels = []
    for index, duration in enumerate(durations):
        image = Image.new("RGB", dimensions, background)
        draw = ImageDraw.Draw(image)
        token = str(390 + index)
        if name == "small_parameter_change":
            lines = ("原创代码页", "const width = " + token + ";", "React + Tailwind", "禁止额外文字")
        else:
            lines = (
                "原创测试页",
                "画布 " + token + " x 844",
                "React + Tailwind",
                "提示词按顺序编写",
                "禁止额外文字",
            )
        offset = 20 * index if name == "scrolling_layout" else 0
        for row, text in enumerate(lines):
            draw.text((36, 36 + row * 75 - offset), text, font=face, fill=foreground)
        if name == "subtitle_changes":
            draw.text((36, dimensions[1] - 60), "字幕变化 " + str(index), font=face, fill=foreground)
        image_path = directory / f"page-{index}.png"
        image.save(image_path, format="PNG")
        labels.append(
            {
                "index": index,
                "start_seconds": round(start, 3),
                "end_seconds": round(start + duration, 3),
                "expected_token": token,
                "critical_prompt_or_code": True,
                "image": image_path.name,
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "authored_text": list(lines),
            }
        )
        start += duration
    concat = directory / "frames.ffconcat"
    concat.write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file 'page-{p['index']}.png'\nduration {p['end_seconds'] - p['start_seconds']:.3f}\n"
            for p in labels
        )
        + "file 'page-4.png'\n",
        encoding="utf-8",
    )
    (directory / "labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
    return labels


def exercise(binary: Path, output: Path, font: Path) -> dict:
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or any(p.is_symlink() for p in (binary, *binary.parents))
    ):
        raise ValueError("Expected an explicit ordinary frozen executable")
    if not font.is_absolute() or not font.is_file() or font.is_symlink():
        raise ValueError("Expected an explicit local developer fixture font")
    helper = fixture_helpers()
    stage = helper.checked_stage(output)
    home, runtime, library = stage / "fresh-home", stage / "new-runtime", stage / "new-library"
    home.mkdir(mode=0o700)
    environment = {"PATH": "", "HOME": str(home), "LANG": "C", "LC_ALL": "C"}
    common = ["--workspace", str(library), "--runtime-dir", str(runtime)]
    report = {
        "state": "failed",
        "verification_scope": "frozen_authored_video_preparation_only",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "fresh_home": True,
        "empty_path": True,
        "product_uses_host_python_or_node": False,
        "annotation_kind": "authored_schedule_not_independent_human_review",
        "model_requests": 0,
        "platform_requests": 0,
        "public_release_authorized": False,
        "process_tree_peak_memory_measured": False,
        "cases": [],
    }
    started = time.monotonic()
    try:
        options = helper.run(binary, [*common, "runtime-options"], stage=stage, environment=environment)
        for prefix in ("ffmpeg-", "ocr-ppocrv6-"):
            item = next(p for p in options["artifacts"] if p["id"].startswith(prefix))
            assert item["state"] == "available" and item["download_bytes"] == 0
            helper.run(
                binary,
                [*common, "install-runtime", "--artifact", item["id"], "--confirm-install"],
                stage=stage,
                environment=environment,
                timeout=120,
            )
        receipt = runtime / "runtime-dependencies.json"
        before = hashlib.sha256(receipt.read_bytes()).hexdigest()
        ffmpeg = RuntimeDependencies(runtime, library_dir=library).resolve("ffmpeg").path
        helper.run(binary, [*common, "init"], stage=stage, environment=environment)
        for case_index, case in enumerate(CASES):
            name = case[0]
            labels = fixture_pages(stage, case, font)
            directory, video = stage / name, stage / name / "original.mp4"
            subprocess.run(
                [
                    str(ffmpeg),
                    "-v",
                    "error",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    "concat",
                    "-safe",
                    "1",
                    "-i",
                    "frames.ffconcat",
                    "-t",
                    "24",
                    "-an",
                    "-r",
                    "10",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "2",
                    "-threads",
                    "1",
                    str(video),
                ],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=30,
            )
            with closing(LibraryStore(library)) as store:
                item = store.upsert(
                    {
                        "native_id": str(120_000 + case_index),
                        "title": "原创 " + name,
                        "body": "仅供本机采样/OCR诊断，不是真实平台收藏",
                    },
                    kind="saved",
                    scope_id="s_original_fixture",
                )["item"]
            began = time.monotonic()
            prepared = helper.run(
                binary,
                [*common, "prepare-video", "--ref", item["id"], "--input", str(video)],
                stage=stage,
                environment=environment,
                timeout=120,
            )
            with closing(LibraryStore(library)) as store:
                registry = PreparedInputs(store)
                manifest = registry.load(prepared["input_id"])
                frames = manifest["frames"]
                records = manifest["coverage"]["ocr_records"]
                assert manifest["originals"][0]["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
                image_data = {
                    p["candidate"]["evidence_id"]: registry._read_blob(item["id"], p["blob"]) for p in frames
                }
                assert not manifest["audio"] and manifest["coverage"]["has_audio"] is False
                assert not store.snapshot()["jobs"] and not store.snapshot().get("model_profiles")
            assessed, matches = assess_pages(labels, directory, records, image_data)
            result = {
                "case": name,
                "input_id": prepared["input_id"],
                "labels": assessed,
                "assessment": "full_pixels_independent_of_ocr_and_nominal_timestamp",
                "pixel_matches": matches,
                "source_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "elapsed_seconds": round(time.monotonic() - began, 3),
                "coverage": manifest["coverage"],
                "cloud_frame_requests_if_executed": len(frames),
                "cloud_requests_executed": 0,
            }
            (directory / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["cases"].append(result)
            (stage / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "case": name,
                        "selected_pages": sum(p["selected"] for p in assessed),
                        "ocr_tokens_found": sum(p["ocr_expected_token_found"] for p in assessed),
                        "frames": len(frames),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        labels = [p for case in report["cases"] for p in case["labels"]]
        report.update(
            state="passed" if all(p["selected"] for p in labels) else "quality_gate_failed",
            total_videos=len(CASES),
            labelled_pages=len(labels),
            selected_pages=sum(p["selected"] for p in labels),
            sampling_omissions=[
                {"case": c["case"], "index": p["index"]}
                for c in report["cases"]
                for p in c["labels"]
                if not p["candidate_seen"]
            ],
            ocr_selection_omissions=[
                {"case": c["case"], "index": p["index"]}
                for c in report["cases"]
                for p in c["labels"]
                if p["candidate_seen"] and not p["selected"]
            ],
            selected_page_recall=sum(p["selected"] for p in labels) / len(labels),
            receipt_unchanged=hashlib.sha256(receipt.read_bytes()).hexdigest() == before,
        )
        assert report["receipt_unchanged"]
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["max_observed_child_rss_host_units"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def reassess(binary: Path, source: Path, output: Path) -> dict:
    """Read the owned immutable fixture/manifests; never overwrite earlier reports."""
    if not source.is_absolute() or any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError("Expected an explicit ordinary original test stage")
    source_report = source / "report.json"
    report = json.loads(source_report.read_bytes())
    if (
        report["verification_scope"] != "frozen_authored_video_preparation_only"
        or report["model_requests"] != 0
        or report["platform_requests"] != 0
        or report["binary_sha256"] != hashlib.sha256(binary.read_bytes()).hexdigest()
    ):
        raise ValueError("Original fixture/binary identity does not match")
    stage = fixture_helpers().checked_stage(output)
    started = time.monotonic()
    with closing(LibraryStore(source / "new-library")) as store:
        registry = PreparedInputs(store)
        for case in report["cases"]:
            if case["case"] not in {entry[0] for entry in CASES}:
                raise ValueError("Unexpected original case")
            directory = source / case["case"]
            labels = json.loads((directory / "labels.json").read_bytes())
            manifest = registry.load(case["input_id"])
            images = {
                p["candidate"]["evidence_id"]: registry._read_blob(manifest["material_ref"], p["blob"])
                for p in manifest["frames"]
            }
            assessed, matches = assess_pages(labels, directory, manifest["coverage"]["ocr_records"], images)
            case.update(
                labels=assessed,
                pixel_matches=matches,
                assessment="full_pixels_independent_of_ocr_and_nominal_timestamp",
            )
    labels = [p for case in report["cases"] for p in case["labels"]]
    report.update(
        state="passed" if all(p["selected"] for p in labels) else "quality_gate_failed",
        selected_pages=sum(p["selected"] for p in labels),
        sampling_omissions=[
            {"case": c["case"], "index": p["index"]}
            for c in report["cases"]
            for p in c["labels"]
            if not p["candidate_seen"]
        ],
        ocr_selection_omissions=[
            {"case": c["case"], "index": p["index"]}
            for c in report["cases"]
            for p in c["labels"]
            if p["candidate_seen"] and not p["selected"]
        ],
        selected_page_recall=sum(p["selected"] for p in labels) / len(labels),
        source_stage=str(source),
        source_report_sha256=hashlib.sha256(source_report.read_bytes()).hexdigest(),
        reassessment_seconds=round(time.monotonic() - started, 3),
        original_report_unchanged=True,
        product_commands_reexecuted=0,
    )
    (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    parser.add_argument(
        "--reassess-stage", type=Path, help="Read-only pixel assessment of an owned prior stage"
    )
    args = parser.parse_args()
    if args.reassess_stage is not None:
        result = reassess(args.binary, args.reassess_stage, args.output)
    else:
        if args.font is None:
            parser.error("An explicit developer fixture font is required to create videos")
        result = exercise(args.binary, args.output, args.font)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
