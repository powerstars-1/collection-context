"""Installed original browser -> folder proof -> owner page selection. Entire platform is intercepted fiction."""

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
from collection_context.cli import main as cli_main
from collection_context.infrastructure.browser import BrowserSession
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.workflows.connection import ConnectionCatalog


def discover_child(root: Path, profile: Path) -> int:
    original = BrowserSession.__enter__
    observed = []
    html = """<html><body>原创账号与收藏夹观察夹具<script>
      fetch('/aweme/v1/web/user/profile/self/');
      fetch('/aweme/v1/web/collects/list/?cursor=0');
      let next=false;addEventListener('wheel',()=>{if(!next){next=true;fetch('/aweme/v1/web/collects/list/?cursor=10');}});
    </script></body></html>"""

    def route(value):
        parts = urlsplit(value.request.url)
        if parts.hostname != "www.douyin.com":
            value.abort()
        elif parts.path == "/user/self":
            value.fulfill(status=200, content_type="text/html", body=html)
        elif parts.path == "/aweme/v1/web/user/profile/self/":
            value.fulfill(
                status=200,
                json={
                    "status_code": 0,
                    "user": {"uid": "123", "sec_uid": "MS4w-original-fixture", "nickname": "原创验证账号"},
                },
            )
            observed.append("account")
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
            observed.append("folder_" + cursor)
        else:
            value.abort()

    def enter(self):
        result = original(self)
        self.context.route("**/*", route)
        return result

    BrowserSession.__enter__ = enter
    try:
        result = cli_main(["--workspace", str(root), "discover-collections", "--browser-dir", str(profile)])
        if result:
            return result
        assert "folder_10" in observed and observed.count("account") >= 3
        print(json.dumps({"fictional_response_observations": observed, "real_platform_requests": 0}))
        return 0
    finally:
        BrowserSession.__enter__ = original


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-root", type=Path)
    parser.add_argument("--child-profile", type=Path)
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Independent installed package required")
    if args.child_root:
        return discover_child(args.child_root, args.child_profile)
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="connection-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创连接库")
    profile = run / "本产品独立浏览器"
    registry = AccessRegistry(store)
    owner = registry.create("连接主人", ui=True, manage=True)
    viewer = registry.create("连接只读", ui=True)
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
    errors, urls = [], []
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
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, locale="zh-CN")
            context.route(
                "**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort()
            )
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: urls.append(r.url))
            page.goto(origin + "/connect")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#connection-state")).to_contain_text("仅向主人")
            expect(page.locator("#self-source-kind")).to_be_disabled()
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#connection-state")).to_contain_text("尚无独立账号证明")
            expect(page.locator("#folder-source-id")).to_be_disabled()
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).absolute()),
                    "--child-root",
                    str(store.files.root),
                    "--child-profile",
                    str(profile),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            observations = json.loads(child.stdout.strip().splitlines()[-1])
            page.get_by_role("button", name="刷新本地连接", exact=True).click()
            expect(page.locator("#connection-state")).to_contain_text("原创验证账号")
            expect(page.locator("#folder-source-id option")).to_have_count(2)
            expect(page.locator("#folder-coverage")).to_contain_text("本次列表已到末页")
            for kind in ("saved", "liked"):
                page.locator("#self-source-kind").select_option(kind)
                page.locator("#self-source-confirmed").check()
                with page.expect_response(
                    lambda r: r.url == origin + "/v1/management/self-source-create"
                ) as response:
                    page.get_by_role("button", name="保存本人范围", exact=True).click()
                assert response.value.json()["ok"]
                expect(page.locator(".source-row")).to_have_count(1 if kind == "saved" else 2)
            page.locator("#folder-source-id").select_option("12")
            page.locator("#folder-source-confirmed").check()
            with page.expect_response(
                lambda r: r.url == origin + "/v1/management/self-source-create"
            ) as response:
                page.get_by_role("button", name="保存收藏夹范围", exact=True).click()
            assert response.value.json()["ok"]
            expect(page.locator(".source-row")).to_have_count(3)
            state = store.snapshot()
            assert not state["jobs"] and not state["items"] and not state["settings"]["auto_sync"]
            assert {
                x["collection_id"] for x in state["sync_scope_configs"].values() if x["kind"] == "collection"
            } == {"12"}
            page.screenshot(path=str(run / "desktop-connection.png"), full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            page.screenshot(path=str(run / "mobile-connection.png"), full_page=True, animations="disabled")
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            assert not overflow
            version = ConnectionCatalog(store).status()["version"]
            ConnectionCatalog(store).record(
                None, expected_version=version, error_code="source_login_required"
            )
            page.get_by_role("button", name="刷新本地连接", exact=True).click()
            expect(page.locator("#connection-state")).to_contain_text("未验证账号")
            expect(page.locator("#folder-source-id")).to_be_disabled()
            expect(page.locator("#folder-source-id option")).to_have_count(0)
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#login-panel")).to_be_visible()
            context.close()
            browser.close()
        assert not errors and all(u.startswith(origin + "/") for u in urls)
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "real_platform_requests": 0,
            "real_cloud_requests": 0,
            "source_protocol_real_compatibility_verified": False,
            "fictional_observations": observations,
            "registered_scope_kinds": ["liked", "saved", "collection"],
            "selected_folder_id": "12",
            "registered_jobs": 0,
            "verified": [
                "real_browser_intercepted_fiction",
                "independent_installed_cli",
                "two_folder_pages",
                "owner_only_selection",
                "same_name_distinct_id",
                "fixed_scope_without_execution",
                "failed_proof_clears_selection",
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
            server.communicate(timeout=10)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
