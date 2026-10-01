"""One verified browser archive's internal aliases, never a user-provided policy.

The framework layout is preserved, not materialized or re-signed. This does not
prove Apple Developer ID signing, notarization, or every resource's integrity.
Generic archives and descriptor-based reads keep rejecting links.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles

HEADED_ID = "chromium-macos-arm64-1243"
HEADED_SOURCE = "https://cdn.playwright.dev/builds/cft/153.0.8010.12/mac-arm64/chrome-mac-arm64.zip"
HEADED_SHA256 = "930e2a2c15addbaca1fe9b07bfa520667bced556d7988707186819cb4279ef3b"
HEADED_EXECUTABLE = "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
HEADED_EXECUTABLE_SHA256 = "8319963f6625accf51c0dd4f55091ceaf9f09ed39e7a52fed4fae12b2a6b668a"
_FRAMEWORK = "chrome-mac-arm64/Google Chrome for Testing.app/Contents/Frameworks/Google Chrome for Testing Framework.framework/"
ALIASES = MappingProxyType(
    {
        _FRAMEWORK + "Resources": "Versions/Current/Resources",
        _FRAMEWORK + "Libraries": "Versions/Current/Libraries",
        _FRAMEWORK + "Helpers": "Versions/Current/Helpers",
        _FRAMEWORK
        + "Google Chrome for Testing Framework": "Versions/Current/Google Chrome for Testing Framework",
        _FRAMEWORK + "Versions/Current": "153.0.8010.12",
    }
)


def archive_aliases(identity: str, sha256: str, source_url: str) -> dict[str, str]:
    if (identity, sha256, source_url) == (HEADED_ID, HEADED_SHA256, HEADED_SOURCE):
        return dict(ALIASES)
    return {}


def _unsafe() -> ContextError:
    return ContextError("runtime_install_unsafe", "浏览器固定内部链接或目标不匹配；未放宽通用路径保护。")


def _canonical_target(name: str, aliases: dict[str, str]) -> str:
    path = str(PurePosixPath(name).parent / aliases[name])
    seen = set()
    for _ in range(len(aliases) + 1):
        if path in seen:
            raise _unsafe()
        seen.add(path)
        parts = PurePosixPath(path).parts
        if not parts or any(part in {"/", ".", ".."} for part in parts) or "\\" in path:
            raise _unsafe()
        matched = next((key for key in aliases if path == key or path.startswith(key + "/")), None)
        if matched is None:
            return path
        path = str(PurePosixPath(matched).parent / aliases[matched]) + path[len(matched) :]
    raise _unsafe()


def _check_target(files: SafeFiles, relative: str) -> None:
    parent, name = files._parent(relative)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or stat.S_ISREG(info.st_mode)
            and info.st_nlink != 1
        ):
            raise _unsafe()
    finally:
        os.close(parent)


def create_archive_aliases(files: SafeFiles, aliases: dict[str, str]) -> None:
    """Called only after all ordinary archive entries and literal aliases verified."""
    for name in aliases:
        _check_target(files, _canonical_target(name, aliases))
    for name, target in aliases.items():
        parent, filename = files._parent(name)
        try:
            os.symlink(target, filename, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
    files.check_root()


def verify_browser_aliases(root: Path, tool: dict) -> None:
    """Only the fixed Chrome tool identity invokes the compiled layout check."""
    relative = tool["relative_path"]
    if tool["source_url"] != HEADED_SOURCE or tool["sha256"] != HEADED_EXECUTABLE_SHA256:
        return
    if not relative.endswith(HEADED_EXECUTABLE):
        raise _unsafe()
    prefix = relative[: -len(HEADED_EXECUTABLE)]
    # RuntimeDependencies has already validated every path component. Prefix
    # is the installed generation (or empty during pre-publication validation).
    with SafeFiles(root) as files:
        for name, target in ALIASES.items():
            parent, filename = files._parent(prefix + name)
            try:
                info = os.stat(filename, dir_fd=parent, follow_symlinks=False)
                if (
                    not stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.getuid()
                    or os.readlink(filename, dir_fd=parent) != target
                ):
                    raise _unsafe()
            finally:
                os.close(parent)
            _check_target(files, prefix + _canonical_target(name, dict(ALIASES)))
        files.check_root()
