"""Installed owner page -> owned browser -> proof/folders/cancel. Platform is original intercepted fiction."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import expect, sync_playwright

import collection_context
from collection_context.infrastructure.browser import BrowserSession
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.server import main as server_main
from collection_context.library.store import LibraryStore


def server_child(root: Path, profile: Path, origin: str, hold: Path) -> int:
    original = BrowserSession.__enter__

    def note(value):
        with (hold.parent / "fixture-events.jsonl").open("a") as stream:
            stream.write(json.dumps({"stage": value, "time": time.monotonic()}) + "\n")

    html = """<html><body><h1>原创离线账号验证样例，不是真实平台</h1><button>登录</button><script>
      fetch('/aweme/v1/web/user/profile/self/');fetch('/aweme/v1/web/collects/list/?cursor=0');
      let next=false;addEventListener('wheel',()=>{if(!next){next=true;fetch('/aweme/v1/web/collects/list/?cursor=10');}});
    </script></body></html>"""

    def route(value):
        parts = urlsplit(value.request.url)
        if parts.hostname != "www.douyin.com":
            value.abort()
        elif parts.path == "/user/self":
            value.fulfill(status=200, content_type="text/html", body=html)
        elif parts.path == "/aweme/v1/web/user/profile/self/":
            note("account_response")
            if hold.exists():
                hold.with_suffix(".observed").write_text("Original account-page request observed.\n")
            payload = {
                "status_code": 0,
                "user": {"uid": "123", "sec_uid": "MS4w-original-page", "nickname": "原创页面验证账号"},
            }
            value.fulfill(status=200, json={"status_code": 8} if hold.exists() else payload)
        elif parts.path == "/aweme/v1/web/collects/list/":
            cursor = parse_qs(parts.query)["cursor"][0]
            identity = "11" if cursor == "0" else "12"
            value.fulfill(
                status=200,
                json={
                    "status_code": 0,
                    "collects_list": [{"collects_id_str": identity, "collects_name": "同名教程"}],
                    "cursor": 10 if cursor == "0" else 20,
                    "has_more": 1 if cursor == "0" else 0,
                },
            )
        else:
            value.abort()

    def enter(self):
        note("browser_launch")
        result = original(self)
        note("browser_ready")
        self.context.route("**/*", route)
        return result

    BrowserSession.__enter__ = enter
    try:
        return server_main(
            [
                "--workspace",
                str(root),
                "--origin",
                origin,
                "--allow-source-connect",
                "--browser-dir",
                str(profile),
            ]
        )
    finally:
        BrowserSession.__enter__ = original


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-root", type=Path)
    parser.add_argument("--child-profile", type=Path)
    parser.add_argument("--child-origin")
    parser.add_argument("--child-hold", type=Path)
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Independent installed runtime required")
    if args.child_root:
        return server_child(args.child_root, args.child_profile, args.child_origin, args.child_hold)
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="connection-page-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创页面连接库")
    registry = AccessRegistry(store)
    owner = registry.create("连接主人", ui=True, manage=True)
    reader = registry.create("只读预览", ui=True)
    profile, hold = run / "本产品独立浏览器", run / "暂不证明账号.fixture"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).absolute()),
            "--child-root",
            str(store.files.root),
            "--child-profile",
            str(profile),
            "--child-origin",
            origin,
            "--child-hold",
            str(hold),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    errors, requests = [], []
    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Test server did not start")
            try:
                with opener.open(origin + "/health", timeout=0.5):
                    break
            except (URLError, OSError):
                time.sleep(0.02)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, locale="zh-CN")
            context.route(
                "**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort()
            )
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: requests.append(r.url))
            page.goto(origin + "/connect")
            page.locator("#access-token").fill(reader["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#connection-run-state")).to_contain_text("仅向主人")
            expect(page.locator("#connection-login")).to_be_disabled()
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#connection-login")).to_be_enabled()
            page.locator("#connection-login").click()
            expect(page.locator("#connection-run-state")).to_contain_text("先确认")
            assert not any(u.endswith("/connection-start") for u in requests)
            page.locator("#connection-access-confirmed").check()
            page.locator("#connection-login").click()
            expect(page.locator("#connection-run-state")).to_contain_text("本次观察结束", timeout=45000)
            expect(page.locator("#connection-state")).to_contain_text("原创页面验证账号")
            page.locator("#connection-access-confirmed").check()
            page.locator("#connection-folders").click()
            expect(page.locator("#folder-source-id option")).to_have_count(2, timeout=15000)
            expect(page.locator("#connection-run-state")).to_contain_text("本次观察结束")
            page.locator("#folder-source-id").select_option("12")
            page.locator("#folder-source-confirmed").check()
            page.get_by_role("button", name="保存收藏夹范围", exact=True).click()
            expect(page.locator(".source-row")).to_have_count(1)
            page.screenshot(path=str(run / "desktop.png"), full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            page.screenshot(path=str(run / "mobile.png"), full_page=True, animations="disabled")
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            assert not overflow
            hold.write_text("Original test fixture; prevents account proof; no secrets.\n")
            page.locator("#connection-access-confirmed").check()
            page.locator("#connection-login").click()
            expect(page.locator("#connection-cancel")).to_be_enabled()
            deadline = time.monotonic() + 10
            while not hold.with_suffix(".observed").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert hold.with_suffix(".observed").exists()
            duplicate = page.evaluate(
                """async()=>{const r=await fetch('/v1/management/connection-start',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({mode:'check',source_confirmed:true})});return (await r.json()).error.code;}"""
            )
            assert duplicate == "connection_busy"
            page.locator("#connection-cancel").click()
            expect(page.locator("#connection-run-state")).to_contain_text("本次观察已取消", timeout=15000)
            expect(page.locator("#connection-state")).to_contain_text("未验证账号")
            expect(page.locator("#folder-source-id")).to_be_disabled()
            expect(page.locator("#folder-source-id option")).to_have_count(0)
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#login-panel")).to_be_visible()
            context.close()
            browser.close()
        state = store.snapshot()
        assert not state["jobs"] and not state["items"] and len(state["sync_scope_configs"]) == 1
        assert not errors and all(u.startswith(origin + "/") for u in requests)
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "source_protocol_real_compatibility_verified": False,
            "registered_jobs": 0,
            "verified": [
                "owner_page_only",
                "explicit_platform_confirmation",
                "actual_headed_owned_browser",
                "async_page_login",
                "two_page_folder_discovery",
                "same_name_stable_id",
                "no_duplicate_window",
                "cancel_closes_observation",
                "failed_proof_disables_folders",
                "zero_sync_or_model",
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
            server.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=10)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
