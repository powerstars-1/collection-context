"""Installed original executor/media/HTTP smoke; synthetic responses are NOT model quality evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import collection_context
from collection_context.application.service import ContextService
from collection_context.infrastructure.media import LocalMedia, PreparedFrame
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    module = Path(collection_context.__file__).resolve()
    if args.require_installed and not module.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Use installed package, outside the repository")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="extraction-smoke-", dir=args.output.absolute()))
    image = Image.new("RGB", (540, 900), "#faf9f6")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(args.font), 28)
    for y, line in enumerate(
        ("原创合成教程", "React + Tailwind", "画布 390 x 844", "协议样例：不是真实收藏")
    ):
        draw.text((30, 100 + y * 60), line, fill="#222222", font=font)
    image.save(root / "original.png")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("An explicit FFmpeg installation is required")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-loop",
            "1",
            "-i",
            str(root / "original.png"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-r",
            "20",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(root / "original.mp4"),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with LocalMedia((root / "original.mp4").read_bytes()) as media:
        audio = list(media.audio_segments())
        candidates, coverage = media.scan_frames()
        frames: list[PreparedFrame] = []
        media.frames(candidates[:1], frames.append)
        assert len(audio) == 1 and len(frames) == 1
        coverage = {**coverage, "smoke_selected_only_first_frame": True, "complete": False}
    requests = []
    server_errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if (
                    self.path != "/v1/chat/completions"
                    or self.headers.get("Authorization") != "Bearer synthetic-fixture-key"
                ):
                    raise ValueError("invalid fixture request")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 4_000_000:
                    raise ValueError("fixture request size")
                request = json.loads(self.rfile.read(length))
                model = request["model"]
                content = request["messages"][0]["content"]
                types = [part["type"] for part in content]
                if model == "fixture-audio":
                    data = base64.b64decode(content[1]["input_audio"]["data"], validate=True)
                    with wave.open(io.BytesIO(data)) as wav:
                        assert wav.getframerate() == 16000 and wav.getnchannels() == 1
                    text = "[无可辨识讲话]（合成协议响应，不是语音识别结果）"
                elif model == "fixture-vision":
                    data = base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1], validate=True)
                    with Image.open(io.BytesIO(data)) as prepared:
                        assert prepared.size == (540, 900)
                        prepared.verify()
                    text = "React + Tailwind；画布 390 x 844。（合成协议响应，不是模型识别结果）"
                elif model == "fixture-summary":
                    assert "非可信来源资料" in content[0]["text"]
                    assert "390 x 844" in content[0]["text"]
                    text = "## 概要\n原创测试画布为390 x 844[f_000000]；"
                    text += (
                        "纯音不作讲话转写[a_000000]。"
                        if '"evidence_id": "a_000000"' in content[0]["text"]
                        else "两页原图按顺序保留[f_000001]，此条图文无音轨。"
                    )
                    text += "\n## 缺口\n仅测试协议，内容准确性未验收。"
                else:
                    raise ValueError("unsupported fixture model")
                requests.append({"model": model, "endpoint": self.path, "content_types": types})
                response = json.dumps(
                    {
                        "model": model + "-synthetic-returned",
                        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 2,
                            "total_tokens": 12,
                            "prompt_tokens_details": {"cached_tokens": 3},
                        },
                    },
                    ensure_ascii=False,
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("x-request-id", "synthetic-" + str(len(requests)))
                self.end_headers()
                self.wfile.write(response)
            except Exception as error:
                server_errors.append(type(error).__name__)
                self.send_error(400, "Fixture validation failed")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = LibraryStore.initialize(root / "原创合成资料库")
    secrets = FileSecrets.initialize(root / "独立服务凭据")
    try:
        material = store.upsert(
            {"native_id": "901", "title": "原创合成 UI 协议测试", "body": "非真实收藏，纯音和静态画布"},
            kind="saved",
            scope_id="s_saved",
        )["item"]

        credential_ref = secrets.put("synthetic-fixture-key")
        models = ModelCatalog(store)

        def configure(role, name, protocol="chat"):
            return models.configure(
                role=role,
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                model=name,
                credential_ref=credential_ref,
                protocol=protocol,
                timeout=10,
            )

        configure("audio", "fixture-audio", "chat_audio")
        configure("vision", "fixture-vision")
        configure("summary", "fixture-summary")
        inputs = PreparedInputs(store)
        input_id = inputs.save(
            material["id"],
            content_hash=material["content_hash"],
            originals=[((root / "original.mp4").read_bytes(), "video/mp4")],
            audio=audio,
            frames=frames,
            coverage=coverage,
            processor_version="smoke_first_frame_v1",
            strategy_hash=coverage["strategy_hash"],
            kind="video",
        )
        workflow = ExtractionWorkflow(store, secrets.get)
        submitted = workflow.submit(input_id, idempotency_key="first", max_calls=3)
        assert len(requests) == 0
        # Defaults really change while a persisted task is queued; the new process MUST use old snapshots.
        configure("vision", "wrong-new-default-must-not-be-called")
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        interrupt_script = """
import os, sys
from pathlib import Path
from collection_context.library.store import LibraryStore
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.workflows.extraction import ExtractionWorkflow
store = LibraryStore(Path(sys.argv[1]))
secrets = FileSecrets(Path(sys.argv[2]))
workflow = ExtractionWorkflow(store, secrets.get)
# Exit only AFTER the paid response/result/usage was atomically confirmed, BEFORE stage commit.
workflow.executor.jobs.commit_stage_result = lambda *args, **kwargs: os._exit(7)
workflow.run(sys.argv[3])
"""
        interrupted = subprocess.run(
            [
                sys.executable,
                "-c",
                interrupt_script,
                str(store.files.root),
                str(secrets.files.root),
                submitted["id"],
            ],
            cwd=root,
            env=environment,
            timeout=30,
            capture_output=True,
        )
        assert interrupted.returncode == 7 and len(requests) == 1
        interrupted_state = workflow.executor.jobs.get(submitted["id"])
        assert (
            interrupted_state["state"] == "running" and interrupted_state["calls"][0]["state"] == "completed"
        )

        def run_new_process(job_id):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "collection_context.cli",
                    "--workspace",
                    str(store.files.root),
                    "run-job",
                    "--job-id",
                    job_id,
                    "--credential-dir",
                    str(secrets.files.root),
                ],
                cwd=root,
                env=environment,
                timeout=30,
                capture_output=True,
                text=True,
            )
            if completed.returncode:
                raise RuntimeError("Isolated job CLI failed; no private inputs echoed")
            assert "synthetic-fixture-key" not in completed.stdout
            return json.loads(completed.stdout)["data"]

        first = run_new_process(submitted["id"])
        assert first["state"] == "succeeded" and len(requests) == 3 and not server_errors
        assert all(call["state"] == "completed" for call in first["calls"])
        for call in first["calls"]:
            result = workflow.executor.jobs.read_result(call["result"])
            assert result["actual_model"].endswith("-synthetic-returned")
            assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
        service = ContextService(store)
        assert service.search("390")["items"][0]["material_ref"] == material["id"]
        screen = service.read(material["id"], artifact="screen")
        assert "[f_000000]" in screen["text"] and screen["warnings"]
        configure("vision", "fixture-vision")
        second = run_new_process(workflow.submit(input_id, idempotency_key="reuse", max_calls=0)["id"])
        assert second["state"] == "succeeded" and second["calls"] == [] and len(requests) == 3
        assert not server_errors
        image_material = store.upsert(
            {"native_id": "902", "title": "原创合成图文协议测试", "media_type": "image"},
            kind="saved",
            scope_id="s_saved",
        )["item"]
        image_prepared = subprocess.run(
            [
                sys.executable,
                "-m",
                "collection_context.cli",
                "--workspace",
                str(store.files.root),
                "prepare-images",
                "--ref",
                image_material["id"],
                "--image",
                str(root / "original.png"),
                "--image",
                str(root / "original.png"),
            ],
            cwd=root,
            env=environment,
            timeout=30,
            capture_output=True,
            text=True,
        )
        assert image_prepared.returncode == 0 and len(requests) == 3
        image_input = json.loads(image_prepared.stdout)["data"]["input_id"]
        image_job = run_new_process(
            workflow.submit(image_input, idempotency_key="image-first", max_calls=3)["id"]
        )
        assert image_job["state"] == "succeeded" and len(requests) == 6 and not server_errors
        image_text = service.read(image_material["id"], artifact="screen")["text"]
        assert "原图第1页" in image_text and "原图第2页" in image_text and "名义采样时间" not in image_text
        assert service.status(image_material["id"])["artifacts"]["audio"]["state"] == "not_applicable"
        report = {
            "scope": "Original synthetic video + actual local HTTP; not cloud/model-quality or full MVP acceptance",
            "module": str(module),
            "library": str(store.files.root),
            "material_ref": material["id"],
            "source_sha256": hashlib.sha256((root / "original.mp4").read_bytes()).hexdigest(),
            "first_job_state": first["state"],
            "reuse_job_state": second["state"],
            "local_http_requests": requests,
            "real_cloud_requests": 0,
            "reuse_new_requests": 0,
            "fresh_execution_processes": 4,
            "interrupted_after_confirmed_audio_resumed_without_duplicate": True,
            "queued_default_changed_but_pinned_profile_kept": True,
            "input_id": input_id,
            "image_input_id": image_input,
            "image_job_state": image_job["state"],
            "image_original_pages": 2,
            "image_audio_state": "not_applicable",
            "credential_backend": "private synthetic service files outside library, not encrypted",
            "server_errors": server_errors,
            "coverage": coverage,
            "screen_warnings": screen["warnings"],
            "artifacts": store.get(material["id"])["artifacts"],
            "actual_usage_source": "synthetic fixture fields; not real billing",
        }
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        secrets.close()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    print(
        json.dumps(
            {
                "report": str(root / "report.json"),
                "local_http_requests": len(requests),
                "real_cloud_requests": 0,
                "reuse_new_requests": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
