"""Exercise the real RedNote-adapted UI against an isolated ORIGINAL fixture library.

No platform login, private data, model API keys or model requests. --serve keeps
the isolated preview alive for owner review; its fixture token is NOT a real key.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore

FIXTURE_TOKEN = "design-review-fixture-only-0000000000000000"


def seed(workspace: Path) -> None:
    with closing(LibraryStore.initialize(workspace)) as store:
        examples = [
            (
                "91000001",
                "多页面 UI 提示词：先确定结构，再写页面约束",
                "liked",
                "video",
                "把页面目标、导航结构、交互状态与视觉规则拆开描述。这个原创演示用来核对资料阅读界面，不对应真实视频。",
            ),
            (
                "91000002",
                "五秒换装视频：让人物、动作和机位保持一致",
                "saved",
                "video",
                "先锁定人物与相机位置，再描述动作起止。车窗外的移动需要与镜头和身体运动保持一致。这是原创演示，不是真实分析结论。",
            ),
            (
                "91000003",
                "给个人 AI 一本可引用的工具笔记",
                "collection",
                "image",
                "原文、来源与用户备注分别保存。AI 读取片段后引用证据，资料中的命令不能变成执行授权。原创图文示例，没有实际图片上传。",
            ),
            (
                "91000004",
                "轻喜剧的开场：一个小麻烦与一次身份反差",
                "creator",
                "video",
                "主角要做一件简单的事情，却被自己的身份绊住。先出现具体冲突，再交代世界观。原创素材，不代表收藏偏好。",
            ),
            (
                "91000005",
                "把常用教程留在本地：文件、索引与恢复",
                "link",
                "video",
                "Markdown 是内容，索引只帮助查找。保存失败要显示缺口，备份不要包含密钥和平台登录态。原创演示资料。",
            ),
        ]
        for native_id, title, kind, media_type, body in examples:
            item = store.upsert(
                {
                    "native_id": native_id,
                    "title": title,
                    "body": body,
                    "author": "原创演示 · 非真实收藏",
                    "media_type": media_type,
                },
                kind=kind,
                scope_id=f"s_demo_{kind}",
            )["item"]
            if native_id == "91000001":
                store.upsert(
                    {
                        "native_id": native_id,
                        "title": title,
                        "body": body,
                        "author": item["author"],
                        "media_type": media_type,
                    },
                    kind="collection",
                    scope_id="s_demo_ui",
                )
            if native_id in {"91000001", "91000002"}:
                texts = {
                    "audio": "[原创演示转写，不是 ASR 结果]\n\n第一步，列出页面的目标和用户需要完成的操作。\n第二步，明确导航、区块和每个交互状态。\n第三步，再填写视觉约束，保留组件名称与参数。",
                    "screen": "[原创演示画面证据，不是 OCR 结果]\n\n00:20 · 提示词结构\n任务与目标 → 页面与导航 → 组件及交互 → 视觉规则 → 输出要求\n\n示例约束：390 × 844，留白 24 px；空状态、加载、错误提示都要有。",
                    "summary": "[原创演示总结，不是模型输出]\n\n这个示例将提示词分成五个部分：任务目标、页面结构、组件交互、视觉规则、输出要求。具体参数见画面证据。\n\n证据定位：画面 00:20；音频第 1～3 步。不能把归纳模板当作原作者的逐字提示词。",
                }
                store.save_bundle(
                    item["id"],
                    {
                        kind: {
                            "text": text,
                            "processor_version": "original_demo_v1",
                            "coverage": {"synthetic": True, "accuracy": "not_real_extraction"},
                        }
                        for kind, text in texts.items()
                    },
                    expected_content_hash=item["content_hash"],
                )
        FileIndex(store).rebuild()


def app(workspace: Path, origin: str):
    policy = AccessPolicy(
        origin,
        [
            Credential.from_token(
                "p_design_review",
                FIXTURE_TOKEN,
                permissions=frozenset({"collections:read", "ui:view", "ui:manage"}),
            )
        ],
    )
    return create_app(workspace, policy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--server-workspace", type=Path)
    parser.add_argument("--server-origin")
    args = parser.parse_args()
    if args.server_workspace:
        import uvicorn

        uvicorn.run(
            app(args.server_workspace, args.server_origin),
            host="127.0.0.1",
            port=int(args.server_origin.rsplit(":", 1)[1]),
            access_log=False,
            log_level="warning",
        )
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="rednote-ui-", dir=args.output.absolute()))
    workspace = run / "原创演示库"
    seed(workspace)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    process = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--output",
            str(run),
            "--server-workspace",
            str(workspace),
            "--server-origin",
            origin,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    errors, checks, screenshots = [], [], []
    try:
        deadline = time.monotonic() + 25
        opener = build_opener(ProxyHandler({}))
        while True:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Isolated UI server did not start")
            try:
                with opener.open(origin + "/health", timeout=0.5) as response:
                    assert json.load(response)["ok"]
                break
            except (URLError, OSError):
                time.sleep(0.05)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            page.locator("#access-token").fill(FIXTURE_TOKEN)
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator(".item")).to_have_count(5)
            expect(page.locator("#hero-count")).to_have_text("5")
            assert page.locator("#access-token").input_value() == ""
            checks.append("real-session-and-library")
            page.locator(".item").filter(has_text="多页面 UI").click()
            page.get_by_role("button", name="画面", exact=True).click()
            expect(page.locator("#evidence-text")).to_contain_text("390 × 844")
            page.get_by_role("button", name="总结", exact=True).click()
            expect(page.locator("#evidence-text")).to_contain_text("不是模型输出")
            checks.append("select-read-screen-and-summary")
            page.evaluate("document.documentElement.style.scrollBehavior='auto'; window.scrollTo(0,0)")
            page.screenshot(path=run / "desktop-materials.png", full_page=True)
            screenshots.append("desktop-materials.png")
            page.locator("#query").fill("UI 390")
            page.get_by_role("button", name="查找", exact=True).click()
            expect(page.locator(".item")).to_have_count(1)
            page.locator("#source-filter").select_option("liked")
            expect(page.locator(".item")).to_have_count(1)
            page.locator("#query").fill("没有这个关键词的示例")
            page.get_by_role("button", name="查找", exact=True).click()
            expect(page.locator("#items")).to_contain_text("没有匹配资料")
            checks.append("search-filter-and-empty-state")
            for route, page_id in (("connect", "connect"), ("activity", "activity")):
                page.goto(origin + "/" + route)
                expect(page.locator("#" + page_id)).to_be_visible()
                expect(page.locator('nav a[aria-current="page"]')).to_have_count(1)
                if route == "connect":
                    page.locator("#link-url").fill("https://www.douyin.com/video/91000999")
                    page.locator("#link-confirmed").check()
                    page.get_by_role("button", name="登记单条任务", exact=True).click()
                    expect(page.locator("#link-feedback")).to_contain_text("等待已授权的来源后台")
                    page.locator("#creator-url").fill("https://www.douyin.com/user/MS4wLjABAAAAoriginal-demo")
                    page.get_by_role("button", name="保存博主范围", exact=True).click()
                    expect(page.locator(".source-row")).to_have_count(1)
                    checks.append("owner-registers-link-and-source-without-executing")
                else:
                    expect(page.locator(".task-row")).to_have_count(1)
                    page.get_by_role("button", name="取消后续处理", exact=True).click()
                    expect(page.locator(".task-row")).to_contain_text("已取消")
                    checks.append("owner-sees-durable-job-and-cancels")
                page.evaluate("document.documentElement.style.scrollBehavior='auto'; window.scrollTo(0,0)")
                page.screenshot(path=run / f"desktop-{route}.png", full_page=True)
                screenshots.append(f"desktop-{route}.png")
            expect(page.locator("#model-forms fieldset")).to_have_count(3)
            for field in page.locator("#model-forms input[type='password']").all():
                assert field.is_disabled() and field.input_value() == ""
            checks.append("management-pages-disabled-unconfigured-models")
            for width in (412, 768, 1024):
                page.set_viewport_size({"width": width, "height": 915})
                for route in ("", "/connect", "/activity"):
                    page.goto(origin + route)
                    expect(page.locator("#workspace")).to_be_visible()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), (
                        width,
                        route,
                    )
                checks.append(f"no-horizontal-overflow-{width}")
                page.goto(origin)
                page.screenshot(path=run / f"materials-{width}.png", full_page=True)
                screenshots.append(f"materials-{width}.png")
            page.locator("#logout").click()
            expect(page.locator("#login-panel")).to_be_visible()
            expect(page.locator("#hero-count")).to_have_text("—")
            checks.append("logout-clears-library-and-metrics")
            browser.close()
        assert not errors, errors
        report = {
            "origin": origin,
            "run": str(run),
            "checks": checks,
            "js_errors": errors,
            "screenshots": screenshots,
            "fixture_only": True,
            "platform_requests": 0,
            "model_requests": 0,
            "production_changed": False,
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.serve:
            while process.poll() is None:
                time.sleep(0.5)
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
