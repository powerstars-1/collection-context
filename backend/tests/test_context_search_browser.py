"""Explicit local browser acceptance; no platform, provider or personal library."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import threading
import time

import pytest

from collection_context.infrastructure.web_runtime import web_runtime_options
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.store import LibraryStore


@pytest.mark.skipif(os.environ.get("RUN_LOCAL_SEARCH_UI") != "1", reason="explicit local browser acceptance")
def test_actual_library_search_continuation_and_changed_version(tmp_path):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    token = "original-search-ui-fixture-" + "r" * 40
    store = LibraryStore.initialize(tmp_path / "原创23条分页测试库")
    items = [
        store.upsert(
            {"native_id": str(100 + number), "title": f"分页原创教程 {number:02d}", "body": "只用于页面验收"},
            kind="saved",
            scope_id="s_saved",
        )["item"]
        for number in range(23)
    ]

    def inventory():
        return {
            str(path.relative_to(store.files.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in store.files.root.rglob("*")
            if path.is_file()
        }

    before = inventory()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    policy = AccessPolicy(
        origin,
        [
            Credential.from_token(
                "p_original_ui",
                token,
                permissions=frozenset({"collections:read", "ui:view"}),
            )
        ],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store.files.root, policy),
            host="127.0.0.1",
            port=listener.getsockname()[1],
            **web_runtime_options(),
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=False)
    errors = []
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
                context = browser.new_context(locale="zh-CN")
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_() if route.request.url.startswith(origin + "/") else route.abort()
                    ),
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.get_by_label("产品访问口令").fill(token)
                page.get_by_role("button", name="进入收藏库", exact=True).click()
                for width in (1365, 412):
                    page.set_viewport_size({"width": width, "height": 915})
                    page.goto(origin + "/?q=分页原创教程")
                    cards = page.get_by_role("button", name=re.compile("分页原创教程"))
                    expect(cards).to_have_count(20)
                    more = page.get_by_role("button", name="继续查看", exact=True)
                    expect(more).to_be_visible()
                    with page.expect_response(
                        lambda response: response.url.endswith("/v1/collections/search")
                    ) as sent:
                        more.click()
                    arguments = sent.value.request.post_data_json
                    assert arguments["offset"] == 20 and len(arguments["version"]) == 64
                    expect(cards).to_have_count(23)
                    expect(more).to_have_count(0)
                    assert len(set(cards.all_text_contents())) == 23
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert inventory() == before  # Search and both page reads did not change the library.
                page.goto(origin + "/?q=分页原创教程")
                expect(cards).to_have_count(20)
                store.exclude(items[0]["id"])
                more.click()
                expect(page.get_by_role("alert")).to_contain_text("搜索结果或条件已变化")
                expect(cards).to_have_count(20)  # The changed page was not appended.
                page.get_by_role("button", name="搜索收藏", exact=True).click()
                expect(page.get_by_role("alert")).to_have_count(0)
                expect(cards).to_have_count(20)
                more.click()
                expect(cards).to_have_count(22)
                expect(more).to_have_count(0)
            finally:
                browser.close()
        assert not errors
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        store.close()
        assert not thread.is_alive()
