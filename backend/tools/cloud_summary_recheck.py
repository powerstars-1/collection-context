"""One separately authorized fourth call; resume confirmed audio/vision, never dispatch them."""

import argparse
import hashlib
import json
import ssl
import subprocess
from pathlib import Path
from urllib.request import HTTPSHandler, build_opener

from cloud_model_acceptance import CountedTransport, save_json

from collection_context.application.contracts import digest
from collection_context.application.service import ContextService
from collection_context.infrastructure.media import AudioSegment, FrameCandidate, PreparedFrame
from collection_context.library.store import LibraryStore
from collection_context.processing.models import CloudModelClient, ModelProfile, SameOriginRedirect
from collection_context.processing.stages import audio_stage, publish_stage, summary_stage, vision_stage
from collection_context.workflows.executor import DurableExecutor


class SummaryOnlyTransport(CountedTransport):
    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        if [part["type"] for part in payload["messages"][0]["content"]] != ["text"]:
            raise RuntimeError("Audio or vision attempted during summary-only authorization; blocked")
        return super().__call__(request, timeout=timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid-fourth-only", action="store_true")
    parser.add_argument("--existing-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.allow_paid_fourth_only:
        parser.error("A fourth-call authorization is required")
    root = args.existing_root.absolute()
    report_path = root / "summary-v3-report.json"
    if report_path.exists():
        raise RuntimeError("Fourth call report already exists; no retry")
    previous = json.loads((root / "report.json").read_text(encoding="utf-8"))
    ledger = json.loads((root / "requests.json").read_text(encoding="utf-8"))
    if (
        ledger["count"] != 3
        or len(ledger["requests"]) != 3
        or any(
            record["state"] != "response_opened" or record["http_status"] != 200
            for record in ledger["requests"]
        )
    ):
        raise RuntimeError("Expected exactly three confirmed previous calls; stop")
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "config/new_api_media.json").read_text(encoding="utf-8")
    )
    if not config.get("enabled") or config["base_url"] != "https://api.getwhite.cloud/v1":
        raise RuntimeError("Authorized existing gateway required; no fallback")
    key_result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", config["keychain_service"], "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = key_result.stdout.strip() if key_result.returncode == 0 else ""
    if not key:
        raise RuntimeError("Authorized Keychain credential unavailable")
    import certifi

    opener = build_opener(
        SameOriginRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
    )
    # Original ledger is preserved before extending it. No API header/key is written to disk.
    save_json(root / "requests-before-summary-v3.json", ledger)
    transport = SummaryOnlyTransport(root, opener, api_key=key, limit=4, existing_records=ledger["requests"])

    def client(protocol="chat"):
        return CloudModelClient(
            ModelProfile(
                config["base_url"],
                config["default_model"],
                key,
                protocol,
                180,
                {"thinking": {"type": "disabled"}, "max_completion_tokens": 1600},
            ),
            transport=transport,
        )

    truth = json.loads((root / "ground-truth.json").read_text(encoding="utf-8"))
    audio_data, image_data = (root / "original.wav").read_bytes(), (root / "original-text.png").read_bytes()
    duration = truth["audio_seconds"]
    audio = AudioSegment(
        "a_000000", 0, duration, duration, audio_data, hashlib.sha256(audio_data).hexdigest(), False
    )
    image = PreparedFrame(
        FrameCandidate("f_000000", 0, 0, ("original_fixture",), 0),
        image_data,
        hashlib.sha256(image_data).hexdigest(),
        page_index=0,
    )
    store = LibraryStore(root / "原创云验收库")
    try:
        item = store.get(previous["material_ref"])
        audio_step = audio_stage(item["id"], audio, client("chat_audio"))
        image_step = vision_stage(item["id"], image, client())
        executor = DurableExecutor(store)
        for stage in (audio_step, image_step):
            signature = digest(
                [
                    stage.name,
                    digest({"declared_input": stage.input_hash, "dependencies": {}}),
                    stage.processor_version,
                ]
            )
            if executor.jobs.reusable_stage(signature, principal="local_owner") is None:
                raise RuntimeError("Confirmed audio/vision cache identity changed; no cloud call permitted")
        coverage = previous["scope"]
        stages = [
            audio_step,
            image_step,
            summary_stage(
                item["id"],
                {"title": item["title"], "body": item["body"]},
                (audio_step.name, image_step.name),
                client(),
                source_coverage=coverage,
            ),
        ]
        stages.append(
            publish_stage(
                store,
                item["id"],
                item["content_hash"],
                tuple(stage.name for stage in stages),
                source_coverage=coverage,
            )
        )
        job = executor.submit(stages, idempotency_key="explicit-summary-v3-fourth-once", max_calls=1)
        completed = executor.run(job["id"], stages)
        if (
            len(transport.records) != 4
            or len(completed["calls"]) != 1
            or completed["calls"][0]["stage"] != "summary"
        ):
            raise RuntimeError("Expected summary-only fourth call; inspect without retry")
        result = {
            "request_count": len(transport.records),
            "new_request_count": 1,
            "request_limit": 4,
            "automatic_retries": 0,
            "audio_and_vision_reused": True,
            "job_state": completed["state"],
            "material_ref": item["id"],
            "calls": completed["calls"],
            "stages": completed["stages"],
            "status": ContextService(store).status(item["id"]),
            "scope": coverage,
            "full_mvp_verified": False,
        }
        if key in json.dumps(result, ensure_ascii=False):
            raise RuntimeError("Credential unexpectedly in output; withheld")
        save_json(report_path, result)
        print(
            json.dumps(
                {
                    "root": str(root),
                    "request_count": 4,
                    "new_request_count": 1,
                    "job_state": completed["state"],
                    "audio_and_vision_reused": True,
                }
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
