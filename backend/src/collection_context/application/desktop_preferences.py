"""Non-authoritative desktop selection, never credentials or execution consent.

This tiny local preference is not a library, access registry or model config.
Restoration only fills visible controls; the normal diagnostics and explicit
Start confirmation must still run. All IO belongs off the GUI thread.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from collection_context.application.contracts import ContextError

_MAX_BYTES = 20_000
_FIELDS = {"schema_version", "workspace", "port", "legacy_readonly"}


@dataclass(frozen=True)
class DesktopSelection:
    workspace: Path
    port: int
    legacy_readonly: bool = False

    def payload(self) -> dict[str, object]:
        value = str(self.workspace)
        if (
            not self.workspace.is_absolute()
            or ".." in self.workspace.parts
            or not 1 <= len(value) <= 4096
            or any(ord(char) < 32 for char in value)
            or type(self.port) is not int
            or not 1 <= self.port <= 65_535
            or type(self.legacy_readonly) is not bool
        ):
            raise ContextError("desktop_preference_invalid", "上次选库记录无效；未沿用该记录。")
        return {
            "schema_version": 1,
            "workspace": value,
            "port": self.port,
            "legacy_readonly": self.legacy_readonly,
        }


def _plain_directories(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError
        if getattr(info, "st_file_attributes", 0) & 0x400:  # Windows reparse point
            raise OSError


class DesktopPreferences:
    """Fixed product-local file; values cannot select a write target or permission."""

    def __init__(self, directory: Path):
        self.directory = directory.absolute()

    def load(self) -> DesktopSelection | None:
        try:
            _plain_directories(self.directory)
            path = self.directory / "last-selection.json"
            try:
                before = path.lstat()
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > _MAX_BYTES
                or getattr(before, "st_file_attributes", 0) & 0x400
            ):
                raise OSError
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError
                data = stream.read(_MAX_BYTES + 1)
                after = os.fstat(stream.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if len(data) > _MAX_BYTES or any(getattr(before, key) != getattr(after, key) for key in fields):
                raise OSError
            payload = json.loads(data)
            if (
                not isinstance(payload, dict)
                or set(payload) != _FIELDS
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != 1
                or not isinstance(payload["workspace"], str)
            ):
                raise ValueError
            result = DesktopSelection(Path(payload["workspace"]), payload["port"], payload["legacy_readonly"])
            result.payload()
            return result
        except (OSError, ValueError, TypeError, ContextError):
            raise ContextError(
                "desktop_preference_unavailable", "上次选库记录无法安全读取；没有自动修复或启动。"
            ) from None

    def save(self, selection: DesktopSelection) -> None:
        data = json.dumps(selection.payload(), ensure_ascii=False).encode("utf-8")
        # Never place even this non-authoritative preference inside the selected
        # source library. An unusual overlapping selection still starts normally,
        # but cannot be remembered in this fixed product-owned location.
        if self.directory.is_relative_to(selection.workspace):
            raise ContextError("desktop_preference_unavailable", "选库记录不能写进当前资料库。")
        temporary: Path | None = None
        try:
            _plain_directories(self.directory)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _plain_directories(self.directory)
            target = self.directory / "last-selection.json"
            try:
                current = target.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or getattr(current, "st_file_attributes", 0) & 0x400
            ):
                raise OSError
            fd, name = tempfile.mkstemp(prefix=".selection-", dir=self.directory)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _plain_directories(self.directory)
            os.replace(temporary, target)
            temporary = None
        except (OSError, ValueError):
            raise ContextError(
                "desktop_preference_unavailable", "本次服务可继续使用，但选库记录未能保存。"
            ) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
