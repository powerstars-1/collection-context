"""Installed page -> durable source job -> real worker/browser using original intercepted fixtures."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import expect, sync_playwright

import collection_context
from collection_context.application.contracts import utc_now
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.workflows.source_schedule import SourceSchedule, moment
from collection_context.workflows.synchronization import SynchronizationWorkflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("This acceptance requires the independently installed package")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="source-management-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创同步库")
    registry = AccessRegistry(store)
    owner = registry.create("原创主人", ui=True, manage=True)
    viewer = registry.create("原创只读", ui=True)
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
    errors, urls, submissions, workers = [], [], [], []

    def worker():
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("synchronization_smoke.py")),
                "--child",
                str(run),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert data["result"]["handled"] == 1 and len(data["observed"]) == 2
        workers.append(
            {"handled": data["result"]["handled"], "fixture_list_responses": len(data["observed"])}
        )

    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated source page server did not start")
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

            def requested(request):
                urls.append(request.url)
                if request.url == origin + "/v1/management/source-submit":
                    submissions.append(request.post_data_json)

            page.on("request", requested)
            page.goto(origin + "/connect")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#source-state")).to_contain_text("只读访问")
            expect(page.locator("#creator-url")).to_be_disabled()
            expect(page.locator("#refresh-sources")).to_be_disabled()
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#source-state")).to_contain_text("已授权主人")
            page.locator("#creator-url").fill(
                "https://www.douyin.com/user/MS4w-synthetic?share_token=synthetic-tracking"
            )
            page.locator("#creator-limit").fill("3")
            page.get_by_role("button", name="保存博主范围", exact=True).click()
            expect(page.locator(".source-row")).to_have_count(1)
            expect(page.locator(".source-row")).to_contain_text("尚未执行")
            assert not store.snapshot()["jobs"]
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="启用定时", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("定时开启")
            assert not store.snapshot()["jobs"]  # The page registers policy, not worker liveness.
            page.get_by_role("button", name="暂停定时", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("定时关闭")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="同步一次", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("已排队")
            expect(page.get_by_role("button", name="同步一次", exact=True)).to_be_disabled()
            assert len(submissions) == 1
            repeat = page.evaluate(
                """async body => {
                const session = await (await fetch('/v1/session')).json();
                return (await fetch('/v1/management/source-submit', {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':session.data.csrf_token}, body:JSON.stringify(body)})).json();
            }""",
                submissions[0],
            )
            assert repeat["ok"] and len(store.snapshot()["jobs"]) == 1
            worker()
            page.get_by_role("button", name="刷新范围", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("本批已保存")
            expect(page.locator(".source-row")).to_contain_text("已确认保存 3 条")
            expect(page.get_by_role("button", name="同步一次", exact=True)).to_be_enabled()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="启用定时", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("定时开启")
            worker()
            workflow = SynchronizationWorkflow(store)
            later = (moment(utc_now()) + timedelta(hours=2)).isoformat()
            proof_timer = SourceSchedule(workflow, clock=lambda: later)
            blocked = proof_timer.admit_due()[0]
            workflow.jobs.cancel(blocked)
            page.get_by_role("button", name="刷新范围", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("受阻，不自动重试")
            assert proof_timer.admit_due() == []
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="确认重新尝试", exact=True).click()
            expect(page.locator("#source-feedback")).to_contain_text("已明确允许")
            worker()
            page.get_by_role("button", name="刷新范围", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("最近批次：完成")
            page.get_by_role("button", name="暂停定时", exact=True).click()
            expect(page.locator(".source-row")).to_contain_text("定时关闭")
            assert page.locator(".source-controls input").evaluate("node => node.getBoundingClientRect().width") == 100
            page.screenshot(path=run / "desktop-sources.png", full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not overflow
            page.screenshot(path=run / "mobile-sources.png", full_page=True, animations="disabled")
            page.locator("nav a[data-page='materials']").click()
            expect(page.locator(".item")).to_have_count(3)
            page.locator("#query").fill("原创范围")
            page.get_by_role("button", name="查找", exact=True).click()
            expect(page.locator(".item")).to_have_count(3)
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#workspace")).to_be_hidden()
            expect(page.locator("#creator-url")).to_have_value("")
            context.close()
            browser.close()
        state = store.snapshot()
        assert len(state["items"]) == 3 and len(state["jobs"]) == 4
        assert sum(j["state"] == "succeeded" for j in state["jobs"].values()) == 3
        assert not state["settings"]["auto_process"] and not state["settings"]["auto_sync"]
        assert all(not j["calls"] for j in state["jobs"].values())
        assert not errors and all(url.startswith(origin + "/") for url in urls)
        assert "synthetic-tracking" not in json.dumps(state)
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "real_private_login_verified": False,
            "original_intercepted_fixtures": True,
            "worker_results": workers,
            "completed_jobs": 3,
            "cancelled_fixture_job": 1,
            "persisted_items": 3,
            "test_clock_advance_hours": 2,
            "verified": [
                "owner_only",
                "creator_canonical_scope",
                "register_not_execute",
                "manual_page_queue",
                "idempotent_repeat",
                "timer_enable_pause",
                "blocked_timer_explicit_retry",
                "installed_worker_browser",
                "readable_search",
                "desktop_mobile",
                "logout",
            ],
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
            raise RuntimeError("Isolated source page server did not stop") from None
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
