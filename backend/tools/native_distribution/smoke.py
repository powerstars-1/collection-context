"""Actual native CLI, stdio and HTTP smoke in a fresh external synthetic library.

No browser interaction, platform requests, cloud calls or personal credentials.
The host Python only prepares original fixture data and acts as the MCP client;
all product requests are handled by the standalone frozen executable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


def native_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "",
        "HOME": str(home),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }


def run(binary: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(binary), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        # CLI access-key output is intentionally never echoed into a failure log.
        raise RuntimeError("Native child failed; output is retained only in process memory")
    return result.stdout


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def exercise_http(
    binary: Path, workspace: Path, token: str, cwd: Path, env: dict[str, str], *, mode: str = "web"
) -> dict:
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    args = (
        ["web", "--workspace", str(workspace), "--origin", origin]
        if mode == "web"
        else ["launch", "--workspace", str(workspace), "--port", str(port), "--no-browser"]
    )
    process = subprocess.Popen(
        [str(binary), *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Native HTTP process exited before ready")
            try:
                with urllib.request.urlopen(origin + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            raise TimeoutError("Native HTTP readiness was not proven within 30 seconds")
        try:
            urllib.request.urlopen(origin + "/v1/collections/overview", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("Unauthenticated overview unexpectedly allowed")
        cookies = CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        login = urllib.request.Request(
            origin + "/v1/session",
            data=json.dumps({"token": token}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        with client.open(login, timeout=3) as response:
            result = json.load(response)
        assert result["ok"] and "ui:manage" in result["data"]["permissions"]
        with client.open(origin + "/v1/collections/overview", timeout=3) as response:
            overview = json.load(response)
        assert overview["ok"]
        for route in ("/", "/settings", "/access"):
            with client.open(origin + route, timeout=3) as response:
                assert b'id="root"' in response.read()
                assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        with client.open(origin + "/assets/app.js", timeout=3) as response:
            assert response.status == 200 and len(response.read()) > 100_000
        with client.open(origin + "/assets/app.css", timeout=3) as response:
            assert response.status == 200 and len(response.read()) > 10_000
        return {"health": True, "unauthenticated_denied": True, "cookie_login": True, "react_assets": True}
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("Native HTTP process required forced kill") from None
        if mode == "launch" and process.returncode != 0:
            raise RuntimeError("Native terminal launcher did not stop cleanly")


async def exercise_mcp(binary: Path, workspace: Path, env: dict[str, str]) -> dict:
    parameters = StdioServerParameters(
        command=str(binary),
        args=["mcp", "--workspace", str(workspace)],
        env=env,
    )
    async with asyncio.timeout(20):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {
                    "search_collections",
                    "read_collection",
                    "collection_status",
                }
                assert all(tool.annotations.read_only_hint for tool in tools)
                found = await session.call_tool("search_collections", {"query": "原生 回归"})
                assert not found.is_error
                data = found.structured_content["data"]
                assert data["model_requests"] == 0
                ref = data["items"][0]["material_ref"]
                read = await session.call_tool("read_collection", {"material_ref": ref, "artifact": "screen"})
                assert read.structured_content["data"]["text"] == "原创原生画面：390×844；无模型请求。"
                denied = await session.call_tool("read_collection", {"material_ref": "../../.env"})
                assert denied.is_error
                return {
                    "initialize": True,
                    "three_readonly_tools": True,
                    "evidence_read": True,
                    "path_denied": True,
                }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="native-smoke-", dir=output))
    original = args.binary.absolute()
    if not original.is_file():
        raise ValueError("Expected native executable")
    relocated = stage / "异路径 原生候选"
    shutil.copytree(original.parent, relocated, symlinks=True)
    binary = relocated / original.name
    home = stage / "isolated-home"
    home.mkdir()
    env = native_env(home)
    workspace = stage / "原创 资料库"
    help_output = run(binary, [], cwd=stage, env=env)
    assert "cli|mcp|web|launch" in help_output and not workspace.exists()
    run(binary, ["cli", "--workspace", str(workspace), "init"], cwd=stage, env=env)
    store = LibraryStore(workspace)
    try:
        item = store.upsert(
            {"native_id": "61", "title": "原创原生回归", "body": "这是原创固定样例，不是真实收藏。"},
            kind="saved",
            scope_id="s_saved",
        )["item"]
        store.save_artifact(
            item["id"],
            "screen",
            "原创原生画面：390×844；无模型请求。",
            processor_version="native_fixture_v1",
            expected_content_hash=item["content_hash"],
        )
        FileIndex(store).rebuild()
    finally:
        store.close()
    key_response = json.loads(
        run(
            binary,
            [
                "cli",
                "--workspace",
                str(workspace),
                "access-key",
                "--name",
                "原创原生测试",
                "--ui",
                "--manage",
            ],
            cwd=stage,
            env=env,
        )
    )
    token = key_response["data"]["token"]
    before = files(workspace)
    found = json.loads(
        run(
            binary,
            ["cli", "--workspace", str(workspace), "search", "--query", "原生 回归"],
            cwd=stage,
            env=env,
        )
    )
    assert found["ok"] and len(found["data"]["items"]) == 1
    mcp = asyncio.run(exercise_mcp(binary, workspace, env))
    http = exercise_http(binary, workspace, token, stage, env)
    launch = exercise_http(binary, workspace, token, stage, env, mode="launch")
    assert files(workspace) == before
    report = {
        "passed": True,
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "native_cli": True,
        "native_mcp": mcp,
        "native_http": http,
        "native_terminal_launcher": launch,
        "relocation": "Chinese and spaces, outside source and build environment",
        "child_path": env["PATH"],
        "no_user_python_or_node_on_path": True,
        "library_unchanged_by_read": True,
        "platform_requests": 0,
        "model_requests": 0,
        "real_credentials": 0,
        "private_library_access": 0,
        "native_children_stopped": True,
        "not_verified": [
            "GUI token display",
            "desktop application",
            "Windows",
            "Linux",
            "source browser",
            "FFmpeg",
            "OCR",
        ],
    }
    destination = stage / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(destination)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
