"""Actual protocol process: original module only, no imported legacy service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


def test_original_stdio_roundtrip_readonly_and_business_errors(tmp_path):
    workspace = tmp_path / "独立资料库"
    store = LibraryStore.initialize(workspace)
    item = store.upsert(
        {"native_id": "123", "title": "原创合成协议测试", "body": "不是已同步的真实作品"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "原创样例画面：UI 390×844；Tailwind。",
        processor_version="fixture_v1",
        expected_content_hash=item["content_hash"],
    )
    FileIndex(store).rebuild()
    store.close()

    def files():
        return {
            str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob("*")
            if p.is_file()
        }

    before = files()

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "collection_context.interfaces.mcp", "--workspace", str(workspace)],
            env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {t.name for t in tools} == {
                    "search_collections",
                    "read_collection",
                    "collection_status",
                }
                assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
                first = await session.call_tool("search_collections", {"query": "UI 390"})
                assert not first.is_error
                data = first.structured_content or json.loads(first.content[0].text)
                assert data["ok"] and data["data"]["model_requests"] == 0
                ref = data["data"]["items"][0]["material_ref"]
                read = await session.call_tool(
                    "read_collection", {"material_ref": ref, "artifact": "screen", "max_chars": 8}
                )
                page = read.structured_content["data"]
                rest = await session.call_tool(
                    "read_collection",
                    {
                        "material_ref": ref,
                        "artifact": "screen",
                        "offset": page["next_offset"],
                        "version": page["version"],
                    },
                )
                assert "390×844" in page["text"] + rest.structured_content["data"]["text"]
                missing = await session.call_tool(
                    "read_collection", {"material_ref": ref, "artifact": "audio"}
                )
                assert missing.is_error and not missing.structured_content["ok"]
                assert missing.structured_content["error"]["code"] == "artifact_missing"
                denied = await session.call_tool("read_collection", {"material_ref": "../../.env.local"})
                assert denied.is_error and denied.structured_content["error"]["code"] == "invalid_reference"
                status = await session.call_tool("collection_status", {"material_ref": ref})
                assert status.structured_content["data"]["content_untrusted"]
                unknown = await session.call_tool("process_media", {"material_ref": ref})
                assert unknown.is_error

    asyncio.run(run())
    assert files() == before
