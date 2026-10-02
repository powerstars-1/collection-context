"""Actual installed stdio entry point; fixture evidence only, no model requests."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import collection_context
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="mcp-smoke-", dir=args.output.absolute()))
    store = LibraryStore.initialize(root / "原创协议样例库")
    try:
        item = store.upsert(
            {"native_id": "1", "title": "原创 UI 协议样例", "body": "不是实际抓取"},
            kind="liked",
            scope_id="s_likes",
        )["item"]
        store.save_artifact(
            item["id"],
            "screen",
            "原创合成文字：UI 390×844，Tailwind。",
            processor_version="fixture_v1",
            expected_content_hash=item["content_hash"],
        )
        FileIndex(store).rebuild()
        workspace = str(store.files.root)
    finally:
        store.close()

    async def run():
        parameters = StdioServerParameters(
            command=str(Path(sys.executable).parent / "collection-context-mcp"),
            args=["--workspace", workspace],
            cwd=root,
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
                found = await session.call_tool("search_collections", {"query": "UI 390"})
                assert not found.is_error and found.structured_content["ok"]
                ref = found.structured_content["data"]["items"][0]["material_ref"]
                screen = await session.call_tool(
                    "read_collection", {"material_ref": ref, "artifact": "screen"}
                )
                assert "390×844" in screen.structured_content["data"]["text"]
                missing = await session.call_tool(
                    "read_collection", {"material_ref": ref, "artifact": "audio"}
                )
                assert missing.is_error and missing.structured_content["error"]["code"] == "artifact_missing"
                return {
                    "tools": [t.name for t in tools],
                    "ref": ref,
                    "missing_artifact_is_error": missing.is_error,
                }

    result = asyncio.run(run())
    print(
        json.dumps(
            {
                "result": "passed",
                "runtime_module": collection_context.__file__,
                "fixture_workspace": workspace,
                "real_sync_or_extraction": False,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
