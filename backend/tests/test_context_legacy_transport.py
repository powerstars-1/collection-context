"""Real CLI and stdio clients read only original legacy-layout fixture files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from collection_context import cli
from collection_context.interfaces import mcp as mcp_interface
from collection_context.library.store import LibraryStore

SRC = str(Path(__file__).parents[1] / "src")
GUARD = """
import importlib.abc, sys
class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('app', 'collection_context.processing', 'collection_context.sources',
                   'collection_context.workflows.addition', 'collection_context.workflows.ingestion',
                   'playwright', 'rapidocr', 'onnxruntime')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise RuntimeError('read-only MCP loaded execution module: ' + fullname)
sys.meta_path.insert(0, NoExecutionImports())
from collection_context.interfaces.mcp import main
sys.exit(main(sys.argv[1:]))
"""


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "原创旧布局"
    vault = root / "content-vault"
    cards = vault / "00_素材收件箱" / "抖音"
    attachments = vault / "80_附件" / "抖音" / "913_原创"
    cards.mkdir(parents=True)
    attachments.mkdir(parents=True)
    card = cards / "原创样例.md"
    card.write_text(
        "# 原创纸飞机教程\n\n## 基本信息\n"
        "- 平台: 抖音\n- 来源: 收藏\n- 作者: 测试作者\n"
        "- 链接: https://www.douyin.com/video/913\n"
        "- 附件目录: 80_附件/抖音/913_原创\n\n"
        "## 原始材料\n先折纸飞机，再观察机翼。\n\n"
        "## 用户备注\n下次尝试蓝色纸张。\n",
        encoding="utf-8",
    )
    (attachments / "画面文字.md").write_text("原创画面：机翼角度 15 度。", encoding="utf-8")
    (attachments / "音频转写.md").write_text("原创音频：折出对称机翼。", encoding="utf-8")
    (attachments / "内容总结.md").write_text("原创总结：纸飞机折法。", encoding="utf-8")
    (root / ".env.local").write_text("LEGACY_SECRET_SENTINEL=not-for-reading\n", encoding="utf-8")
    relative = card.relative_to(vault).as_posix()
    ref = "m1:" + base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
    return root, vault, ref


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def child_env(tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return {"PYTHONPATH": SRC, "HOME": str(home), "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}


def run_cli(tmp_path, workspace, *arguments, legacy_mode=True):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "collection_context.cli",
            "--workspace",
            str(workspace),
            *(["--legacy-vault"] if legacy_mode else []),
            *arguments,
        ],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.parametrize("directory", ["root", "vault"])
def test_legacy_cli_all_read_commands_preserve_library_and_boundaries(tmp_path, legacy, directory):
    root, vault, ref = legacy
    workspace = root if directory == "root" else vault
    before = snapshot(root)
    queries = [
        ("search", "--query", "机翼 15"),
        ("list", "--source-kind", "saved"),
        ("overview",),
        ("status", "--ref", ref),
        ("read", "--ref", ref, "--artifact", "screen", "--max-chars", "8"),
    ]
    results = []
    for command in queries:
        process = run_cli(tmp_path, workspace, *command)
        assert process.returncode == 0, process.stderr + process.stdout
        response = json.loads(process.stdout)
        assert response["ok"] and response["data"]["model_requests"] == 0
        assert "LEGACY_SECRET_SENTINEL" not in process.stdout
        results.append(response["data"])
    assert results[0]["items"][0]["material_ref"] == ref
    assert results[1]["items"][0]["relations"][0]["action_at"] is None
    assert results[3]["artifacts"]["screen"]["coverage"]["complete"] is None
    page = results[4]
    process = run_cli(
        tmp_path,
        workspace,
        "read",
        "--ref",
        ref,
        "--artifact",
        "screen",
        "--offset",
        str(page["next_offset"]),
        "--version",
        page["version"],
    )
    assert process.returncode == 0
    assert page["text"] + json.loads(process.stdout)["data"]["text"] == "原创画面：机翼角度 15 度。"
    denied = run_cli(tmp_path, workspace, "read", "--ref", "../../.env.local")
    assert denied.returncode == 1
    assert json.loads(denied.stdout)["error"]["code"] == "invalid_reference"
    assert snapshot(root) == before
    assert not (root / ".context").exists()
    assert not (vault / ".context").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("init",),
        ("run-job", "--job-id", "j_example", "--credential-dir", "never-open"),
        ("agent-add", "--url", "https://www.douyin.com/video/913", "--idempotency-key", "test"),
        (
            "configure-model",
            "--role",
            "summary",
            "--base-url",
            "https://example.invalid/v1",
            "--model",
            "fixture",
            "--prompt-key",
        ),
        ("runtime-options",),
    ],
)
def test_legacy_cli_real_write_commands_refused_before_opening_missing_workspace(tmp_path, arguments):
    missing = tmp_path / "must-not-create"
    process = run_cli(tmp_path, missing, *arguments)
    assert process.returncode == 1
    assert json.loads(process.stdout)["error"]["code"] == "permission_denied"
    assert not missing.exists()
    assert "Traceback" not in process.stderr


def test_every_other_registered_cli_command_is_denied_before_any_library_or_secret(monkeypatch, capsys):
    parser = cli.parser()
    choices = next(action.choices for action in parser._actions if action.dest == "command")
    expected_read_commands = {"search", "read", "status", "list", "overview"}

    def forbidden(*args, **kwargs):
        pytest.fail("A rejected legacy command opened a library, authority or credential backend")

    for name in ("LibraryStore", "LegacyLayoutReader", "FileSecrets", "SystemSecrets", "AccessRegistry"):
        monkeypatch.setattr(cli, name, forbidden)
    for command in choices.keys() - expected_read_commands:
        args = SimpleNamespace(command=command, legacy_vault=True)
        monkeypatch.setattr(cli, "parser", lambda: SimpleNamespace(parse_args=lambda argv: args))
        assert cli.main([]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "permission_denied"


def test_new_library_default_cli_unchanged_and_new_list_overview_work(tmp_path):
    workspace = tmp_path / "new-library"
    store = LibraryStore.initialize(workspace)
    item = store.upsert(
        {"native_id": "917", "title": "原创新库样例", "body": "原生资料检索"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.close()
    for command in [("search", "--query", "原生"), ("list",), ("overview",)]:
        process = run_cli(tmp_path, workspace, *command, legacy_mode=False)
        assert process.returncode == 0, process.stderr + process.stdout
        data = json.loads(process.stdout)["data"]
        if command[0] != "overview":
            assert data["items"][0]["material_ref"] == item["id"]
    process = run_cli(tmp_path, workspace, "list")
    assert process.returncode == 1
    assert json.loads(process.stdout)["error"]["code"] == "legacy_mode_not_allowed"


def test_mcp_allow_add_rejected_before_credentials_or_either_library(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Legacy MCP addition opened a library or credential authority")

    for name in ("LibraryStore", "LegacyLayoutReader", "AccessRegistry"):
        monkeypatch.setattr(mcp_interface, name, forbidden)
    assert (
        mcp_interface.main(["--workspace", str(tmp_path / "missing"), "--legacy-vault", "--allow-add"]) == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "permission_denied" in output.err


def test_legacy_mcp_stdio_roundtrip_without_execution_imports_or_writes(tmp_path, legacy):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root, vault, ref = legacy
    before = snapshot(root)

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-c", GUARD, "--workspace", str(root), "--legacy-vault"],
            env=child_env(tmp_path),
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
                    result = await session.call_tool("search_collections", {"query": "机翼 15"})
                    assert not result.is_error
                    assert result.structured_content["data"]["items"][0]["material_ref"] == ref
                    result = await session.call_tool(
                        "read_collection", {"material_ref": ref, "artifact": "user_note"}
                    )
                    assert not result.is_error
                    assert result.structured_content["data"]["text"] == "下次尝试蓝色纸张。"
                    result = await session.call_tool("collection_status", {"material_ref": ref})
                    assert result.structured_content["data"]["model_requests"] == 0
                    assert result.structured_content["data"]["accuracy"] == "not_verified"
                    denied = await session.call_tool(
                        "add_collection", {"url": "https://www.douyin.com/video/913"}
                    )
                    assert denied.is_error
                    denied = await session.call_tool(
                        "read_collection", {"material_ref": ref, "artifact": "../../.env.local"}
                    )
                    assert denied.is_error
                    assert denied.structured_content["error"]["code"] == "invalid_artifact"

    asyncio.run(run())
    assert snapshot(root) == before
