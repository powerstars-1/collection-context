"""Original Creator task controls against authenticated local HTTP, no external requests."""

import os
import socket
import threading
import time

import pytest
from test_context_model_recovery import interrupted
from test_context_scheduling import env as env

from collection_context.infrastructure.web_runtime import web_runtime_options
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential


@pytest.mark.skipif(os.environ.get("RUN_LOCAL_SEARCH_UI") != "1", reason="explicit local browser acceptance")
def test_actual_creator_task_unknown_review_and_retry_at_two_widths(env, monkeypatch, tmp_path):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    original, vision, sent = interrupted(env, monkeypatch)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    token = "original-recovery-fixture-" + "r" * 40
    policy = AccessPolicy(
        origin,
        [
            Credential.from_token(
                "p_owner_original", token, permissions=frozenset({"collections:read", "ui:view", "ui:manage"})
            )
        ],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(env[0].files.root, policy),
            host="127.0.0.1",
            port=listener.getsockname()[1],
            **web_runtime_options(),
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=False)
    errors, outside, submitted = [], [], []
    thread.start()
    try:
        deadline = time.monotonic() + 5
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

                def route(value):
                    if value.request.url.startswith(origin + "/"):
                        value.continue_()
                    else:
                        outside.append(value.request.url)
                        value.abort()

                context.route("**/*", route)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda request: (
                        submitted.append(request) if request.url.endswith("/v1/management/retry") else None
                    ),
                )
                page.goto(origin + "/activity")
                page.get_by_label("产品访问口令").fill(token)
                page.get_by_role("button", name="进入收藏库", exact=True).click()
                for width in (1365, 412):
                    page.set_viewport_size({"width": width, "height": 915})
                    page.goto(origin + "/activity")
                    expect(page.locator("#auto-calls")).to_have_value("")
                    expect(page.locator("#history-calls")).to_have_value("")
                    card = page.locator("#task-list article").filter(has_text="媒体提取 · " + original["id"])
                    card.get_by_role("button", name="核对并选择重试阶段", exact=True).click()
                    card.get_by_label(vision + " · 需人工处理", exact=True).check()
                    card.get_by_role("button", name="预览所选阶段", exact=True).click()
                    create = card.get_by_role("button", name="创建新尝试", exact=True)
                    expect(create).to_be_visible()
                    create.click()
                    expect(card).to_contain_text("请填写足够的明确上限")
                    assert len(submitted) == (0 if width == 1365 else 1)
                    card.get_by_label("本次最多云请求次数", exact=True).fill("2")
                    card.get_by_label(
                        "确认本次范围、媒体上传和请求上限；不会自动补额或无限重试。", exact=True
                    ).check()
                    create.click()
                    assert len(submitted) == (0 if width == 1365 else 1)
                    card.get_by_label("已核对仍需重试，我接受本次可能重复计费。", exact=True).check()
                    card.get_by_label("我已核对 " + vision, exact=False).check()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    assert card.evaluate("node => node.scrollWidth <= node.clientWidth")
                    assert card.locator("label").evaluate_all(
                        "nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"
                    )
                    page.screenshot(
                        path=str(tmp_path / f"recovery-{width}.png"), full_page=True, animations="disabled"
                    )
                    with page.expect_response(
                        lambda response: response.url.endswith("/v1/management/retry")
                    ) as response:
                        create.click()
                    assert response.value.json()["ok"]
                    new_id = response.value.json()["data"]["job_id"]
                    expect(page.locator("#management-feedback")).to_contain_text(new_id)
                    current = env[3].executor.jobs.get(new_id)
                    assert current["state"] == "queued" and current["calls"] == []
                    assert current["recovery"]["selected_stages"] == [vision]
                    env[3].executor.jobs.cancel(
                        new_id
                    )  # Only this test's own new attempt, so next width can retry.
                    assert env[3].executor.jobs.get(original["id"])["calls"] == original["calls"]
                assert len(sent) == 1 and not outside and not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
