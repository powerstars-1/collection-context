"""Fixed bundled media package, never a user-selected source or download URL."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.runtime_dependencies import _absolute

MEDIA_ID = "ffmpeg-macos-arm64-9.0.2-development-1"
MEDIA_FILENAME = MEDIA_ID + ".zip"
MEDIA_BYTES = 32_134_093
MEDIA_SHA256 = "af49a020030ea6d0758aa10f800fb4fcb3327950bb4daa2ba0998a5c6b953fa2"
MEDIA_PAYLOAD_BYTES = 53_818_344
MEDIA_SOURCE = "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz"
MEDIA_TOOLS = {
    "ffmpeg": (20_971_320, "fc62a6701c1645ad79a1618d872e078c680c0ad8a914ae64277d54cac0acb6eb"),
    "ffprobe": (20_762_248, "8f7d44dec8f994142dc078e91b04240fd41eb84a179f872329c8e7c789c0cc7b"),
}


def bundled_component_archive(filename: str, size: int) -> Path:
    if not getattr(sys, "frozen", False):
        raise ContextError(
            "runtime_component_not_bundled", "请使用带所选组件的独立程序；不会从源码地址下载组件。"
        )
    executable = Path(sys.executable)
    # PyInstaller's macOS resource aliases are not traversed. Use the real
    # Resources tree explicitly; ordinary onedir builds use the fixed _MEIPASS.
    if (
        sys.platform == "darwin"
        and executable.parent.name == "MacOS"
        and executable.parent.parent.name == "Contents"
        and executable.parent.parent.parent.suffix == ".app"
    ):
        base = executable.parent.parent / "Resources"
    else:
        value = getattr(sys, "_MEIPASS", None)
        if not isinstance(value, str):
            raise ContextError("runtime_component_not_bundled", "此程序没有所选组件资源；不会联网替代。")
        base = Path(value)
    path = base / "native_software" / filename
    try:
        _absolute(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
            raise OSError
    except FileNotFoundError:
        raise ContextError(
            "runtime_component_not_bundled", "此独立程序未携带所选组件，请使用对应安装包。"
        ) from None
    except (ContextError, OSError):
        raise ContextError("runtime_component_invalid", "随包组件的路径或大小不符；不会联网替代。") from None
    # RuntimeInstaller takes a descriptor-checked snapshot and verifies the
    # entire compiled size/hash before unpacking. This lookup is metadata only.
    return path


def bundled_media_archive() -> Path:
    return bundled_component_archive(MEDIA_FILENAME, MEDIA_BYTES)


def bundled_media_state() -> str:
    try:
        bundled_media_archive()
    except ContextError as error:
        return (
            "bundled_component_missing"
            if error.code == "runtime_component_not_bundled"
            else "bundled_component_invalid"
        )
    return "available"
