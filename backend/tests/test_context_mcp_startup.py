"""Fresh-process read-only startup must not load source or media execution code."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore

SRC = str(Path(__file__).parents[1] / "src")
GUARD = """
import importlib.abc, json, sys
class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = (
            'collection_context.processing', 'collection_context.sources',
            'collection_context.workflows.addition', 'collection_context.workflows.ingestion',
            'collection_context.infrastructure.media', 'collection_context.infrastructure.runtime_ocr',
            'rapidocr', 'onnxruntime', 'playwright', 'PIL',
        )
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise RuntimeError('read-only startup loaded execution module: ' + fullname)
sys.meta_path.insert(0, NoExecutionImports())
"""


def child_env(tmp_path):
    # Deliberately no inherited provider/account credentials, PATH tools or production HOME.
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return {"PYTHONPATH": SRC, "HOME": str(home), "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}


@pytest.mark.parametrize("entry", ["module", "gateway"])
def test_readonly_import_graph_excludes_optional_execution(tmp_path, entry):
    statement = (
        "import collection_context.interfaces.mcp"
        if entry == "module"
        else "import collection_context.application.agent_addition"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", GUARD + statement],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_denied_add_permission_does_not_load_execution(tmp_path):
    store = LibraryStore.initialize(tmp_path / "original-library")
    store.close()
    code = GUARD + "\nfrom collection_context.interfaces.mcp import main\nsys.exit(main(sys.argv[1:]))\n"
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, "--workspace", str(tmp_path / "original-library"), "--allow-add"],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "authentication_required" in result.stderr
    assert "read-only startup loaded" not in result.stderr


def test_real_stdio_handshake_and_read_with_execution_imports_forbidden(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    workspace = tmp_path / "original-library"
    store = LibraryStore.initialize(workspace)
    item = store.upsert(
        {"native_id": "918", "title": "原创启动样例", "body": "只读启动无媒体权限"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    FileIndex(store).rebuild()
    before = store.snapshot()
    code = GUARD + "\nfrom collection_context.interfaces.mcp import main\nsys.exit(main(sys.argv[1:]))\n"

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-c", code, "--workspace", str(workspace)],
            env=child_env(tmp_path),
        )
        # Keep the existing handshake bound; no timeout increase to mask a slow start.
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
                    found = await session.call_tool("search_collections", {"query": "原创启动"})
                    data = found.structured_content or json.loads(found.content[0].text)
                    assert data["ok"] and data["data"]["model_requests"] == 0
                    assert data["data"]["items"][0]["material_ref"] == item["id"]
                    denied = await session.call_tool("add_collection", {})
                    assert denied.is_error

    try:
        asyncio.run(run())
        assert store.snapshot() == before
    finally:
        store.close()
