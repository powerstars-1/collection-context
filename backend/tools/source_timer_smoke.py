"""Installed source timer + real processes/browser; original intercepted responses, no real login."""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from synchronization_smoke import ACCOUNT_PAYLOAD, child, original_row

import collection_context
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.account import self_account
from collection_context.sources.douyin import normalize_item
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.source_schedule import SourceSchedule, moment
from collection_context.workflows.synchronization import SynchronizationWorkflow


def cli(root: Path, *arguments: str, python: str | None = None, success: bool = True) -> dict:
    result = subprocess.run(
        [python or sys.executable, "-m", "collection_context.cli", "--workspace", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
    )
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value["ok"] is success and (result.returncode == 0) is success
    return value


def media_reuse(run: Path) -> dict:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("Explicit FFmpeg is required for the real retained-media proof")
    video = run / "原创两秒.mp4"
    subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=20",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with closing(LibraryStore.initialize(run / "原创媒体复用库")) as store:
        downloads = []
        prepares = []
        original = PreparedInputs.prepare_video

        def downloaded(observed):
            downloads.append("original_fixture_bytes")
            return [(video.read_bytes(), "video/mp4")]

        def prepared(self, *arguments, **kwargs):
            prepares.append("real_ffmpeg_prepare")
            return original(self, *arguments, **kwargs)

        PreparedInputs.prepare_video = prepared
        try:
            observed = normalize_item(original_row("91"))
            ingest = IngestionWorkflow(store, SimpleNamespace(), SimpleNamespace(fetch=downloaded))
            first = ingest.import_item(observed, kind="saved", scope_id="s_saved", download=True)
            second = ingest.import_item(observed, kind="liked", scope_id="s_liked", download=True)
            assert first["download"] == second["download"] == "ready"
            assert not first["download_reused"] and second["download_reused"]
            assert first["input_id"] == second["input_id"] and len(downloads) == len(prepares) == 1
            payload = PreparedInputs(store).load(first["input_id"])
            assert (
                payload["audio"]
                and payload["frames"]
                and len(store.get(first["material_ref"])["relations"]) == 2
            )
            assert not store.snapshot()["jobs"]
            return {
                "original_seconds": 2,
                "download_fixture_count": len(downloads),
                "real_media_prepare_count": len(prepares),
                "audio_segments": len(payload["audio"]),
                "frames": len(payload["frames"]),
                "retained_input_reused": True,
            }
        finally:
            PreparedInputs.prepare_video = original


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--legacy-python", type=Path)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--interrupt", action="store_true")
    args = parser.parse_args()
    if args.child:
        return child(args.child, interrupt=args.interrupt)
    if args.output is None or args.legacy_python is None:
        parser.error("--output and --legacy-python required")
    if os.name != "posix" or "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Installed POSIX proof required; not Windows or real platform acceptance")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="source-timer-", dir=args.output.absolute()))
    root = run / "原创同步库"
    store = LibraryStore.initialize(root)
    configs = []
    account = self_account(ACCOUNT_PAYLOAD).public()["account_ref"]
    for kind in ("liked", "saved", "collection", "creator"):
        options = (
            ["--creator-url", "https://www.douyin.com/user/MS4w-synthetic"]
            if kind == "creator"
            else ["--account-ref", account]
        )
        if kind == "collection":
            options += ["--collection-id", "9"]
        config = cli(root, "configure-sync", "--kind", kind, "--limit", "3", *options)["data"]["config_id"]
        configs.append(config)
        cli(root, "configure-auto-sync", "--config-id", config, "--enabled", "yes", "--allow-source-sync")
    assert not store.snapshot()["jobs"]
    command = [sys.executable, str(Path(__file__).absolute()), "--child", str(run)]
    process = subprocess.Popen(
        [*command, "--interrupt"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        reached = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([process.stdout], [], [], 0.1)[0]:
                if process.stdout.readline().strip() == "fixture_checkpoint_gate":
                    reached = True
                    break
        if not reached:
            raise RuntimeError("Timer did not reach committed-work gate")
        before = store.snapshot()
        jobs = list(before["jobs"])
        interrupted = next(ref for ref in jobs if before["jobs"][ref]["state"] == "running")
        assert len(jobs) == 4 and len(before["items"]) == 1
        assert len(before["jobs"][interrupted]["stages"]["source_sync"]["result"]["imported"]) == 1
        for config in configs:
            cli(root, "configure-auto-sync", "--config-id", config, "--enabled", "no")
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        paused_process = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
        paused = json.loads(paused_process.stdout.strip().splitlines()[-1])
        assert paused["result"]["handled"] == 0 and not paused["observed"]
        assert all(store.snapshot()["jobs"][ref]["state"] == "queued" for ref in jobs)

        previous = store.snapshot()
        legacy = cli(
            root,
            "worker",
            "--allow-source-sync",
            "--browser-dir",
            str(run / "独立浏览器"),
            "--once",
            python=str(args.legacy_python),
            success=False,
        )
        assert legacy["error"]["code"] == "invalid_dispatch_policy"
        assert (
            store.snapshot() == previous
        )  # Older fixed-sync binary refuses before touching browser or jobs.
        for config in configs:
            cli(root, "configure-auto-sync", "--config-id", config, "--enabled", "yes", "--allow-source-sync")
        resumed_process = subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
        resumed = json.loads(resumed_process.stdout.strip().splitlines()[-1])
        assert resumed["result"]["handled"] == 4 and len(resumed["observed"]) == 8
        state = store.snapshot()
        assert len(state["items"]) == 3 and len(state["jobs"]) == 4
        assert all(state["jobs"][ref]["state"] == "succeeded" for ref in jobs)
        assert state["jobs"][interrupted]["stages"]["source_sync"]["result"]["coverage"][
            "reobserved_after_restart"
        ]
        before = store.snapshot()
        idle = cli(root, "worker", "--allow-source-sync", "--browser-dir", str(run / "独立浏览器"), "--once")
        assert idle["data"]["handled"] == 0 and store.snapshot() == before

        # Explicit test-clock injection at the scheduler seam, never an end-user CLI bypass.
        future = (
            moment(cli(root, "sync-settings")["data"]["scopes"][0]["next_due_at"]) + timedelta(hours=72)
        ).isoformat()
        timer = SourceSchedule(SynchronizationWorkflow(store), clock=lambda: future)
        due = timer.admit_due(limit=20)
        assert len(due) == 4
        future_process = subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
        later = json.loads(future_process.stdout.strip().splitlines()[-1])
        assert later["result"]["handled"] == 4
        assert len(store.snapshot()["jobs"]) == 8  # Not 72 hourly catch-up batches per scope.
        assert len(store.snapshot()["items"]) == 3
        assert all(len(i["relations"]) == 4 for i in store.snapshot()["items"].values())
        for config in configs:
            cli(root, "configure-auto-sync", "--config-id", config, "--enabled", "no")
        before = store.snapshot()
        assert timer.admit_due() == [] and store.snapshot() == before
        assert all(not j["calls"] for j in before["jobs"].values())
        assert "synthetic-never-export" not in json.dumps(before)
        retained = media_reuse(run)
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "source_contracts": ["liked", "saved", "collection", "creator"],
            "default_interval_minutes": 60,
            "jobs_succeeded": 8,
            "unique_works": 3,
            "relations_per_work": 4,
            "interrupted_exit": process.returncode,
            "real_private_login": False,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "clock_advancement_is_fixture": True,
            "retained_media": retained,
            "verified": [
                "installed_cli_scope_and_timer",
                "autonomous_worker_admission",
                "atomic_four_jobs",
                "pause_before_sigkill",
                "restart_stays_paused_no_browser",
                "same_checkpoint_resume",
                "older_fixed_sync_binary_rejects_dispatch",
                "real_cli_idle_zero_writes",
                "bounded_sleep_no_catchup",
                "same_works_four_relations",
                "no_model_credentials_or_calls",
                "all_disabled_no_admission",
            ],
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
