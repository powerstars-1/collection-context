"""Installed authenticated HTTP/MCP -> own durable queue -> separate browser worker.

Original intercepted site fixtures only; no real login, media download or model request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import collection_context
from collection_context.cli import main as cli_main
from collection_context.infrastructure.browser import BrowserSession
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


def worker(root: Path, profile: Path, crash: bool) -> int:
    enter, finish = BrowserSession.__enter__, JobManager.finish

    def route(value):
        path = urlsplit(value.request.url).path
        if path == "/video/81":
            value.fulfill(
                status=200,
                content_type="text/html",
                body="<script>fetch('/aweme/v1/web/aweme/detail/')</script>",
            )
        elif path == "/aweme/v1/web/aweme/detail/":
            value.fulfill(
                status=200,
                json={
                    "status_code": 0,
                    "aweme_detail": {
                        "aweme_id": "81",
                        "desc": "原创AI添加样例：仅保存完整原文，不下载、不调用模型。",
                        "create_time": 1700000000,
                        "author": {"nickname": "原创验收作者", "sec_uid": "MS4w-fiction"},
                        "video": {
                            "play_addr": {
                                "url_list": ["https://fiction.douyinvod.com/video?signature=never-persist"]
                            }
                        },
                    },
                },
            )
        else:
            value.abort()

    def opened(self):
        result = enter(self)
        self.context.route("**/*", route)
        return result

    def completed(self, ref, state_name, **kwargs):
        if crash and state_name == "succeeded":
            os._exit(17)  # Browser already closed; stage committed, final status not committed.
        return finish(self, ref, state_name, **kwargs)

    BrowserSession.__enter__, JobManager.finish = opened, completed
    return cli_main(
        [
            "--workspace",
            str(root),
            "worker",
            "--allow-source-sync",
            "--browser-dir",
            str(profile),
            "--once",
            "--max-jobs",
            "5",
        ]
    )


async def protocol(
    root: Path, token: str, job: str, foreign: str, registry: AccessRegistry, principal: str
) -> dict:
    base = ["-B", "-m", "collection_context.interfaces.mcp", "--workspace", str(root)]
    env = {**os.environ, "COLLECTION_CONTEXT_ACCESS_TOKEN": token}
    env.pop("PYTHONPATH", None)
    default = StdioServerParameters(command=sys.executable, args=base, env=env)
    async with stdio_client(default) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert {t.name for t in (await session.list_tools()).tools} == {
            "search_collections",
            "read_collection",
            "collection_status",
        }
    optional = StdioServerParameters(command=sys.executable, args=[*base, "--allow-add"], env=env)
    async with stdio_client(optional) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert len(tools) == 5 and any(
            t.name == "add_collection" and not t.annotations.read_only_hint for t in tools
        )
        own = await session.call_tool("get_job", {"job_id": job})
        assert own.structured_content["data"]["state"] == "succeeded"
        denied = await session.call_tool("get_job", {"job_id": foreign})
        assert denied.is_error and denied.structured_content["error"]["code"] == "not_found"
        illegal = await session.call_tool(
            "add_collection",
            {"url": "https://www.douyin.com/video/81", "idempotency_key": "extra", "download": True},
        )
        assert illegal.is_error and illegal.structured_content["error"]["code"] == "invalid_argument"
        retry = await session.call_tool(
            "add_collection", {"url": "https://www.douyin.com/video/81", "idempotency_key": "shared"}
        )
        assert retry.structured_content["data"]["job_id"] == job
        found = await session.call_tool("search_collections", {"query": "原创AI添加"})
        ref = found.structured_content["data"]["items"][0]["material_ref"]
        evidence = await session.call_tool("read_collection", {"material_ref": ref})
        assert "完整原文" in evidence.structured_content["data"]["text"]
        registry.revoke(principal)
        revoked = await session.call_tool(
            "add_collection", {"url": "https://www.douyin.com/video/81", "idempotency_key": "after-revoke"}
        )
        assert revoked.is_error and revoked.structured_content["error"]["code"] == "permission_denied"
    return {
        "default_tools": 3,
        "authorized_tools": 5,
        "mcp_raw_extra_rejected": True,
        "mcp_own_job_only": True,
        "mcp_live_revocation": True,
        "mcp_reads_saved_evidence": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-root", type=Path)
    parser.add_argument("--worker-profile", type=Path)
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    if "site-packages" not in str(collection_context.__file__):
        raise RuntimeError("Proof requires a non-editable installation outside the repository")
    if args.worker_root:
        return worker(args.worker_root, args.worker_profile, args.crash)
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="agent-addition-", dir=args.output.absolute()))
    store = LibraryStore.initialize(run / "原创AI添加资料库")
    registry = AccessRegistry(store)
    read = registry.create("只读")
    first, second = registry.create("AI甲", add=True), registry.create("AI乙", add=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    server = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "collection_context.interfaces.server",
            "--workspace",
            str(store.files.root),
            "--origin",
            origin,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    opener = build_opener(ProxyHandler({}))

    def request(path, token, body=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        raw = json.dumps(body).encode() if body is not None else None
        req = Request(origin + path, data=raw, headers=headers)
        try:
            with opener.open(req, timeout=15) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def execute(crash=False):
        cmd = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--worker-root",
            str(store.files.root),
            "--worker-profile",
            str(run / "独立浏览器"),
        ]
        if crash:
            cmd.append("--crash")
        return subprocess.run(cmd, env=env, capture_output=True, timeout=90).returncode

    try:
        until = time.monotonic() + 15
        while True:
            try:
                with opener.open(origin + "/health", timeout=2) as response:
                    assert response.status == 200
                break
            except (URLError, OSError):
                if time.monotonic() > until:
                    raise RuntimeError("Isolated server did not start") from None
                time.sleep(0.1)
        payload = {"url": "https://www.douyin.com/video/81", "idempotency_key": "shared"}
        assert request("/v1/collections", read["token"], payload)[0] == 403
        code, result = request("/v1/collections", first["token"], payload)
        assert code == 202 and result["ok"]
        job = result["data"]["job_id"]
        assert request("/v1/collections", first["token"], payload)[1]["data"]["job_id"] == job
        foreign = request(
            "/v1/collections", second["token"], {**payload, "url": "https://www.douyin.com/video/82"}
        )[1]["data"]["job_id"]
        assert request("/v1/jobs/" + foreign, first["token"])[0] == 404
        registry.revoke(second["principal"])
        assert execute(crash=True) == 17
        assert execute() == 0
        own = request("/v1/jobs/" + job, first["token"])[1]["data"]
        assert own["state"] == "succeeded" and own["model_requests"] == 0
        blocked = JobManager(store).get(foreign, principal=second["principal"])
        assert blocked["state"] == "blocked" and blocked["error"]["code"] == "permission_denied"
        proof = asyncio.run(
            protocol(store.files.root, first["token"], job, foreign, registry, first["principal"])
        )
        assert request("/v1/jobs/" + job, first["token"])[0] == 401
        snapshot = store.snapshot()
        assert len(snapshot["items"]) == 1 and len(snapshot["jobs"]) == 2
        assert next(iter(snapshot["items"].values()))["first_observed_principal"] == first["principal"]
        assert not snapshot.get("automatic_admissions") and not snapshot.get("prepared_inputs")
        assert not any(j["calls"] for j in snapshot["jobs"].values())
        assert "never-persist" not in json.dumps(snapshot)
        server.terminate()
        server.wait(timeout=10)
        report = {
            "status": "passed",
            "module": str(collection_context.__file__),
            "http_read_only_denied": True,
            "http_submission_202": True,
            "http_same_key_same_job": True,
            "foreign_job_denied": True,
            "revoked_queued_blocked": True,
            "worker_real_exit": 17,
            "fresh_worker_recovered": True,
            "model_requests": 0,
            "real_platform_requests": 0,
            "real_media_downloads": 0,
            "fictional_site_interception": True,
            "jobs": 2,
            "items": 1,
            "production_changed": False,
            "children_terminated": True,
            **proof,
        }
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(run / "report.json"), **report}, ensure_ascii=False))
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        server.communicate()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
