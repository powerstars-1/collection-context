"""Installed page -> durable job -> separate original browser worker -> read evidence.

All platform responses and media are fictional intercepted fixtures. No real login or cloud call.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from PIL import Image
from playwright.sync_api import expect, sync_playwright

import collection_context
from collection_context.cli import main as cli_main
from collection_context.infrastructure.browser import BrowserSession
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.sources.downloads import DouyinDownloads
from collection_context.workflows.jobs import JobManager


def worker(root: Path, profile: Path, log: Path, crash: bool) -> int:
    enter = BrowserSession.__enter__
    finish = JobManager.finish

    def note(stage):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stage": stage}) + "\n")

    def route(value):
        parts = urlsplit(value.request.url)
        if parts.hostname != "www.douyin.com":
            value.abort()
        elif parts.path in {"/video/81", "/note/82"}:
            identity = parts.path.rsplit("/", 1)[1]
            value.fulfill(
                status=200,
                content_type="text/html",
                body=f"<html><body>原创单条样例<script>fetch('/aweme/v1/web/aweme/detail/?id={identity}')</script></body></html>",
            )
        elif parts.path == "/aweme/v1/web/aweme/detail/":
            identity = parse_qs(parts.query)["id"][0]
            note("detail_" + identity)
            raw = {
                "aweme_id": identity,
                "desc": "原创单条验收\n完整正文：先保留原文再准备媒体。",
                "create_time": 1700000000,
                "author": {"nickname": "原创夹具作者", "sec_uid": "MS4w-original-fixture"},
                "video": {
                    "play_addr": {"url_list": ["https://fixture.douyinvod.com/video?signature=never-persist"]}
                },
            }
            if identity == "82":
                raw["images"] = [
                    {"url_list": [f"https://fixture.douyinvod.com/page{i}?signature=never-persist"]}
                    for i in (1, 2)
                ]
            value.fulfill(status=200, json={"status_code": 0, "aweme_detail": raw})
        else:
            value.abort()

    def owned(self):
        result = enter(self)
        self.context.route("**/*", route)
        return result

    def download(self, observed):
        note("original_image_fixture")
        pages = []
        for index in range(len(observed.assets)):
            buffer = io.BytesIO()
            Image.new("RGB", (160, 90), (230, 215 - index * 20, 200)).save(buffer, "PNG")
            pages.append((buffer.getvalue(), "image/png"))
        return pages

    def interrupted(self, ref, state_name, **kwargs):
        # The factory context has already closed the browser; authoritative stage is committed.
        if crash and state_name == "succeeded":
            note("committed_before_process_exit")
            os._exit(17)
        return finish(self, ref, state_name, **kwargs)

    BrowserSession.__enter__ = owned
    DouyinDownloads.fetch = download
    JobManager.finish = interrupted
    return cli_main(
        [
            "--workspace",
            str(root),
            "worker",
            "--allow-source-sync",
            "--browser-dir",
            str(profile),
            "--once",
            "--max-jobs",
            "1",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-root", type=Path)
    parser.add_argument("--worker-profile", type=Path)
    parser.add_argument("--worker-log", type=Path)
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Must validate an installed non-editable package outside the repository")
    if args.worker_root:
        return worker(args.worker_root, args.worker_profile, args.worker_log, args.crash)
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="addition-page-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "独立单条资料库")
    registry = AccessRegistry(store)
    owner = registry.create("原创验收主人", ui=True, manage=True)
    viewer = registry.create("原创只读", ui=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "collection_context.interfaces.server",
            "--workspace",
            str(store.files.root),
            "--origin",
            origin,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    errors = []
    requests = []
    processes = []

    def execute(crash=False):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker-root",
            str(store.files.root),
            "--worker-profile",
            str(run / "独立浏览器"),
            "--worker-log",
            str(run / "fixture-events.jsonl"),
        ]
        if crash:
            command.append("--crash")
        process = subprocess.Popen(command, cwd=run, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        stdout, stderr = process.communicate(timeout=120)
        if process.returncode != (17 if crash else 0):
            raise RuntimeError("Isolated worker failed: " + stderr.decode()[-800:])
        (run / ("interrupted-worker.jsonl" if crash else f"worker-{len(processes)}.jsonl")).write_bytes(
            stdout
        )

    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated server did not start")
            try:
                with opener.open(origin + "/health", timeout=0.5):
                    break
            except (URLError, OSError):
                time.sleep(0.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, locale="zh-CN")
            context.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(origin + "/") else route.abort()
                ),
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(origin + "/connect")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#link-url")).to_be_disabled()
            page.locator("#logout").click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#link-url")).to_be_enabled()
            page.locator("#link-url").fill("https://www.douyin.com/video/81?tracking=never-persist")
            page.get_by_role("button", name="登记单条任务", exact=True).click()
            expect(page.locator("#link-feedback")).to_contain_text("须确认")
            assert not store.snapshot()["jobs"]
            page.locator("#link-confirmed").check()
            page.get_by_role("button", name="登记单条任务", exact=True).click()
            expect(page.locator("#link-result")).to_contain_text("已排队")
            first = next(iter(store.snapshot()["jobs"]))
            page.locator("#link-confirmed").check()
            page.get_by_role("button", name="登记单条任务", exact=True).click()
            expect(page.locator("#link-feedback")).to_contain_text("已登记")
            assert len(store.snapshot()["jobs"]) == 1
            execute(crash=True)
            assert store.snapshot()["jobs"][first]["state"] == "running"
            assert len(store.snapshot()["items"]) == 1
            execute()
            page.locator("#link-refresh").click()
            expect(page.locator("#link-result")).to_contain_text("完成")
            expect(page.locator("#link-result")).to_contain_text("原文：已保存")
            events = (run / "fixture-events.jsonl").read_text()
            assert events.count("detail_81") == 1
            page.locator("#link-result a").click()
            expect(page.locator("#evidence-text")).to_contain_text("完整正文")
            page.goto(origin + "/connect")
            expect(page.locator("#link-url")).to_be_enabled()
            page.locator("#link-url").fill("https://www.douyin.com/note/82")
            page.locator("#link-download").check()
            page.locator("#link-confirmed").check()
            page.get_by_role("button", name="登记单条任务", exact=True).click()
            expect(page.locator("#link-result")).to_contain_text("已排队")
            execute()
            page.locator("#link-refresh").click()
            expect(page.locator("#link-result")).to_contain_text("媒体：已保存")
            page.screenshot(path=run / "desktop.png", full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            page.screenshot(path=run / "mobile.png", full_page=True, animations="disabled")
            overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.goto(origin + "/activity")
            expect(page.locator(".task-row")).to_have_count(2)
            expect(page.locator(".task-row a")).to_have_count(2)
            page.locator("#logout").click()
            expect(page.locator("#link-url")).to_have_value("")
            browser.close()
        state = store.snapshot()
        assert all(j["state"] == "succeeded" and not j["calls"] for j in state["jobs"].values())
        assert not state["settings"]["auto_process"]
        assert "never-persist" not in json.dumps(state)
        assert len(state["items"]) == 2
        assert len(state.get("prepared_inputs", {})) == 1
        assert not errors and not overflow
        report = {
            "status": "passed",
            "installed_module": str(collection_context.__file__),
            "read_only_denied": True,
            "confirmation_required": True,
            "duplicate_submission_one_job": True,
            "real_process_exit_after_commit": 17,
            "restart_without_reobserving_ready_work": True,
            "source_detail_observations": 2,
            "jobs_succeeded": 2,
            "items": 2,
            "ordered_original_images": 2,
            "prepared_inputs": 1,
            "direct_saved_material_read": True,
            "javascript_errors": errors,
            "mobile_horizontal_overflow": overflow,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "real_cdn_download_verified": False,
            "model_processing_enabled": False,
            "synthetic_platform_interception": True,
            "terminated_children": True,
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(run / "report.json"), "status": "passed"}))
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=30)
        if server.poll() is None:
            server.terminate()
        server.communicate(timeout=40)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
