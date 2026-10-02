"""Actual Creator UI reads an original legacy fixture without write or network lanes."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit

import pytest

from collection_context.infrastructure.web_runtime import web_runtime_options
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.store import LibraryStore


def inventory(root):
    return {
        str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.skipif(os.environ.get("RUN_LOCAL_SEARCH_UI") != "1", reason="explicit local browser acceptance")
@pytest.mark.parametrize("width", [1365, 412])
def test_actual_legacy_search_read_and_deep_link_are_readonly(tmp_path, width):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    root = tmp_path / "原创旧资料"
    vault = root / "content-vault"
    cards = vault / "00_素材收件箱" / "抖音"
    attachment = vault / "80_附件" / "抖音" / "713_原创教程"
    cards.mkdir(parents=True)
    attachment.mkdir(parents=True)
    card = cards / "原创纸飞机教程.md"
    original = "原创教程：折纸飞机时，先对齐中心线，再折叠两侧机翼。"
    note = "我的备注：周末试试蓝色纸张。<script>window.legacyNoteExecuted=true</script>"
    screen = "画面提示词\n机翼角度 15 度；配色 Cerulean；保留对称折线。\n先确定机身，再调整机翼，最后检查左右平衡。"
    card.write_text(
        "# 原创纸飞机教程\n\n## 基本信息\n- 平台: 抖音\n- 来源: 收藏\n"
        "- 作者: 原创测试作者\n- 链接: https://www.douyin.com/video/713\n"
        "- 附件目录: 80_附件/抖音/713_原创教程\n\n"
        f"## 原始材料\n{original}\n\n## 用户备注\n{note}\n",
        encoding="utf-8",
    )
    (attachment / "画面文字.md").write_text(screen, encoding="utf-8")
    (attachment / "音频转写.md").write_text("原创讲解：中心线要对齐。", encoding="utf-8")
    (attachment / "内容总结.md").write_text("原创总结：对齐、折叠、检查。", encoding="utf-8")
    (root / ".env.local").write_text("PRIVATE_SENTINEL=not-part-of-library\n", encoding="utf-8")
    relative = card.relative_to(vault).as_posix()
    reference = "m1:" + base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
    before = inventory(root)
    authority = tmp_path / "独立访问配置"
    store = LibraryStore.initialize(authority)
    store.close()
    token = "original-legacy-ui-fixture-" + "r" * 40
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    policy = AccessPolicy(
        origin,
        [
            Credential.from_token(
                "p_legacy_owner",
                token,
                permissions=frozenset({"collections:read", "collections:add", "ui:view", "ui:manage"}),
            )
        ],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(authority, policy, legacy_vault=root),
            host="127.0.0.1",
            port=listener.getsockname()[1],
            **web_runtime_options(),
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=False)
    errors, requested, external = [], [], []
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with sync_playwright() as playwright:
            binary = os.environ.get("COLLECTION_CONTEXT_TEST_BROWSER_BINARY")
            browser = playwright.chromium.launch(
                headless=True, **({"executable_path": binary} if binary else {})
            )
            try:
                context = browser.new_context(locale="zh-CN", viewport={"width": width, "height": 915})

                def route_request(route):
                    if route.request.url.startswith(origin + "/"):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()

                context.route("**/*", route_request)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requested.append(urlsplit(request.url).path))
                page.goto(origin)
                page.get_by_label("产品访问口令").fill(token)
                with page.expect_response(
                    lambda response: (
                        response.url.endswith("/v1/session") and response.request.method == "POST"
                    )
                ) as login:
                    page.get_by_role("button", name="进入收藏库", exact=True).click()
                session = login.value.json()["data"]
                assert session["library_mode"] == "legacy_readonly"
                assert set(session["permissions"]) == {"collections:read", "ui:view"}
                expect(page.get_by_text("旧资料库 · 只读浏览", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="收藏库", exact=True)).to_be_visible()
                page.get_by_label("搜索已有资料").fill("机翼 Cerulean")
                page.get_by_role("button", name="搜索收藏", exact=True).click()
                selected = page.get_by_role("button", name=re.compile("原创纸飞机教程"))
                expect(selected).to_have_count(1)
                selected.click()
                expect(selected).to_have_attribute("aria-pressed", "true")
                expect(page.locator("#evidence-text")).to_have_text(original)
                page.get_by_role("tab", name="画面文字", exact=True).click()
                expect(page.locator("#evidence-text")).to_have_text(screen)
                expect(page.get_by_role("alert")).to_have_count(0)
                expect(page.get_by_role("region", name="资料管理", exact=True)).to_have_count(0)
                expect(page.get_by_role("button", name="仅更新总结", exact=True)).to_have_count(0)
                expect(page.get_by_role("button", name="核对外部编辑", exact=True)).to_have_count(0)
                expect(page.get_by_role("button", name="排除资料", exact=True)).to_have_count(0)
                page.locator("#evidence-text").scroll_into_view_if_needed()
                expect(page.locator("#evidence-text")).to_be_in_viewport(ratio=1)
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(tmp_path / f"legacy-screen-{width}.png"), full_page=True)
                page.get_by_role("tab", name="备注", exact=True).click()
                expect(page.locator("#evidence-text")).to_have_text(note)
                page.locator("#evidence-text").scroll_into_view_if_needed()
                expect(page.locator("#evidence-text")).to_be_in_viewport(ratio=1)
                assert page.evaluate("window.legacyNoteExecuted === undefined")
                page.goto(origin + "/?ref=" + quote(reference, safe=""))
                expect(page.locator("#evidence-text")).to_have_text(original)
                expect(page.get_by_role("heading", name="原创纸飞机教程", exact=True)).to_be_visible()
                page.get_by_role("tab", name="画面文字", exact=True).click()
                expect(page.locator("#evidence-text")).to_have_text(screen)
                expect(
                    page.get_by_text("旧库模式提供已有文字读取；原始媒体和关键帧保留在原目录。", exact=True)
                ).to_be_visible()
                page.locator("#evidence-text").scroll_into_view_if_needed()
                expect(page.locator("#evidence-text")).to_be_in_viewport(ratio=1)
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(tmp_path / f"legacy-deep-link-{width}.png"), full_page=True)
                page.goto(origin + "/access")
                expect(page.get_by_role("heading", name="把收藏交给你的 AI。", exact=True)).to_be_visible()
                cli_example = page.locator("pre").filter(has_text="collection-context --workspace")
                expect(cli_example).to_contain_text('--workspace "<旧资料目录路径>" --legacy-vault')
                mcp_example = page.locator("pre").filter(has_text="collection-context-mcp")
                expect(mcp_example).to_contain_text('--workspace "<旧资料目录路径>" --legacy-vault')
                cli_example.scroll_into_view_if_needed()
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(tmp_path / f"legacy-access-{width}.png"), full_page=True)
                page.goto(origin + "/connect")
                expect(page.get_by_role("heading", name="当前连接旧资料库", exact=True)).to_be_visible()
                expect(
                    page.get_by_text(
                        "此连接支持浏览、搜索和 AI 读取。旧库没有本产品的同步任务、模型配置或处理记录。",
                        exact=True,
                    )
                ).to_be_visible()
                expect(page.get_by_role("link", name="返回收藏库", exact=True)).to_be_visible()
                expect(page.get_by_role("alert")).to_have_count(0)
                assert page.locator("[data-management-root]").count() == 0
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(tmp_path / f"legacy-connect-{width}.png"), full_page=True)
                assert inventory(root) == before
                assert not external
                assert not errors
                assert not any(
                    "/management/" in path or path.endswith("/frames") or "/jobs/" in path
                    for path in requested
                )
                assert all(
                    path in {"/", "/access", "/connect"}
                    or path.startswith("/assets/")
                    or path == "/v1/session"
                    or path.startswith("/v1/collections/")
                    for path in requested
                )
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        assert not thread.is_alive()
