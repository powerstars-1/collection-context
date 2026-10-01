"""Explicit paid acceptance of original fixtures; never reads a private material library."""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, build_opener

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.infrastructure.media import AudioSegment, FrameCandidate, PreparedFrame
from collection_context.library.store import LibraryStore
from collection_context.processing.models import CloudModelClient, ModelProfile, SameOriginRedirect
from collection_context.processing.stages import audio_stage, publish_stage, summary_stage, vision_stage
from collection_context.workflows.executor import DurableExecutor

REQUEST_LIMIT = 3  # The fourth request remains unused; no fallback or automatic retries.
IMAGE_LINES = (
    "原创界面参数测试（非私人收藏）",
    "第一步：创建 390 × 844 画布",
    "第二步：React + Tailwind CSS",
    "主色 #2563EB；圆角 16px；间距 24px",
    "四个维度：品牌、用户、功能、视觉",
    "提示词：保留导航，禁止额外文字。",
)
SPOKEN = "这是原创语音验收。第一步创建三百九十乘八百四十四的画布。第二步使用 React 和 Tailwind。四个维度分别是品牌、用户、功能和视觉。图片主色是蓝色，圆角十六像素，间距二十四像素。不要自动发布，也不要把收藏直接当成用户观点。"


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def export_text_outputs(root: Path, report_name: str, prefix: str) -> list[str]:
    """Export recorded results only; no credential lookup, model access or recomputation."""
    report = json.loads((root / report_name).read_text(encoding="utf-8"))
    exported = []
    for stage, descriptor in report["stages"].items():
        if stage not in {"audio_a_000000", "screen_f_000000", "summary"}:
            continue
        relative = Path(descriptor["result"]["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unexpected recorded result path")
        result = json.loads((root / "原创云验收库" / relative).read_text(encoding="utf-8"))
        destination = root / f"{prefix}-{stage}.md"
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(result["output"]["text"])
        destination.chmod(0o600)
        exported.append(str(destination))
    return exported


class CountedTransport:
    def __init__(
        self,
        root: Path,
        opener,
        *,
        api_key: str,
        limit: int = REQUEST_LIMIT,
        existing_records: list[dict] | None = None,
    ):
        self.root, self.opener, self.key, self.limit = root, opener, api_key, limit
        self.records: list[dict] = list(existing_records or [])
        self.path = root / "requests.json"
        self.checkpoint()

    def checkpoint(self):
        save_json(self.path, {"limit": self.limit, "count": len(self.records), "requests": self.records})

    def __call__(self, request, *, timeout):
        if len(self.records) >= self.limit:
            raise ContextError("acceptance_budget_exhausted", "本轮请求上限已用完，未发请求。")
        payload = json.loads(request.data)
        types = [part["type"] for part in payload["messages"][0]["content"]]
        role = "audio" if "input_audio" in types else "vision" if "image_url" in types else "summary"
        record = {
            "number": len(self.records) + 1,
            "role": role,
            "state": "started",
            "model": payload["model"],
        }
        self.records.append(record)
        self.checkpoint()  # Persist intent BEFORE dispatch, so interruptions cannot hide spending.
        start = time.monotonic()
        try:
            response = self.opener.open(request, timeout=timeout)
            record.update(state="response_opened", http_status=response.status)
            self.checkpoint()
            return SecretCheckedResponse(response, self.key)
        except HTTPError as error:
            record.update(state="http_error", http_status=error.code)
            self.checkpoint()
            raise
        except Exception as error:
            record.update(state="outcome_unknown", error_type=type(error).__name__)
            self.checkpoint()
            raise
        finally:
            record["open_elapsed_seconds"] = round(time.monotonic() - start, 3)
            self.checkpoint()


class SecretCheckedResponse:
    """Do not let an upstream echo of the header secret reach stored stage results."""

    def __init__(self, response, key: str):
        self.response, self.key = response, key.encode()
        self.headers = response.headers

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.response.close()

    def read(self, size: int):
        body = self.response.read(size)
        if self.key in body:
            raise ContextError(
                "upstream_secret_echo", "上游响应包含秘密；未保存正文。", possibly_charged=True
            )
        return body


def make_fixtures(root: Path, font: Path) -> tuple[AudioSegment, PreparedFrame]:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1100, 690), "#FAFAFA")
    draw = ImageDraw.Draw(image)
    face = ImageFont.truetype(str(font), 36)
    for number, line in enumerate(IMAGE_LINES):
        draw.text((44, 44 + number * 95), line, fill="#18181B", font=face)
    image_path = root / "original-text.png"
    image.save(image_path)
    subprocess.run(
        ["/usr/bin/say", "-v", "Tingting", "-o", str(root / "original.aiff"), SPOKEN],
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(root / "original.aiff"),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(root / "original.wav"),
        ],
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import wave

    with wave.open(str(root / "original.wav")) as wav:
        duration = wav.getnframes() / wav.getframerate()
    audio, frame = (root / "original.wav").read_bytes(), image_path.read_bytes()
    save_json(
        root / "ground-truth.json",
        {
            "image_lines": IMAGE_LINES,
            "spoken": SPOKEN,
            "audio_seconds": duration,
            "fixture_origin": "new original text and local macOS Tingting synthesis; not human speech or private media",
        },
    )
    return (
        AudioSegment("a_000000", 0, duration, duration, audio, hashlib.sha256(audio).hexdigest(), False),
        PreparedFrame(
            FrameCandidate("f_000000", 0, 0, ("original_fixture",), 0),
            frame,
            hashlib.sha256(frame).hexdigest(),
            page_index=0,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font", required=True, type=Path)
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Explicit --allow-paid authorization is required; no request was sent")
    root = args.output.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    config_path = Path(__file__).resolve().parents[1] / "config/new_api_media.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not config.get("enabled") or config["base_url"] != "https://api.getwhite.cloud/v1":
        raise RuntimeError("Expected authorized existing media gateway; no fallback is allowed")
    key_result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", config["keychain_service"], "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = key_result.stdout.strip() if key_result.returncode == 0 else ""
    if not key:
        raise RuntimeError("Authorized Keychain credential unavailable; no fallback")
    audio, frame = make_fixtures(root, args.font)
    import certifi

    # Use a verified CA bundle for the FIRST attempt; never disable TLS or retry SSL failures.
    opener = build_opener(
        SameOriginRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
    )
    transport = CountedTransport(root, opener, api_key=key)
    parameters = {"thinking": {"type": "disabled"}, "max_completion_tokens": 1600}

    def client(protocol="chat"):
        return CloudModelClient(
            ModelProfile(config["base_url"], config["default_model"], key, protocol, 180, parameters),
            transport=transport,
        )

    store = LibraryStore.initialize(root / "原创云验收库")
    try:
        material = store.upsert(
            {
                "native_id": "990001",
                "title": "原创界面参数测试",
                "body": "独立原创截图与本机合成语音。不是私人收藏；仅验收本轮指定片段。",
                "media_type": "video",
            },
            kind="link",
            scope_id="s_original_cloud_test",
        )["item"]
        coverage = {
            "complete": False,
            "scope": "one original still page and one locally synthesized voice segment",
            "accuracy": "requires_ground_truth_comparison",
            "real_video_recall_verified": False,
        }
        audio_step, image_step = (
            audio_stage(material["id"], audio, client("chat_audio")),
            vision_stage(material["id"], frame, client()),
        )
        dependencies = (audio_step.name, image_step.name)
        stages = [
            audio_step,
            image_step,
            summary_stage(
                material["id"],
                {"title": material["title"], "body": material["body"]},
                dependencies,
                client(),
                source_coverage=coverage,
            ),
        ]
        stages.append(
            publish_stage(
                store,
                material["id"],
                material["content_hash"],
                tuple(stage.name for stage in stages),
                source_coverage=coverage,
            )
        )
        executor = DurableExecutor(store)
        job = executor.submit(stages, idempotency_key="explicit-original-cloud-once", max_calls=REQUEST_LIMIT)
        completed = executor.run(job["id"], stages)
        # No rerun: executor and transport both leave failures explicit rather than spending again.
        report = {
            "request_count": len(transport.records),
            "request_limit": REQUEST_LIMIT,
            "automatic_retries": 0,
            "provider_fallback": False,
            "job_state": completed["state"],
            "material_ref": material["id"],
            "calls": completed["calls"],
            "stages": completed["stages"],
            "status": ContextService(store).status(material["id"]),
            "scope": coverage,
            "human_and_multi_speaker_asr_verified": False,
            "full_mvp_verified": False,
        }
        encoded = json.dumps(report, ensure_ascii=False)
        if key in encoded:
            raise RuntimeError("Credential unexpectedly found in output; report withheld")
        save_json(root / "report.json", report)
        print(
            json.dumps(
                {
                    "root": str(root),
                    "request_count": len(transport.records),
                    "job_state": completed["state"],
                    "full_mvp_verified": False,
                }
            )
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
