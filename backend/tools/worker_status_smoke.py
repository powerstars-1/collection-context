"""Installed CLI workers and real owner browser; no platform/provider work or private keys."""

from __future__ import annotations

import argparse
import json
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import expect, sync_playwright

import collection_context
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Independent installation required")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="worker-status-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创后台状态库")
    private = run / "空私有凭据目录"
    FileSecrets.initialize(private).close()
    registry = AccessRegistry(store)
    owner = registry.create("后台状态主人", ui=True, manage=True)
    viewer = registry.create("后台状态只读", ui=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
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
    workers = []
    events = []
    errors, urls = [], []

    def worker(*, model: bool, source: bool):
        command = [
            sys.executable,
            "-m",
            "collection_context.cli",
            "--workspace",
            str(store.files.root),
            "worker",
            "--poll-seconds",
            "0.1",
        ]
        if model:
            command += ["--allow-model-calls", "--credential-dir", str(private)]
        if source:
            command += ["--allow-source-sync", "--browser-dir", str(run / "未打开的浏览器目录")]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        workers.append(child)
        if not select.select([child.stdout], [], [], 15)[0]:
            raise RuntimeError("Worker did not report startup")
        line = child.stdout.readline()
        value = json.loads(line)
        assert value["data"]["event"] == "worker_started", value
        events.append(value["data"])
        return child

    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Owner server did not start")
            try:
                with opener.open(origin + "/health", timeout=0.5):
                    break
            except (URLError, OSError):
                time.sleep(0.02)
        before = store.snapshot()["generation"]
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, locale="zh-CN")
            context.route(
                "**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort()
            )
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: urls.append(r.url))
            page.clock.install()

            def refresh_worker():
                with page.expect_response(lambda r: r.url == origin + "/v1/management/overview") as response:
                    page.get_by_role("button", name="刷新状态", exact=True).click()
                stamp = response.value.json()["data"]["worker"]["observed_at"]
                expect(page.locator("#worker-state")).to_contain_text(stamp)

            page.goto(origin + "/activity")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#worker-state")).to_contain_text("仅向主人")
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#worker-state")).to_contain_text("未检测到运行中的后台")
            child = worker(model=True, source=False)
            refresh_worker()
            expect(page.locator("#worker-state")).to_contain_text("后台持有运行锁")
            expect(page.locator("#worker-state")).to_contain_text("同步未允许，模型调用允许")
            expect(page.locator("#worker-state")).to_contain_text("不代表任务有进度")
            child.send_signal(signal.SIGSTOP)
            refresh_worker()
            expect(page.locator("#worker-state")).to_contain_text("后台持有运行锁")
            page.clock.fast_forward(31_000)
            expect(page.locator("#worker-state")).to_contain_text("快照已过期")
            child.kill()
            child.communicate(timeout=10)
            assert child.returncode == -9
            refresh_worker()
            expect(page.locator("#worker-state")).to_contain_text("未检测到运行中的后台")
            assert (store.files.root / ".context/后台所有权.lock").exists()
            child = worker(model=False, source=True)
            page.locator('[data-page="connect"]').click()
            expect(page.locator("#source-state")).to_contain_text("后台持有运行锁")
            expect(page.locator("#source-state")).to_contain_text("同步允许，模型调用未允许")
            page.screenshot(
                path=str(run / "desktop-source-status.png"), full_page=True, animations="disabled"
            )
            page.locator('[data-page="activity"]').click()
            expect(page.locator("#worker-state")).to_contain_text("同步允许，模型调用未允许")
            page.set_viewport_size({"width": 412, "height": 915})
            page.screenshot(path=str(run / "mobile-worker-status.png"), full_page=True, animations="disabled")
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            assert overflow is False
            child.terminate()
            child.communicate(timeout=10)
            assert child.returncode == 0
            refresh_worker()
            expect(page.locator("#worker-state")).to_contain_text("未检测到运行中的后台")
            page.get_by_role("button", name="退出", exact=True).click()
            page.clock.fast_forward(31_000)
            expect(page.locator("#login-panel")).to_be_visible()
            context.close()
            browser.close()
        assert store.snapshot()["generation"] == before and not store.snapshot()["jobs"]
        assert not errors and all(u.startswith(origin + "/") for u in urls)
        assert not list(private.iterdir()) and not (run / "未打开的浏览器目录").exists()
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "worker_start_events": events,
            "idle_library_generation_unchanged": True,
            "killed_worker_exit_code": -9,
            "graceful_worker_exit_code": 0,
            "snapshot_expiry_test_clock_seconds": 31,
            "live_lease_not_progress": True,
            "verified": [
                "reader_denied",
                "actual_installed_cli_worker",
                "model_only_authority",
                "source_only_authority",
                "SIGSTOP_not_progress",
                "SIGKILL_stale_file_offline",
                "explicit_refresh",
                "snapshot_expiry",
                "graceful_shutdown",
                "logout",
                "desktop_mobile",
            ],
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        for child in [*workers, server]:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
