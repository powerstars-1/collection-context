"""Installed polling process + real loopback HTTP + signals; no provider quality claim."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

import collection_context
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.scheduling import ProcessingSchedule


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-installed", action="store_true")
    parser.add_argument(
        "--legacy-python", type=Path, help="Optional explicit earlier v1 candidate; synthetic library only"
    )
    args = parser.parse_args()
    module = Path(collection_context.__file__).resolve()
    if args.require_installed and not module.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Run with the installed candidate, outside the repository")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="worker-smoke-", dir=args.output.absolute()))
    entered, release = threading.Event(), threading.Event()
    requests: list[dict] = []
    server_errors: list[str] = []
    gated = {"value": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if (
                    self.path != "/v1/chat/completions"
                    or self.headers.get("Authorization") != "Bearer fixture-only"
                ):
                    raise ValueError("Unexpected fixture request")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 4_000_000:
                    raise ValueError("Fixture size invalid")
                payload = json.loads(self.rfile.read(size))
                requests.append({"model": payload["model"], "endpoint": self.path})
                if gated["value"]:
                    gated["value"] = False
                    entered.set()
                    if not release.wait(20):
                        raise TimeoutError("Fixture request gate")
                response = json.dumps(
                    {
                        "model": payload["model"] + "-synthetic",
                        "choices": [
                            {
                                "message": {"content": "原创合成协议结果[f_000000]，不是真实模型识别。"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"total_tokens": 7},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected after forced client-process death, not a successful client result.
            except Exception as error:
                server_errors.append(type(error).__name__)
                self.send_error(400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = LibraryStore.initialize(root / "原创合成库")
    secrets = FileSecrets.initialize(root / "独立合成凭据")
    children: list[subprocess.Popen] = []
    try:
        key = secrets.put("fixture-only")
        for role in ("vision", "summary"):
            ModelCatalog(store).configure(
                role=role,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="fixture-" + role,
                credential_ref=key,
                timeout=20,
            )
        workflow = ExtractionWorkflow(store, secrets.get)
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "#eeeeee").save(image, format="PNG")

        def prepare(native_id: str):
            item = store.upsert(
                {"native_id": native_id, "media_type": "image", "title": "原创后台协议样例"},
                kind="saved",
                scope_id="s_saved",
            )["item"]
            identity = PreparedInputs(store).prepare_images(item["id"], [(image.getvalue(), "image/png")])
            return identity

        def submit(native_id: str, *, suffix="first", max_calls=2):
            return workflow.submit(
                prepare(native_id), idempotency_key=native_id + suffix, max_calls=max_calls
            )

        command = [
            sys.executable,
            "-m",
            "collection_context.cli",
            "--workspace",
            str(store.files.root),
            "worker",
            "--credential-dir",
            str(secrets.files.root),
            "--allow-model-calls",
            "--poll-seconds",
            "0.1",
        ]
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}

        def start(name):
            path = root / (name + ".jsonl")
            with path.open("w") as log:
                child = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.DEVNULL)
            children.append(child)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if '"worker_started"' in path.read_text():
                    return child, path
                if child.poll() is not None:
                    raise RuntimeError("Worker exited before ready")
                time.sleep(0.05)
            raise TimeoutError("Worker startup")

        def once(name):
            completed = subprocess.run(
                command + ["--once"], cwd=root, env=env, capture_output=True, text=True, timeout=15
            )
            (root / (name + ".jsonl")).write_text(completed.stdout)
            assert completed.returncode == 0

        def cli(name, *arguments):
            completed = subprocess.run(
                command[:5] + list(arguments), cwd=root, env=env, capture_output=True, text=True, timeout=15
            )
            (root / (name + ".jsonl")).write_text(completed.stdout)
            assert completed.returncode == 0
            return json.loads(completed.stdout)["data"]

        first, log = start("graceful")
        # New tasks committed by a different process/store while the worker is already polling.
        job = submit("1001")
        assert entered.wait(10)
        second = submit("1001", suffix="reuse", max_calls=0)
        competing = subprocess.run(
            command + ["--once"], cwd=root, env=env, capture_output=True, text=True, timeout=10
        )
        assert competing.returncode == 1 and '"worker_busy"' in competing.stdout
        (root / "competing.jsonl").write_text(competing.stdout)
        first.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        assert first.poll() is None  # An already-dispatched request is not falsely reported as withdrawn.
        release.set()
        assert first.wait(15) == 0
        assert workflow.executor.jobs.get(job["id"])["state"] == "succeeded"
        assert workflow.executor.jobs.get(second["id"])["state"] == "queued"
        assert len(requests) == 2
        events = [json.loads(line)["data"] for line in log.read_text().splitlines()]
        assert any(e.get("event") == "worker_stopped" for e in events)
        once("restart-reuse")
        assert workflow.executor.jobs.get(second["id"])["state"] == "succeeded"
        assert workflow.executor.jobs.get(second["id"])["calls"] == [] and len(requests) == 2
        entered.clear()
        release.clear()
        gated["value"] = True
        crashed = submit("1002")
        child, _ = start("forced-exit")
        assert entered.wait(10)
        child.kill()
        assert child.wait(10) < 0
        release.set()
        before = len(requests)
        once("restart-unknown")
        blocked = workflow.executor.jobs.get(crashed["id"])
        assert blocked["state"] == "blocked" and blocked["error"]["code"] == "upstream_outcome_unknown"
        assert len(requests) == before == 3 and not server_errors
        # Policy commands run in separate installed processes; the worker discovers new items itself.
        ModelCatalog(store).configure(
            role="audio",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="fixture-audio",
            credential_ref=key,
            protocol="chat_audio",
            timeout=20,
        )
        historical_input = prepare("1003")
        cli(
            "auto-enable",
            "configure-auto",
            "--enabled",
            "yes",
            "--allow-model-calls",
            "--max-calls",
            "2",
            "--max-new-tasks",
            "2",
        )
        for native in ("1004", "1005", "1006"):
            prepare(native)
        once("auto-first-round")
        automatic_jobs = [
            j
            for j in store.snapshot()["jobs"].values()
            if j["payload"].get("dispatch", {}).get("mode") == "automatic"
        ]
        assert len(automatic_jobs) == 2
        paused_job = next(j for j in automatic_jobs if j["state"] == "queued")
        cli("auto-disable", "configure-auto", "--enabled", "no")
        calls_before_pause = len(requests)
        once("auto-paused-round")
        assert len(requests) == calls_before_pause == 5
        batch = cli(
            "history-submit",
            "submit-history",
            "--input-id",
            historical_input,
            "--idempotency-key",
            "installed-selected-history",
            "--max-calls",
            "2",
            "--allow-model-calls",
        )
        assert batch["max_calls_total"] == 2
        once("history-while-auto-paused")
        assert workflow.executor.jobs.get(batch["job_ids"][0])["state"] == "succeeded"
        assert len(requests) == 7
        for role in ("vision", "summary"):
            ModelCatalog(store).configure(
                role=role,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="fixture-updated-" + role,
                credential_ref=key,
                timeout=20,
            )
        cli("auto-reenable", "configure-auto", "--enabled", "yes", "--allow-model-calls", "--max-calls", "2")
        once("old-backlog-still-paused")
        assert len(requests) == 7
        preview = cli("selected-backlog-preview", "resume-auto", "--job-id", paused_job["id"])
        assert preview["count"] == 1 and preview["max_calls"] == 2
        cli(
            "selected-backlog-resume",
            "resume-auto",
            "--job-id",
            paused_job["id"],
            "--preview-token",
            preview["preview_token"],
            "--allow-model-calls",
        )
        once("selected-backlog-executed")
        assert workflow.executor.jobs.get(paused_job["id"])["state"] == "succeeded"
        assert len(requests) == 9
        assert [r["model"] for r in requests[-2:]] == ["fixture-vision", "fixture-summary"]
        assert not server_errors
        legacy_checked = False
        legacy_upgraded = False
        if args.legacy_python is not None:
            # An idle older runner legitimately writes nothing. Give it a genuinely paused
            # automatic job so the check covers the unsafe operation, not process startup.
            prepare("1007")
            pending_for_legacy = ProcessingSchedule(workflow).admit_new()
            assert len(pending_for_legacy) == 1
            cli("auto-pause-before-older-runner", "configure-auto", "--enabled", "no")
            # The older runner lacks dispatch-policy checks. Its writer must fail before any fee request.
            old_command = [str(args.legacy_python.absolute()), *command[1:], "--once"]
            old = subprocess.run(old_command, cwd=root, env=env, capture_output=True, text=True, timeout=15)
            (root / "older-v1-runner-denied.jsonl").write_text(old.stdout)
            assert old.returncode == 1 and '"writer_upgrade_required"' in old.stdout
            assert len(requests) == 9
            assert workflow.executor.jobs.get(pending_for_legacy[0])["state"] == "queued"
            legacy_checked = True
            legacy_root = root / "older-v1-original-library"
            old_init = subprocess.run(
                [
                    str(args.legacy_python.absolute()),
                    "-m",
                    "collection_context.cli",
                    "--workspace",
                    str(legacy_root),
                    "init",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert old_init.returncode == 0
            old_seed = subprocess.run(
                [
                    str(args.legacy_python.absolute()),
                    "-c",
                    "import sys; from pathlib import Path; from collection_context.library.store import LibraryStore; "
                    "s=LibraryStore(Path(sys.argv[1])); "
                    "s.upsert({'native_id':'2001','title':'原创旧版升级样例'},kind='saved',scope_id='s_saved'); s.close()",
                    str(legacy_root),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert old_seed.returncode == 0
            old_library = LibraryStore(legacy_root)
            try:
                old_snapshot = old_library.snapshot()
            finally:
                old_library.close()
            upgrade = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "collection_context.cli",
                    "--workspace",
                    str(legacy_root),
                    "upgrade-writer",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            (root / "older-v1-explicit-upgrade.jsonl").write_text(upgrade.stdout)
            assert (
                upgrade.returncode == 0
                and json.loads(upgrade.stdout)["data"]["writer_protocol"] == "os_writer_v2"
            )
            upgraded_library = LibraryStore(legacy_root)
            try:
                assert upgraded_library.snapshot() == old_snapshot
            finally:
                upgraded_library.close()
            assert len(requests) == 9
            legacy_upgraded = True
        report = {
            "passed": True,
            "installed_module": str(module),
            "python": sys.version,
            "worker_polling_accepts_later_submission": True,
            "competing_worker_rejected": True,
            "sigterm_finishes_current_not_next": True,
            "restart_reuses_confirmed_result_without_requests": True,
            "sigkill_unknown_result_blocks_without_retry": True,
            "automatic_ignores_preexisting_and_enforces_total_cap": True,
            "automatic_pause_does_not_block_selected_history": True,
            "reenable_does_not_implicitly_resume_backlog": True,
            "selected_backlog_preview_resume_keeps_original_models": True,
            "older_v1_runner_cannot_write_or_dispatch": legacy_checked,
            "older_v1_library_explicitly_upgraded_without_data_changes": legacy_upgraded,
            "loopback_requests": requests,
            "actual_cloud_requests": 0,
            "actual_platform_requests": 0,
            "production_or_private_data_used": False,
            "quality_claim": "Synthetic loopback protocol responses only, not model recognition quality",
        }
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"report": str(root / "report.json"), "passed": True}, ensure_ascii=False))
    finally:
        release.set()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(10)
        secrets.close()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
