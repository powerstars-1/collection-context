"""Installed original source queue + real Chromium; intercepted original fixtures, not platform acceptance."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import collection_context
from collection_context.cli import no_model_authority
from collection_context.infrastructure.browser import BrowserSession
from collection_context.library.store import LibraryStore
from collection_context.sources.account import self_account
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker

ACCOUNT_PAYLOAD = {
    "status_code": 0,
    "user": {"uid": "123", "sec_uid": "MS4w-synthetic", "nickname": "原创浏览器验证"},
}


def original_row(identity: str) -> dict:
    return {
        "aweme_id": identity,
        "desc": "原创范围/恢复样例 " + identity,
        "create_time": 1700000000,
        "author": {"nickname": "原创验证作者", "sec_uid": "MS4w-synthetic"},
        "video": {
            "play_addr": {
                "url_list": ["https://fixture.douyinvod.com/video?signature=synthetic-never-export"]
            }
        },
    }


@contextmanager
def intercepted_source(profile: Path):
    observed = []
    with BrowserSession(profile, headless=True) as browser:
        assert browser.context is not None

        def route(request_route):
            request = request_route.request
            parts = urlsplit(request.url)
            if parts.scheme != "https" or parts.hostname != "www.douyin.com":
                request_route.abort()
                return
            params = parse_qs(parts.query)
            if parts.path == "/aweme/v1/web/user/profile/self/":
                request_route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(ACCOUNT_PAYLOAD)
                )
                return
            if parts.path in {"/user/self", "/user/MS4w-synthetic"}:
                tab = params.get("showTab", [None])[0]
                if parts.path.endswith("MS4w-synthetic"):
                    api = "/aweme/v1/web/aweme/post/?sec_user_id=MS4w-synthetic&max_cursor="
                elif tab == "like":
                    api = "/aweme/v1/web/aweme/favorite/?sec_user_id=MS4w-synthetic&max_cursor="
                elif params.get("collects_id") == ["9"]:
                    api = "/aweme/v1/web/collects/video/list/?collects_id=9&cursor="
                elif tab:
                    api = "/aweme/v1/web/aweme/listcollection/?cursor="
                else:
                    api = None
                script = "fetch('/aweme/v1/web/user/profile/self/');"
                if api:
                    script += f"fetch({json.dumps(api + '0')}); window.addEventListener('wheel', () => fetch({json.dumps(api + '10')}), {{once:true}});"
                request_route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<html><body style='height:4000px'>原创合成页面<script>"
                    + script
                    + "</script></body></html>",
                )
                return
            if parts.path in {
                "/aweme/v1/web/aweme/post/",
                "/aweme/v1/web/aweme/favorite/",
                "/aweme/v1/web/aweme/listcollection/",
                "/aweme/v1/web/collects/video/list/",
            }:
                field = "max_cursor" if "max_cursor" in params else "cursor"
                cursor = params[field][0]
                observed.append({"endpoint": parts.path, "cursor": cursor})
                payload = {
                    "status_code": 0,
                    "aweme_list": [
                        original_row(i) for i in (("81", "82") if cursor == "0" else ("82", "83"))
                    ],
                    field: 10 if cursor == "0" else 20,
                    "has_more": int(cursor == "0"),
                }
                request_route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
                return
            request_route.abort()

        browser.context.route("**/*", route)
        yield DouyinBrowserSource(browser), observed


def child(root: Path, *, interrupt: bool) -> int:
    with closing(LibraryStore(root / "原创同步库")) as store:
        requests = []

        @contextmanager
        def factory():
            with intercepted_source(root / "独立浏览器") as (source, observed):
                yield source
                requests.extend(observed)

        if interrupt:
            original = IngestionWorkflow.import_item

            def gated(self, observed, **kwargs):
                if observed.source["native_id"] == "82":
                    print("fixture_checkpoint_gate", flush=True)
                    while True:
                        time.sleep(0.1)
                return original(self, observed, **kwargs)

            IngestionWorkflow.import_item = gated
        worker = BackgroundWorker(
            ExtractionWorkflow(store, no_model_authority), SynchronizationWorkflow(store, factory)
        )
        result = worker.serve(allow_model_calls=False, allow_source_sync=True, once=True, max_jobs=20)
        print(
            json.dumps(
                {"result": result, "observed": requests, "runtime_module": collection_context.__file__}
            ),
            flush=True,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--interrupt", action="store_true")
    args = parser.parse_args()
    if args.child:
        return child(args.child, interrupt=args.interrupt)
    if args.output is None:
        parser.error("--output required")
    if os.name != "posix":
        raise RuntimeError("This process-group interruption proof is POSIX-only, not Windows acceptance")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="sync-smoke-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创同步库")
    planner = SynchronizationWorkflow(store)
    account_ref = self_account(ACCOUNT_PAYLOAD).public()["account_ref"]
    jobs = []
    for kind in ("liked", "saved", "collection", "creator"):
        options = (
            {"creator_url": "https://www.douyin.com/user/MS4w-synthetic"}
            if kind == "creator"
            else {"account_ref": account_ref, **({"collection_id": "9"} if kind == "collection" else {})}
        )
        config = planner.configure(kind, limit=3, **options)
        jobs.append(planner.submit(config["config_id"], idempotency_key="original-" + kind)["id"])
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
        # Bounded parent polling avoids an unbounded readline if browser startup fails.
        import select

        deadline = time.monotonic() + 60
        line = ""
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([process.stdout], [], [], 0.1)[0]:
                line = process.stdout.readline().strip()
                if line == "fixture_checkpoint_gate":
                    break
        if line != "fixture_checkpoint_gate":
            raise RuntimeError("Original checkpoint gate was not reached")
        assert len(store.snapshot()["items"]) == 1
        before = planner.jobs.get(jobs[0])
        assert (
            before["state"] == "running" and len(before["stages"]["source_sync"]["result"]["imported"]) == 1
        )
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        resumed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
        completed = json.loads(resumed.stdout.strip().splitlines()[-1])
        state = store.snapshot()
        assert completed["result"]["handled"] == 4 and len(state["items"]) == 3
        assert all(
            planner.jobs.get(ref)["state"] == "succeeded" and not planner.jobs.get(ref)["calls"]
            for ref in jobs
        )
        assert all(len(item["relations"]) == 4 for item in state["items"].values())
        assert planner.jobs.get(jobs[0])["stages"]["source_sync"]["result"]["coverage"][
            "reobserved_after_restart"
        ]
        assert "synthetic-never-export" not in json.dumps(state)
        idle_command = [
            sys.executable,
            "-m",
            "collection_context.cli",
            "--workspace",
            str(store.files.root),
            "worker",
            "--allow-source-sync",
            "--browser-dir",
            str(run / "独立浏览器"),
            "--once",
        ]
        idle = subprocess.run(idle_command, capture_output=True, text=True, timeout=30, check=True)
        assert json.loads(idle.stdout.strip().splitlines()[-1])["data"]["handled"] == 0
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "source_contracts": ["liked", "saved", "collection", "creator"],
            "jobs_succeeded": 4,
            "unique_works": 3,
            "relations_per_work": 4,
            "observed_responses_after_restart": len(completed["observed"]),
            "real_cloud_requests": 0,
            "real_platform_requests": 0,
            "real_private_login": False,
            "verified": [
                "installed_own_browser",
                "two_observed_pages",
                "cursor_continuity",
                "four_relations",
                "sigkill_after_first_checkpoint",
                "same_profile_reopen",
                "same_queue_resume",
                "installed_cli_idle_no_repeat",
                "no_model_credential",
                "signed_urls_not_persisted",
            ],
            "interrupted_exit": process.returncode,
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
