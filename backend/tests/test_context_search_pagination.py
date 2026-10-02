"""Version-bound read-only search pages share CLI, HTTP and MCP contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.gateway import ReadGateway
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "隔离搜索分页库")
    for number in range(5):
        store.upsert(
            {"native_id": str(number + 1), "title": "共同教程 UI " + str(number), "body": "原创文案"},
            kind="saved",
            scope_id="s_saved",
        )
    try:
        yield store
    finally:
        store.close()


def fail(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def inventory(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_pages_cover_stable_order_without_overlap_or_writes(library):
    service = ContextService(library)
    before = inventory(library.files.root)
    all_items = service.search("共同教程", limit=20)
    first = service.search("共同教程", limit=2)
    second = service.search("共同教程", limit=1, offset=first["next_offset"], version=first["version"])
    third = service.search("共同教程", limit=2, offset=second["next_offset"], version=first["version"])
    assert first["offset"] == 0 and first["next_offset"] == 2
    assert second["offset"] == 2 and second["next_offset"] == 3
    assert third["offset"] == 3 and third["next_offset"] is None and not third["has_more"]
    assert first["version"] == second["version"] == third["version"] == all_items["version"]
    assert first["items"] + second["items"] + third["items"] == all_items["items"]
    assert first["total_matches"] == 5 and first["model_requests"] == 0
    assert inventory(library.files.root) == before


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": True},
        {"offset": -1},
        {"offset": 100_001},
        {"offset": "2"},
        {"version": False},
        {"version": ""},
        {"version": "a" * 65},
        {"version": "G" * 64},
    ],
)
def test_invalid_page_contract_before_library_io(library, monkeypatch, arguments):
    monkeypatch.setattr(library, "snapshot", lambda: pytest.fail("invalid arguments opened the library"))
    fail("invalid_argument", lambda: ContextService(library).search("共同教程", **arguments))


def test_continuation_needs_version_and_checks_result_bounds(library):
    service = ContextService(library)
    fail("version_required", lambda: service.search("共同教程", offset=1))
    first = service.search("共同教程")
    fail("invalid_argument", lambda: service.search("共同教程", offset=6, version=first["version"]))
    exhausted = service.search("共同教程", offset=5, version=first["version"])
    assert exhausted["items"] == [] and exhausted["next_offset"] is None
    empty = service.search("未命中词")
    assert empty["total_matches"] == 0 and not empty["has_more"] and len(empty["version"]) == 64


@pytest.mark.parametrize("change", ["content", "excluded", "query", "filters", "artifact_edit"])
def test_changed_content_or_query_does_not_mix_pages(library, change):
    service = ContextService(library)
    item = next(iter(library.snapshot()["items"].values()))
    if change == "artifact_edit":
        artifact = library.save_artifact(
            item["id"],
            "screen",
            "原创画面",
            processor_version="fixture",
            expected_content_hash=item["content_hash"],
        )
    first = service.search("共同教程", limit=1)
    arguments = {"query": "共同教程", "offset": 1, "version": first["version"]}
    if change == "content":
        library.upsert(
            {"native_id": item["native_id"], "title": "共同教程 已改", "body": "新文案"},
            kind="saved",
            scope_id="s_saved",
        )
    elif change == "excluded":
        library.exclude(item["id"])
    elif change == "query":
        arguments["query"] = "原创"
    elif change == "filters":
        arguments["filters"] = {"source_kinds": ["saved"]}
    else:
        library.files.write(artifact["path"], b"edited outside the manifest", replace=True)
        # The manifest/index did not change, but candidate evidence is no longer valid.
        assert service.search("共同教程")["library_version"] == first["library_version"]
    fail("version_changed", lambda: service.search(**arguments))


def test_job_progress_and_explicit_index_rebuild_do_not_invalidate_page(library):
    service = ContextService(library)
    first = service.search("共同教程 UI", limit=1)
    JobManager(library).submit("process", {}, idempotency_key="original-job", max_calls=0)
    FileIndex(library).rebuild()
    rest = service.search(" ui  共同教程 UI ", offset=1, version=first["version"], limit=20)
    assert rest["version"] == first["version"] and len(rest["items"]) == 4


def test_gateway_accepts_pages_but_rejects_other_arguments(library):
    gateway = ReadGateway(ContextService(library))
    first = gateway.dispatch("search_collections", {"query": "共同教程", "limit": 1})["data"]
    result = gateway.dispatch(
        "search_collections",
        {
            "query": "共同教程",
            "offset": 1,
            "version": first["version"],
            "limit": 2,
        },
    )
    assert result["ok"] and len(result["data"]["items"]) == 2
    denied = gateway.dispatch("search_collections", {"query": "共同教程", "path": "/arbitrary"})
    assert not denied["ok"] and denied["error"]["code"] == "invalid_argument"


def test_cli_uses_same_versioned_search_pages(library, capsys):
    prefix = ["--workspace", str(library.files.root), "search", "--query", "共同教程", "--limit", "1"]
    assert main(prefix) == 0
    first = json.loads(capsys.readouterr().out)["data"]
    assert main(prefix + ["--offset", str(first["next_offset"]), "--version", first["version"]]) == 0
    second = json.loads(capsys.readouterr().out)["data"]
    assert first["version"] == second["version"] and first["items"] != second["items"]
    assert main(prefix + ["--offset", "1"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "version_required"


def test_authenticated_http_pages_and_conflict_response(library):
    from fastapi.testclient import TestClient

    token = "synthetic-reader-" + "r" * 40
    policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_reader", token)])
    headers = {"Authorization": "Bearer " + token}
    before = inventory(library.files.root)
    with TestClient(create_app(library.files.root, policy), base_url=policy.origin) as client:
        url = "/v1/collections/search"
        first = client.post(url, json={"query": "共同教程", "limit": 1}, headers=headers).json()["data"]
        arguments = {"query": "共同教程", "offset": 1, "version": first["version"]}
        response = client.post(url, json=arguments, headers=headers)
        assert response.status_code == 200 and response.json()["data"]["offset"] == 1
        assert inventory(library.files.root) == before
        arguments["query"] = "原创"
        response = client.post(url, json=arguments, headers=headers)
        assert response.status_code == 409 and response.json()["error"]["code"] == "version_changed"
        assert client.post(url, json=arguments).status_code == 401


def test_real_stdio_mcp_exposes_and_returns_search_pages(library):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    before = inventory(library.files.root)

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "collection_context.interfaces.mcp", "--workspace", str(library.files.root)],
            env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                fields = tools["search_collections"].input_schema["properties"]
                assert {"offset", "version"} <= fields.keys()
                first = await session.call_tool("search_collections", {"query": "共同教程", "limit": 1})
                data = first.structured_content["data"]
                rest = await session.call_tool(
                    "search_collections",
                    {
                        "query": "共同教程",
                        "limit": 20,
                        "offset": data["next_offset"],
                        "version": data["version"],
                    },
                )
                assert not rest.is_error and len(rest.structured_content["data"]["items"]) == 4
                assert rest.structured_content["data"]["next_offset"] is None
                denied = await session.call_tool("search_collections", {"query": "共同教程", "offset": 1})
                assert denied.is_error and denied.structured_content["error"]["code"] == "version_required"
                for extra in ({"offset": True}, {"limit": "2"}, {"path": "/arbitrary"}):
                    invalid = await session.call_tool("search_collections", {"query": "共同教程", **extra})
                    assert (
                        invalid.is_error and invalid.structured_content["error"]["code"] == "invalid_argument"
                    )

    asyncio.run(run())
    assert inventory(library.files.root) == before
