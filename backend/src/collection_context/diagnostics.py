"""Side-effect-free startup diagnostics for the standalone product package."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import stat
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


def _unlinked_file(path: Path) -> bool:
    """Conservative static check only; this does not authorize executing a file."""
    try:
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            return False
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _browser_revisions(package_root: Path) -> dict[str, str]:
    manifest = package_root / "driver" / "package" / "browsers.json"
    if not _unlinked_file(manifest):
        raise ValueError
    # Bound the read itself, not merely the file's earlier stat size.
    with manifest.open("rb") as stream:
        data = stream.read(65_537)
    if len(data) > 65_536:
        raise ValueError
    payload = json.loads(data)
    browsers = payload["browsers"]
    if not isinstance(browsers, list) or len(browsers) > 32:
        raise ValueError
    names = {"chromium", "chromium-headless-shell"}
    revisions: dict[str, str] = {}
    for browser in browsers:
        if not isinstance(browser, dict):
            raise ValueError
        name = browser.get("name")
        if name not in names:
            continue
        revision = browser.get("revision")
        if (
            name in revisions
            or not isinstance(revision, str)
            or not revision.isascii()
            or not revision.isdecimal()
            or not 1 <= len(revision) <= 12
            or browser.get("revisionOverrides")
        ):
            # Host-specific overrides require the official registry; never guess.
            raise ValueError
        revisions[name] = revision
    if set(revisions) != names or len(set(revisions.values())) != 1:
        raise ValueError
    return revisions


def _playwright_browser(*, sdk: Any = None) -> dict[str, Any]:
    """No driver/browser startup, downloads, network, or profile creation.

    An already initialized SDK may be injected by an explicit diagnostic caller.
    Only its public Chromium executable_path is consulted. The SDK exposes no
    public default headless-shell executable_path, so that role stays unverified.
    """
    package = False
    manifest_verified = False
    desktop = {"state": "not_verified", "static_available": False, "runtime_verified": False}
    headless = {"state": "not_verified", "static_available": False, "runtime_verified": False}
    try:
        spec = importlib.util.find_spec("playwright")
        package = spec is not None
        if spec is not None:
            package_root = Path(next(iter(spec.submodule_search_locations or [])))
            revisions = _browser_revisions(package_root)
            manifest_verified = True
            if sdk is not None:
                # No launch(), sync_playwright(), private registry or guessed
                # per-platform executable layout. SDK ownership stays with caller.
                path = Path(sdk.chromium.executable_path)
                if not _unlinked_file(path):
                    desktop["state"] = "missing_or_unsafe"
                elif f"chromium-{revisions['chromium']}" not in {p.name for p in path.parents}:
                    desktop["state"] = "revision_not_verified"
                elif not os.access(path, os.X_OK):
                    desktop["state"] = "not_executable"
                else:
                    desktop.update(state="static_available", static_available=True)
    except Exception:
        # Import/manifest/SDK errors may carry local paths or environment values.
        # Do not echo them or treat any failed check as readiness.
        desktop.update(state="not_verified", static_available=False)
    try:
        package_version = _version("playwright") if package else None
    except Exception:
        package_version = None
    return {
        "role": "source_browser",
        "available": False,
        "static_available": False,
        "runtime_verified": False,
        "required_for_start": False,
        "version": package_version,
        "package_present": package,
        "package_available": package,
        "manifest_verified": manifest_verified,
        "desktop": desktop,
        "headless": headless,
        "runtime_check": "static_only_no_process_started",
        "next_action": "浏览器未功能探测；需要来源连接时再显式检查匹配运行文件与启动能力。",
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
                None if ffmpeg_ready else "需要处理视频时，单独安装 FFmpeg；启动器不会自动下载。"
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
