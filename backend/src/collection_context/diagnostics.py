"""Side-effect-free startup diagnostics for the standalone product package."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import sys
from importlib import metadata
from pathlib import Path
from typing import Any


def default_workspace() -> Path:
    """Return a product-owned default path without creating it."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "CollectionContext" / "workspace"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CollectionContext" / "workspace"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "collection-context" / "workspace"


def workspace_report(path: Path) -> dict[str, Any]:
    """Classify a path without initializing, copying, or repairing anything."""
    path = path.absolute()
    if path.is_symlink():
        state = "unsafe_link"
    elif not path.exists():
        state = "missing"
    elif not path.is_dir():
        state = "not_a_directory"
    else:
        try:
            first_entry = next(path.iterdir(), None)
        except OSError:
            state = "unavailable"
        else:
            if first_entry is None:
                state = "empty"
            elif (path / "context-workspace.json").is_file():
                state = "initialized_candidate"
            else:
                state = "non_empty_uninitialized"
    return {
        "path": str(path),
        "state": state,
        "requires_explicit_initialization": state in {"missing", "empty"},
        "preserved_without_migration": state == "initialized_candidate",
    }


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _python_dependency(
    *, role: str, module: str, distribution: str, required_for_start: bool, next_action: str
) -> dict[str, Any]:
    available = importlib.util.find_spec(module) is not None
    return {
        "role": role,
        "available": available,
        "required_for_start": required_for_start,
        "version": _version(distribution) if available else None,
        "next_action": None if available else next_action,
    }


def _playwright_browser() -> dict[str, Any]:
    spec = importlib.util.find_spec("playwright")
    package = spec is not None
    available = False
    if spec is not None:
        try:
            package_root = Path(next(iter(spec.submodule_search_locations or [])))
            manifest = json.loads(
                (package_root / "driver" / "package" / "browsers.json").read_text(encoding="utf-8")
            )
            revision = next(
                item["revision"] for item in manifest["browsers"] if item["name"] == "chromium"
            )
            configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            if configured == "0":
                cache = package_root / "driver" / "package" / ".local-browsers"
            elif configured:
                cache = Path(configured).expanduser()
            elif sys.platform == "darwin":
                cache = Path.home() / "Library" / "Caches" / "ms-playwright"
            elif sys.platform == "win32":
                cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                cache /= "ms-playwright"
            else:
                cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                cache /= "ms-playwright"
            runtime = cache / f"chromium-{revision}"
            available = runtime.is_dir() and any(runtime.iterdir())
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            available = False
    return {
        "role": "source_browser",
        "available": available,
        "required_for_start": False,
        "version": _version("playwright") if package else None,
        "package_available": package,
        "runtime_check": "matching_cached_revision",
        "next_action": (
            None
            if available
            else "需要来源连接时，先安装 browser 依赖，再明确执行 Playwright Chromium 安装。"
        ),
    }


def dependency_report() -> dict[str, Any]:
    """Report capabilities only; never install packages, browsers, or model weights."""
    python_ready = sys.version_info >= (3, 12)
    ffmpeg_ready = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    rapidocr_ready = importlib.util.find_spec("rapidocr") is not None
    onnxruntime_ready = importlib.util.find_spec("onnxruntime") is not None
    dependencies = [
        {
            "role": "python_runtime",
            "available": python_ready,
            "required_for_start": True,
            "version": platform.python_version(),
            "next_action": None if python_ready else "请使用 Python 3.12 或更高版本。",
        },
        _python_dependency(
            role="management_api",
            module="fastapi",
            distribution="fastapi",
            required_for_start=True,
            next_action="安装本产品的 launcher 可选依赖。",
        ),
        _python_dependency(
            role="web_server",
            module="uvicorn",
            distribution="uvicorn",
            required_for_start=True,
            next_action="安装本产品的 launcher 可选依赖。",
        ),
        _playwright_browser(),
        {
            "role": "media_ffmpeg",
            "available": ffmpeg_ready,
            "required_for_start": False,
            "version": None,
            "next_action": (
                None
                if ffmpeg_ready
                else "需要处理视频时，单独安装 FFmpeg；启动器不会自动下载。"
            ),
        },
        {
            "role": "local_ocr",
            "available": rapidocr_ready and onnxruntime_ready,
            "required_for_start": False,
            "version": _version("rapidocr") if rapidocr_ready else None,
            "next_action": (
                None
                if rapidocr_ready and onnxruntime_ready
                else "需要本地 OCR 时，单独安装 ocr 依赖；启动器不会下载权重。"
            ),
        },
    ]
    return {
        "ready_for_management_page": all(
            item["available"] for item in dependencies if item["required_for_start"]
        ),
        "dependencies": dependencies,
        "downloads_performed": False,
    }


def port_report(port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    if type(port) is not int or not 1 <= port <= 65_535:
        return {"host": host, "port": port, "available": False, "code": "invalid_port"}
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        return {"host": host, "port": port, "available": False, "code": "port_in_use"}
    return {"host": host, "port": port, "available": True, "code": None}


def startup_report(workspace: Path, port: int) -> dict[str, Any]:
    """Return a secret-free report suitable for the terminal or support bundle."""
    return {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "verified_support": "current_machine_only",
        },
        "workspace": workspace_report(workspace),
        "listen": port_report(port),
        "capabilities": dependency_report(),
        "authorizations": {
            "source_sync": False,
            "model_calls": False,
            "automatic_downloads": False,
        },
    }
