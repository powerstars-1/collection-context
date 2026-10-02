"""Static handoffs only: no library, credential, command or network access."""

import builtins
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from collection_context.application import agent_setup as module
from collection_context.application.contracts import ContextError


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original fixture, never execute")
    path.chmod(0o700)
    return path


def test_source_exact_arguments_fixed_environment_and_no_library_creation(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("EXTRA_SECRET", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted-parent-path")
    workspace = tmp_path / "中文 空资料库"
    result = module.agent_setup(workspace, "http://127.0.0.1:18798")
    server = result["mcp"]["configuration"]["mcpServers"]["collection-context"]
    assert server == {
        "command": sys.executable,
        "args": ["-m", "collection_context.native_bootstrap", "mcp", "--workspace", str(workspace)],
        "env": {"PYTHONPATH": str(module._SOURCE_ROOT)},
    }
    assert result["runtime_kind"] == "development_source"
    assert not result["distribution_verified"]
    assert "需现有 Python" in result["mcp"]["reason"]
    assert not workspace.exists()
    for example in result["cli"]["examples"]:
        assert example["command"] == sys.executable
        assert example["args"][:5] == [
            "-m",
            "collection_context.native_bootstrap",
            "cli",
            "--workspace",
            str(workspace),
        ]
    rendered = json.dumps(result, ensure_ascii=False)
    assert "synthetic-secret" not in rendered and "untrusted-parent-path" not in rendered
    assert "--token" not in rendered and "--allow-add" not in rendered


def test_legacy_uses_source_not_http_access_library(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    access, source = tmp_path / "独立访问配置", tmp_path / "旧 Markdown 库"
    result = module.agent_setup(access, "http://localhost:8787", legacy_vault=source)
    assert result["library_mode"] == "legacy_readonly"
    command = result["mcp"]["configuration"]["mcpServers"]["collection-context"]
    assert command["args"][-3:] == ["--workspace", str(source), "--legacy-vault"]
    assert str(access) not in json.dumps(result)
    assert not source.exists() and not access.exists()


@pytest.mark.parametrize("desktop", [False, True])
def test_frozen_console_and_desktop_companion(tmp_path, monkeypatch, desktop):
    console = executable(tmp_path / "dist" / "CollectionContext" / "CollectionContext")
    running = console
    if desktop:
        running = executable(
            tmp_path
            / "dist"
            / "CollectionContextDesktop.app"
            / "Contents"
            / "MacOS"
            / "CollectionContextDesktop"
        )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(running))
    workspace = tmp_path / "资料"
    result = module.agent_setup(workspace, "https://agent.example")
    server = result["mcp"]["configuration"]["mcpServers"]["collection-context"]
    assert server == {"command": str(console), "args": ["mcp", "--workspace", str(workspace)]}
    assert result["runtime_kind"] == ("desktop_companion" if desktop else "frozen_console")
    assert not result["http"]["same_computer_only"]
    assert result["mcp"]["same_computer_only"]
    assert not workspace.exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable", "symlink"])
def test_missing_or_invalid_companion_never_guesses_path(tmp_path, monkeypatch, kind):
    running = executable(
        tmp_path / "dist" / "CollectionContextDesktop.app" / "Contents" / "MacOS" / "CollectionContextDesktop"
    )
    companion = tmp_path / "dist" / "CollectionContext" / "CollectionContext"
    if kind == "directory":
        companion.mkdir(parents=True)
    elif kind == "nonexecutable":
        executable(companion).chmod(0o600)
    elif kind == "symlink":
        companion.parent.mkdir(parents=True)
        companion.symlink_to(running)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(running))
    result = module.agent_setup(tmp_path / "workspace", "http://127.0.0.1:8787")
    assert result["mcp"]["available"] is False
    assert result["mcp"]["configuration"] is None
    assert result["cli"] == {"available": False, "examples": []}
    assert result["http"]["base_url"] == "http://127.0.0.1:8787"


def test_arbitrary_frozen_executable_is_not_a_product_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable(tmp_path / "other")))
    assert not module.agent_setup(tmp_path / "workspace", "http://127.0.0.1:8787")["mcp"]["available"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com/",
        "https://example.com?q=secret",
        "https://example.com#secret",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "https://example.com\nsecret",
        "https://",
        "file:///secret",
        None,
    ],
)
def test_invalid_origins_do_not_echo_input(tmp_path, origin):
    with pytest.raises(ContextError) as caught:
        module.agent_setup(tmp_path / "workspace", origin)
    assert caught.value.code == "agent_setup_invalid"
    assert "secret" not in caught.value.message


@pytest.mark.parametrize(
    "path", [Path("relative"), Path("/safe/../other"), Path("/safe/\nsecret"), "not-Path"]
)
def test_bad_library_paths_rejected_without_echo(path):
    with pytest.raises(ContextError) as caught:
        module.agent_setup(path, "http://127.0.0.1:8787")
    assert caught.value.code == "agent_setup_invalid"


def test_no_body_reads_commands_network_or_credential_discovery(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("static instructions must not read bodies or execute operations")

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = module.agent_setup(tmp_path / "workspace", "https://service.example")
    assert result["model_requests"] == result["platform_requests"] == 0
    assert set(result["read_only_tools"]) == {"search_collections", "read_collection", "collection_status"}
    assert result["http"]["authentication"] == "Authorization: Bearer <专用只读产品口令>"
    assert result["http"]["read_paths"][-1] == {
        "method": "GET",
        "path": "/v1/collections/{material_ref}/status",
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_cli_examples_parse_through_the_real_readonly_parser(tmp_path, monkeypatch, legacy):
    from collection_context.cli import parser

    monkeypatch.delattr(sys, "frozen", raising=False)
    workspace, source = tmp_path / "access", tmp_path / "source"
    result = module.agent_setup(workspace, "http://127.0.0.1:8787", legacy_vault=source if legacy else None)
    for example in result["cli"]["examples"]:
        args = parser().parse_args(example["args"][3:])
        assert args.workspace == (source if legacy else workspace)
        assert args.legacy_vault is legacy
        assert args.command == example["action"]
    assert not workspace.exists() and not source.exists()
