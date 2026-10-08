"""Installed package, actual browser management; original fixtures and zero cloud requests."""

from __future__ import annotations

import argparse
import io
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from PIL import Image
from playwright.sync_api import expect, sync_playwright

import collection_context
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="management-smoke-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创管理验证库")
    # Fake reference never resolves: this test registers plans but does not launch a worker.
    for role in ("audio", "vision", "summary"):
        ModelCatalog(store).configure(
            role=role,
            base_url="https://fixture.invalid/v1",
            model="synthetic-" + role,
            credential_ref="k_00000000000000000000000000000000",
            protocol="chat_audio" if role == "audio" else "chat",
        )
    image = io.BytesIO()
    Image.new("RGB", (80, 80), (227, 222, 211)).save(image, format="PNG")
    item = store.upsert(
        {"native_id": "101", "media_type": "image", "title": "原创管理样例：画面与节奏"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    PreparedInputs(store).prepare_images(item["id"], [(image.getvalue(), "image/png")])
    registry = AccessRegistry(store)
    owner = registry.create("隔离主人", ui=True, manage=True)
    viewer = registry.create("隔离只读", ui=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    process = subprocess.Popen(
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
    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated management server did not start")
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
            page.goto(origin + "/activity")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#management-state")).to_contain_text("只读访问")
            expect(page.locator("#auto-enabled")).to_be_disabled()
            denied = page.evaluate("""async () => {
              const session = await (await fetch('/v1/session')).json();
              return (await fetch('/v1/management/overview', {method:'POST', headers:{'Content-Type':'application/json',
                'X-CSRF-Token':session.data.csrf_token}, body:'{}'})).status;
            }""")
            assert denied == 403
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#management-state")).to_contain_text("已授权主人管理")
            expect(page.locator("#prepared-items input")).to_have_count(1)
            page.locator("#auto-enabled").select_option("yes")
            page.get_by_role("button", name="保存自动规则", exact=True).click()
            expect(page.locator("#management-feedback")).to_contain_text("须确认")
            assert store.snapshot()["settings"]["auto_process"] is False
            page.locator("#auto-fee").check()
            page.get_by_role("button", name="保存自动规则", exact=True).click()
            expect(page.locator("#management-feedback")).to_contain_text("已保存")
            expect(page.locator("#auto-state")).to_contain_text("处理开启")
            assert not store.snapshot()["jobs"]
            page.locator("#prepared-items input").check()
            page.locator("#history-fee").check()
            page.get_by_role("button", name="提交所选历史批次", exact=True).click()
            expect(page.locator(".task-row")).to_have_count(1)
            expect(page.locator(".task-row")).to_contain_text("已排队")
            # Same browser-session idempotency key is preserved for a repeat submission.
            page.locator("#prepared-items input").check()
            page.locator("#history-fee").check()
            page.get_by_role("button", name="提交所选历史批次", exact=True).click()
            expect(page.locator("#management-feedback")).to_contain_text("已登记")
            assert len(store.snapshot()["jobs"]) == 1
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="取消后续处理", exact=True).click()
            expect(page.locator(".task-row")).to_contain_text("已取消")
            page.locator("#auto-enabled").select_option("no")
            page.get_by_role("button", name="保存自动规则", exact=True).click()
            expect(page.locator("#auto-state")).to_contain_text("处理关闭")
            page.screenshot(path=run / "desktop-management.png", full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not overflow
            page.screenshot(path=run / "mobile-management.png", full_page=True, animations="disabled")
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#workspace")).to_be_hidden()
            context.close()
            browser.close()
        assert not errors and all(url.startswith(origin + "/") for url in requests)
        state = store.snapshot()
        assert all(not job["calls"] for job in state["jobs"].values())
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "real_cloud_requests": 0,
            "verified": [
                "owner_cookie_authority",
                "viewer_cannot_manage",
                "fee_confirm_before_auto",
                "auto_pause",
                "explicit_history",
                "browser_repeat_no_duplicate",
                "cancel_task",
                "persisted_states",
                "logout",
                "desktop_and_mobile",
            ],
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise RuntimeError("Isolated management server did not stop") from None
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
