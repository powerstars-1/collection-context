"""Real socket + browser smoke of isolated original fixtures; no user data or cloud calls."""

from __future__ import annotations

import argparse
import json
import platform
import re
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
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--browser-binary", type=Path, help="Explicit installed developer browser; no download"
    )
    args = parser.parse_args()
    if args.browser_binary is not None and (
        not args.browser_binary.is_absolute()
        or not args.browser_binary.is_file()
        or args.browser_binary.is_symlink()
    ):
        raise ValueError("Choose an explicit ordinary installed browser binary")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="web-smoke-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创合成资料库")
    title = "原创演示：用 UI 画布练习"
    item = store.upsert(
        {
            "native_id": "1",
            "title": title,
            "body": "原文含 <script>window.fixtureInjected=1</script>，只用于验证文字渲染，不是实际同步的视频。",
            "author": "原创验证样例",
        },
        kind="liked",
        scope_id="s_likes",
    )["item"]
    store.upsert(
        {"native_id": "1", "title": title, "body": item["body"], "author": item["author"]},
        kind="collection",
        scope_id="s_ui",
    )
    store.save_artifact(
        item["id"],
        "screen",
        "原创合成画面：390×844。Tailwind 页面模板；禁止额外文字。不是实际 OCR 输出。",
        processor_version="synthetic_fixture_v1",
        expected_content_hash=item["content_hash"],
        coverage={"accuracy": "synthetic_fixture", "not_real_extraction": True},
    )
    store.upsert(
        {
            "native_id": "2",
            "media_type": "image",
            "title": "原创演示：剪辑节奏清单",
            "body": "原创协议夹具，不是真实视频。",
            "author": "原创验证样例",
        },
        kind="creator",
        scope_id="s_creator",
    )
    FileIndex(store).rebuild()
    registry = AccessRegistry(store)
    access = registry.create("隔离网页验证", ui=True)
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
    try:
        deadline = time.monotonic() + 20
        opener = build_opener(ProxyHandler({}))
        while True:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated web server did not start")
            try:
                with opener.open(origin + "/health", timeout=0.5) as response:
                    assert json.load(response)["data"]["service"] == "collection-context"
                break
            except (URLError, OSError):
                time.sleep(0.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(args.browser_binary) if args.browser_binary else None,
            )
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, locale="zh-CN")
            context.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(origin + "/") else route.abort()
                ),
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(origin)
                expect(page.get_by_label("产品访问口令")).to_be_visible()
                page.get_by_label("产品访问口令").fill(access["token"])
                page.get_by_role("button", name="进入收藏库", exact=True).click()
                cards = page.get_by_role("button", name=re.compile("原创演示："))
                expect(cards).to_have_count(2)
                expect(page.get_by_label("产品访问口令")).to_have_count(0)
                cards.filter(has_text=title).click()
                expect(page.locator("#evidence-text")).to_contain_text("fixtureInjected")
                assert page.evaluate("window.fixtureInjected || 0") == 0
                page.get_by_role("tab", name="转写", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("此类内容尚未提取")
                expect(page.locator("#evidence-text")).not_to_contain_text("fixtureInjected")
                page.get_by_role("tab", name="画面文字", exact=True).click()
                expect(page.locator("#evidence-text")).to_contain_text("390×844")
                page.screenshot(path=run / "desktop-materials.png", full_page=True, animations="disabled")
                page.get_by_label("搜索已有资料").fill("UI 390")
                page.get_by_role("button", name="搜索收藏", exact=True).click()
                expect(cards).to_have_count(1)
                page.get_by_role("button", name="博主作品", exact=True).click()
                expect(cards).to_have_count(0)
                page.get_by_role("button", name="同步与处理", exact=True).click()
                expect(page.locator("#connect")).to_be_visible()
                expect(page.locator("#connection-actions")).to_have_attribute("disabled", "")
                expect(page.get_by_role("button", name="打开独立登录", exact=True)).to_be_disabled()
                page.get_by_role("button", name="任务与处理", exact=True).click()
                expect(page.locator("#activity")).to_be_visible()
                expect(page.locator("#management-state")).to_contain_text("只读访问")
                expect(page.locator("#auto-fields")).to_have_attribute("disabled", "")
                page.screenshot(path=run / "desktop-activity.png", full_page=True, animations="disabled")
                page.get_by_role("button", name="收藏库", exact=True).click()
                expect(cards).to_have_count(2)
                cards.filter(has_text="原创演示：剪辑节奏清单").click()
                page.get_by_role("tab", name="转写", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("此资料没有音轨，转写不适用")
                page.set_viewport_size({"width": 412, "height": 915})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=run / "mobile-materials.png", full_page=True, animations="disabled")
                page.get_by_role("button", name="退出", exact=True).click()
                expect(page.get_by_label("产品访问口令")).to_be_visible()
                assert page.get_by_label("产品访问口令").input_value() == ""
                expect(page.locator("#main-content")).to_have_count(0)
            except Exception:
                page.screenshot(path=run / "failed-page.png", full_page=True, animations="disabled")
                raise
            finally:
                context.close()
                browser.close()
        assert not errors, f"Browser JavaScript errors: {errors}"
        (run / "report.json").write_text(
            json.dumps(
                {
                    "result": "passed",
                    "scope": "current_host_original_loopback_browser_only",
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "runtime_module": collection_context.__file__,
                    "javascript_errors": len(errors),
                    "model_requests": 0,
                    "platform_requests": 0,
                    "native_linux_execution_verified": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "result": "passed",
                    "artifact_dir": str(run),
                    "runtime_module": collection_context.__file__,
                    "server_entrypoint": "collection_context.interfaces.server",
                    "real_sync_or_extraction": False,
                    "verified": [
                        "socket_http",
                        "cookie_login",
                        "two_source_dedupe",
                        "list_search_filters",
                        "source_text_not_executable",
                        "missing_audio",
                        "image_audio_not_applicable",
                        "screen_read",
                        "navigation",
                        "mobile_no_horizontal_overflow",
                        "logout",
                    ],
                    "javascript_errors": len(errors),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as error:
        (run / "report.json").write_text(
            json.dumps(
                {
                    "result": "failed",
                    "scope": "current_host_original_loopback_browser_only",
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "error_type": type(error).__name__,
                    "model_requests": 0,
                    "platform_requests": 0,
                    "native_linux_execution_verified": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps({"result": "failed", "artifact_dir": str(run)}), flush=True)
        raise
    finally:
        try:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise RuntimeError("Isolated server did not stop cleanly") from None
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
