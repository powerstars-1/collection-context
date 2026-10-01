"""Installed owner configuration -> actual CLI worker against local synthetic HTTP; no real provider."""

from __future__ import annotations

import argparse
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("An independently installed package is required")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="model-setup-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创配置库")
    private = run / "独立私有凭据"
    image = io.BytesIO()
    Image.new("RGB", (80, 80), (227, 222, 211)).save(image, format="PNG")
    item = store.upsert(
        {"native_id": "101", "media_type": "image", "title": "原创模型配置验证"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    PreparedInputs(store).prepare_images(item["id"], [(image.getvalue(), "image/png")])
    registry = AccessRegistry(store)
    owner = registry.create("模型主人", ui=True, manage=True)
    viewer = registry.create("模型只读", ui=True)
    requests: list[dict] = []
    fixture_keys = {
        "original-vision": "synthetic-owner-vision",
        "original-summary": "synthetic-owner-summary",
    }

    class Response(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            model = body["model"]
            authorized = self.headers.get("Authorization") == "Bearer " + fixture_keys.get(model, "invalid")
            requests.append({"model": model, "authorization_matched": authorized, "endpoint": self.path})
            if not authorized or self.path != "/v1/chat/completions":
                self.send_error(403)
                return
            value = json.dumps(
                {
                    "model": model,
                    "choices": [
                        {
                            "message": {"content": "原创协议样例[f_000000]，非实际识别结果。"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 7},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Response)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    endpoint = f"http://127.0.0.1:{fixture.server_port}/v1"
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
            "--allow-model-config",
            "--credential-dir",
            str(private),
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
                raise RuntimeError("Owner setup server did not start")
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
            page.on("request", lambda request: urls.append(request.url))
            page.goto(origin + "/activity")
            page.locator("#access-token").fill(viewer["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#model-state")).to_contain_text("只读访问")
            expect(page.locator("#model-forms form")).to_have_count(0)
            page.get_by_role("button", name="退出", exact=True).click()
            page.locator("#access-token").fill(owner["token"])
            page.get_by_role("button", name="进入资料库", exact=True).click()
            expect(page.locator("#model-forms form")).to_have_count(3)
            expect(page.locator("#model-state")).to_contain_text("私有文件，尚未加密")
            names = {"audio": "音频转写", "vision": "画面与图片", "summary": "内容总结"}
            for role in names:
                page.locator(f"#model-{role}-url").fill(endpoint)
                page.locator(f"#model-{role}-name").fill("original-" + role)
                page.locator(f"#model-{role}-key").fill("synthetic-owner-" + role)
                page.locator(f"#model-{role}-confirmed").check()
                with page.expect_response(
                    lambda response: response.url == origin + "/v1/management/model-save"
                ) as saved_response:
                    page.get_by_role("button", name="保存" + names[role], exact=True).click()
                assert saved_response.value.json()["ok"]
                expect(page.locator("#model-feedback")).to_contain_text("配置已保存")
                expect(page.locator(f"#model-{role}-key")).to_have_value("")
                expect(page.locator(f".model-card[data-role='{role}']")).to_contain_text("已登记")
                assert not requests and not store.snapshot()["jobs"]
            page.get_by_role("button", name="刷新配置", exact=True).click()
            expect(page.locator("#model-audio-name")).to_have_value("original-audio")
            expect(page.locator("#model-audio-protocol")).to_have_value("chat_audio")
            page.locator("#prepared-items input").check()
            page.locator("#history-fee").check()
            page.get_by_role("button", name="提交所选历史批次", exact=True).click()
            expect(page.locator(".task-row")).to_contain_text("已排队")
            assert len(store.snapshot()["jobs"]) == 1
            fixed_job = next(iter(store.snapshot()["jobs"].values()))
            original_profiles = dict(store.snapshot()["settings"]["model_roles"])
            page.locator("#model-vision-name").fill("changed-default-vision")
            page.locator("#model-vision-key").fill("synthetic-rotated-vision")
            page.locator("#model-vision-confirmed").check()
            with page.expect_response(
                lambda response: response.url == origin + "/v1/management/model-save"
            ) as rotated_response:
                page.get_by_role("button", name="保存画面与图片", exact=True).click()
            assert rotated_response.value.json()["ok"]
            expect(page.locator("#model-vision-name")).to_have_value("changed-default-vision")
            assert store.snapshot()["jobs"][fixed_job["id"]]["payload"] == fixed_job["payload"]
            assert not requests
            worker = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "collection_context.cli",
                    "--workspace",
                    str(store.files.root),
                    "worker",
                    "--credential-dir",
                    str(private),
                    "--allow-model-calls",
                    "--once",
                    "--max-jobs",
                    "10",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            response = json.loads(worker.stdout.strip().splitlines()[-1])
            assert response["ok"] and response["data"]["handled"] == 1
            assert [r["model"] for r in requests] == ["original-vision", "original-summary"]
            assert all(r["authorization_matched"] for r in requests)
            assert store.snapshot()["jobs"][fixed_job["id"]]["state"] == "succeeded"
            page.get_by_role("button", name="刷新状态", exact=True).click()
            expect(page.locator(".task-row")).to_contain_text("完成")
            page.screenshot(path=run / "desktop-models.png", full_page=True, animations="disabled")
            page.set_viewport_size({"width": 412, "height": 915})
            overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not overflow
            page.screenshot(path=run / "mobile-models.png", full_page=True, animations="disabled")
            page.locator("#model-summary-key").fill("synthetic-discard-on-logout")
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#model-forms form")).to_have_count(0)
            assert page.evaluate("document.body.textContent.includes('synthetic-owner-')") is False
            context.close()
            browser.close()
        state = store.snapshot()
        assert "synthetic-owner-" not in json.dumps(state)
        assert "synthetic-rotated-" not in json.dumps(state)
        assert all(
            "synthetic-owner-" not in file.read_text(errors="replace")
            for file in store.files.root.rglob("*")
            if file.is_file()
        )
        assert len(list(private.iterdir())) == 4
        assert ModelCatalog(store).get(original_profiles["vision"])["model"] == "original-vision"
        assert not errors and all(url.startswith(origin + "/") for url in urls)
        report = {
            "result": "passed",
            "runtime_module": collection_context.__file__,
            "artifact_dir": str(run),
            "javascript_errors": len(errors),
            "mobile_horizontal_overflow": overflow,
            "configured_roles": ["audio", "vision", "summary"],
            "model_requests_during_setup": 0,
            "local_synthetic_model_requests": len(requests),
            "real_cloud_requests": 0,
            "real_platform_requests": 0,
            "old_pinned_model_and_key_preserved": True,
            "private_file_count": len(list(private.iterdir())),
            "credential_store_encrypted": False,
            "quality_verified": False,
            "verified": [
                "owner_cookie_only",
                "three_role_forms",
                "keys_never_read_back",
                "setup_zero_calls",
                "refresh_saved_configuration",
                "private_store_outside_library",
                "rotate_does_not_mutate_fixed_job",
                "installed_cli_worker",
                "local_http_authorization",
                "desktop_mobile",
                "logout_clears_unsaved_key",
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
            raise RuntimeError("Owner setup server did not stop") from None
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=5)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
