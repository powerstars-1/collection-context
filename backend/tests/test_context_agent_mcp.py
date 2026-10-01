"""Actual original stdio/CLI processes; no platform login, media or model calls."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from collection_context.application.agent_addition import AgentAdditionGateway
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.jobs import JobManager

TOKEN_ENV = "COLLECTION_CONTEXT_ACCESS_TOKEN"
READ_TOOLS = {"search_collections", "read_collection", "collection_status"}
ADD_TOOLS = READ_TOOLS | {"add_collection", "get_job"}
URL = "https://www.douyin.com/video/81"
SRC = str(Path(__file__).parents[1] / "src")


@pytest.fixture
def agent_library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "原创受限接入样例库")
    item = store.upsert(
        {"native_id": "73", "title": "原创协议权限样例", "body": "仅用于离线验证，不是真实同步"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "原创画面文字：春天的工具箱。",
        processor_version="fixture_v1",
        expected_content_hash=item["content_hash"],
    )
    FileIndex(store).rebuild()
    registry = AccessRegistry(store)
    credential = registry.create("原创协议 AI", add=True)
    try:
        yield store, registry, credential
    finally:
        store.close()


def process_env(token=None):
    # Isolated child imports only our module, never old login/provider configuration.
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop(TOKEN_ENV, None)
    if token is not None:
        env[TOKEN_ENV] = token
    return env


@asynccontextmanager
async def stdio_session(store, *, token=None, allow_add=False):
    args = ["-B", "-m", "collection_context.interfaces.mcp", "--workspace", str(store.files.root)]
    if allow_add:
        args.append("--allow-add")
    parameters = StdioServerParameters(command=sys.executable, args=args, env=process_env(token))
    async with asyncio.timeout(20):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session


def body(result):
    value = result.structured_content or json.loads(result.content[0].text)
    assert value["ok"] is (not result.is_error)
    return value


def assert_zero_fee_jobs(store):
    for job in store.snapshot()["jobs"].values():
        assert job["calls"] == []
        assert job["budget"] == {"max_calls": 0}
        assert job["state"] == "queued"


def test_actual_stdio_default_stays_three_readonly_even_with_add_token(agent_library):
    store, _, credential = agent_library
    before = store.snapshot()

    async def run():
        async with stdio_session(store, token=credential["token"]) as session:
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == READ_TOOLS
            assert all(tool.annotations.read_only_hint for tool in tools)
            assert all(not tool.annotations.destructive_hint for tool in tools)
            found = body(await session.call_tool("search_collections", {"query": "春天"}))
            assert found["data"]["model_requests"] == 0
            ref = found["data"]["items"][0]["material_ref"]
            read = body(
                await session.call_tool("read_collection", {"material_ref": ref, "artifact": "screen"})
            )
            assert "春天的工具箱" in read["data"]["text"]
            denied = await session.call_tool("add_collection", {"url": URL, "idempotency_key": "none"})
            assert denied.is_error

    asyncio.run(run())
    assert store.snapshot() == before


@pytest.mark.parametrize("credential_kind", ["missing", "readonly", "invalid"])
def test_actual_stdio_explicit_add_requires_valid_independent_permission(agent_library, credential_kind):
    store, registry, credential = agent_library
    token = None
    if credential_kind == "readonly":
        token = registry.create("原创只读 AI")["token"]
    elif credential_kind == "invalid":
        token = credential["token"] + "invalid"
    before = store.snapshot()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "collection_context.interfaces.mcp",
            "--workspace",
            str(store.files.root),
            "--allow-add",
        ],
        env=process_env(token),
        input="",
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        "permission_denied" if credential_kind == "readonly" else "authentication_required"
    ) in result.stderr
    assert credential["token"] not in result.stderr
    if token:
        assert token not in result.stderr
    assert store.snapshot() == before


def test_actual_stdio_add_is_idempotent_identity_bound_and_zero_fee(agent_library):
    store, registry, credential = agent_library
    foreign = registry.create("另一原创 AI", add=True)
    foreign_job = AgentAdditionGateway(
        AdditionWorkflow(store, agent_authority=registry.authorize_add), foreign["principal"]
    ).dispatch("add_collection", {"url": URL, "idempotency_key": "shared"})["data"]["job_id"]
    owner_job = JobManager(store).submit("sync", {"fixture": "主人任务"}, idempotency_key="owner")["id"]

    async def run():
        async with stdio_session(store, token=credential["token"], allow_add=True) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == ADD_TOOLS
            assert not tools["add_collection"].annotations.read_only_hint
            assert tools["get_job"].annotations.read_only_hint
            for name in ("add_collection", "get_job"):
                assert not tools[name].annotations.destructive_hint
            added = body(await session.call_tool("add_collection", {"url": URL, "idempotency_key": "shared"}))
            repeated = body(
                await session.call_tool("add_collection", {"url": URL, "idempotency_key": "shared"})
            )
            assert added["data"] == repeated["data"]
            data = added["data"]
            assert data["job_id"] != foreign_job
            assert data["state"] == "queued"
            assert data["download"] == data["extraction"] == "not_requested"
            assert data["max_calls"] == data["model_requests"] == 0
            assert not {"principal", "payload", "url", "stages", "calls", "token"} & data.keys()
            own = body(await session.call_tool("get_job", {"job_id": data["job_id"]}))
            assert own["data"] == added["data"]
            for job_id in (foreign_job, owner_job):
                denied = body(await session.call_tool("get_job", {"job_id": job_id}))
                assert denied["error"]["code"] == "not_found"
            conflict = body(
                await session.call_tool(
                    "add_collection", {"url": "https://www.douyin.com/video/82", "idempotency_key": "shared"}
                )
            )
            assert conflict["error"]["code"] == "idempotency_conflict"

    asyncio.run(run())
    assert len(store.snapshot()["jobs"]) == 3
    assert_zero_fee_jobs(store)


def test_actual_stdio_rejects_raw_extra_and_coercion_before_sdk_filtering(agent_library):
    store, _, credential = agent_library

    async def run():
        async with stdio_session(store, token=credential["token"], allow_add=True) as session:
            for extra in (
                {"download": True},
                {"model": "unapproved-model"},
                {"principal": "local_owner"},
                {"max_calls": 1},
                {"allow_model_calls": True},
            ):
                invalid = body(
                    await session.call_tool(
                        "add_collection", {"url": URL, "idempotency_key": "strict", **extra}
                    )
                )
                assert invalid["error"]["code"] == "invalid_argument"
                assert store.snapshot()["jobs"] == {}
            for invalid_args in (
                {"url": 81, "idempotency_key": "strict"},
                {"url": URL, "idempotency_key": 5},
                {"url": URL},
                {"url": URL, "idempotency_key": True},
            ):
                invalid = body(await session.call_tool("add_collection", invalid_args))
                assert not invalid["ok"]
                assert store.snapshot()["jobs"] == {}
            added = body(await session.call_tool("add_collection", {"url": URL, "idempotency_key": "strict"}))
            invalid_job = body(
                await session.call_tool(
                    "get_job", {"job_id": added["data"]["job_id"], "principal": "local_owner"}
                )
            )
            assert invalid_job["error"]["code"] == "invalid_argument"

    asyncio.run(run())
    assert len(store.snapshot()["jobs"]) == 1
    assert_zero_fee_jobs(store)


def test_actual_live_stdio_revocation_blocks_add_and_job_without_restart(agent_library):
    store, registry, credential = agent_library

    async def run():
        async with stdio_session(store, token=credential["token"], allow_add=True) as session:
            added = body(
                await session.call_tool("add_collection", {"url": URL, "idempotency_key": "before-revoke"})
            )
            registry.revoke(credential["principal"])
            before = store.snapshot()
            for action, args in (
                (
                    "add_collection",
                    {"url": "https://www.douyin.com/video/82", "idempotency_key": "after-revoke"},
                ),
                ("add_collection", {"url": URL, "idempotency_key": "before-revoke"}),
                ("get_job", {"job_id": added["data"]["job_id"]}),
            ):
                denied = body(await session.call_tool(action, args))
                assert denied["error"]["code"] == "permission_denied"
                assert store.snapshot() == before
            # The local host's default read capability remains independent from the optional grant.
            found = body(await session.call_tool("search_collections", {"query": "春天"}))
            assert found["data"]["model_requests"] == 0

    asyncio.run(run())
    assert_zero_fee_jobs(store)


def run_cli(store, *args, token=None):
    return subprocess.run(
        [sys.executable, "-B", "-m", "collection_context.cli", "--workspace", str(store.files.root), *args],
        env=process_env(token),
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_actual_cli_add_permission_and_shared_add_job_contract(agent_library):
    store, registry, _ = agent_library
    granted = run_cli(store, "access-key", "--name", "原创 CLI AI", "--add")
    assert granted.returncode == 0
    credential = json.loads(granted.stdout)["data"]
    assert set(credential["permissions"]) == {"collections:read", "collections:add"}
    assert "ui:manage" not in credential["permissions"]
    token = credential["token"]
    assert all(token != record["secret_sha256"] for record in registry.records())
    first = run_cli(store, "agent-add", "--url", URL, "--idempotency-key", "cli", token=token)
    assert first.returncode == 0 and not first.stderr
    data = json.loads(first.stdout)
    repeated = run_cli(store, "agent-add", "--url", URL, "--idempotency-key", "cli", token=token)
    assert json.loads(repeated.stdout)["data"] == data["data"]
    status = run_cli(store, "agent-job", "--job-id", data["data"]["job_id"], token=token)
    assert json.loads(status.stdout)["data"] == data["data"]
    assert token not in first.stdout + first.stderr + status.stdout + status.stderr

    async def read_via_mcp():
        async with stdio_session(store, token=token, allow_add=True) as session:
            same = body(await session.call_tool("get_job", {"job_id": data["data"]["job_id"]}))
            assert same["data"] == data["data"]

    asyncio.run(read_via_mcp())
    registry.revoke(credential["principal"])
    rejected = run_cli(store, "agent-job", "--job-id", data["data"]["job_id"], token=token)
    assert rejected.returncode != 0
    assert not json.loads(rejected.stdout)["ok"]
    assert token not in rejected.stdout + rejected.stderr
    assert len(store.snapshot()["jobs"]) == 1
    assert_zero_fee_jobs(store)


def test_actual_cli_missing_or_readonly_token_cannot_admit(agent_library):
    store, registry, _ = agent_library
    readonly = registry.create("原创 CLI 只读")["token"]
    for token in (None, readonly):
        rejected = run_cli(store, "agent-add", "--url", URL, "--idempotency-key", "denied", token=token)
        assert rejected.returncode != 0
        assert not json.loads(rejected.stdout)["ok"]
        if token:
            assert token not in rejected.stdout + rejected.stderr
    assert store.snapshot()["jobs"] == {}
