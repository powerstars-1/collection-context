"""Static, secret-free instructions bound to this service and its real entry point.

No command is executed. Discovery checks only fixed executable/package locations;
it does not prove SDK imports, host integration, or a distributable installation.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collection_context.application.contracts import ContextError

_SOURCE_ROOT = Path(__file__).absolute().parents[2]


def _invalid() -> ContextError:
    return ContextError("agent_setup_invalid", "当前资料库或服务地址无法生成接入说明；未回显输入。")


def _library_path(path: Path) -> str:
    # The caller supplies an already authorised library path, never a request field.
    # Do not inspect its contents or initialise an absent directory.
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in str(path))
    ):
        raise _invalid()
    return str(path)


def _origin(origin: str) -> bool:
    if type(origin) is not str or not origin or any(ord(char) <= 32 or ord(char) == 127 for char in origin):
        raise _invalid()
    try:
        parsed = urlsplit(origin)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (not local and parsed.scheme != "https")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise _invalid()
    except ValueError:
        raise _invalid() from None
    return local


def _executable(path: Path, *, allow_link: bool = False) -> bool:
    try:
        if not path.is_absolute() or (not allow_link and path.is_symlink()):
            return False
        return stat.S_ISREG(path.stat().st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _entry() -> tuple[str | None, list[str], dict[str, str], str, str]:
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        if (
            executable.name == "CollectionContextDesktop"
            and executable.parent.name == "MacOS"
            and executable.parents[1].name == "Contents"
            and executable.parents[2].suffix == ".app"
        ):
            companion = executable.parents[3] / "CollectionContext" / "CollectionContext"
            if _executable(companion):
                return str(companion), [], {}, "desktop_companion", "已发现同级控制台伴侣；尚未验证宿主接入。"
            return None, [], {}, "unavailable", "未找到同级控制台伴侣；不可用桌面进程冒充 stdio MCP。"
        if executable.name in {"CollectionContext", "CollectionContext.exe"} and _executable(executable):
            return str(executable), [], {}, "frozen_console", "已发现当前控制台入口；尚未验证宿主接入。"
        return None, [], {}, "unavailable", "未发现受支持的控制台入口；不会猜测 PATH 中的命令。"
    bootstrap = _SOURCE_ROOT / "collection_context" / "native_bootstrap.py"
    try:
        source_available = bootstrap.is_file() and not bootstrap.is_symlink()
    except OSError:
        source_available = False
    if _executable(executable, allow_link=True) and source_available:
        return (
            str(executable),
            ["-m", "collection_context.native_bootstrap"],
            {"PYTHONPATH": str(_SOURCE_ROOT)},
            "development_source",
            "开发源码入口，需现有 Python 与对应可选依赖；不证明免依赖发行或实际宿主接入。",
        )
    return None, [], {}, "unavailable", "当前运行入口不可确认；未生成猜测命令。"


def agent_setup(workspace: Path, origin: str, *, legacy_vault: Path | None = None) -> dict[str, Any]:
    """Use the server's fixed workspace/AccessPolicy origin, not Host or user input.

    Legacy stdio reads the legacy source directly; its independent HTTP access
    workspace is intentionally not supplied to CLI/MCP. Stdio uses local filesystem
    authority, not an HTTP token. Remote agents require authenticated HTTPS.
    """
    _library_path(workspace)
    library = _library_path(legacy_vault) if legacy_vault is not None else str(workspace)
    local = _origin(origin)
    command, prefix, environment, kind, reason = _entry()
    library_arguments = ["--workspace", library] + (["--legacy-vault"] if legacy_vault is not None else [])
    configuration = None
    examples = []
    if command is not None:
        server: dict[str, Any] = {"command": command, "args": [*prefix, "mcp", *library_arguments]}
        if environment:
            server["env"] = dict(environment)
        configuration = {"mcpServers": {"collection-context": server}}
        for action, arguments in (
            ("search", ["search", "--query", "UI 提示词", "--limit", "3"]),
            ("read", ["read", "--ref", "<搜索返回的 material_ref>", "--artifact", "original"]),
            ("status", ["status", "--ref", "<搜索返回的 material_ref>"]),
        ):
            example: dict[str, Any] = {
                "action": action,
                "command": command,
                "args": [*prefix, "cli", *library_arguments, *arguments],
            }
            if environment:
                example["env"] = dict(environment)
            examples.append(example)
    return {
        "library_mode": "legacy_readonly" if legacy_vault is not None else "managed",
        "runtime_kind": kind,
        "distribution_verified": False,
        "mcp": {
            "available": command is not None,
            "configuration": configuration,
            "reason": reason,
            "same_computer_only": True,
            "authentication": "stdio 直接读取本机资料，不需要 HTTP 产品口令或额外 API Key。",
        },
        "cli": {"available": command is not None, "examples": examples},
        "http": {
            "base_url": origin,
            "authentication": "Authorization: Bearer <专用只读产品口令>",
            "read_paths": [
                {"method": "POST", "path": "/v1/collections/search"},
                {"method": "POST", "path": "/v1/collections/read"},
                {"method": "GET", "path": "/v1/collections/{material_ref}/status"},
            ],
            "same_computer_only": local,
            "remote_note": "远程 AI 不能使用这台电脑的 stdio 路径；须另行部署认证 HTTPS，不自动开放公网。",
        },
        "read_only_tools": ["search_collections", "read_collection", "collection_status"],
        "model_requests": 0,
        "platform_requests": 0,
    }
