"""Exclusive OS ownership of one already identified product browser profile."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ownership import _FileLease
from collection_context.infrastructure.platform_safety import require_ownership_runtime

PROFILE_MARKER = "collection-browser-profile.json"
PROFILE_MARKER_BODY = b'{"schema_version":1,"owner":"collection-context"}\n'


@dataclass(frozen=True)
class ProfileMarker:
    identity: tuple[int, int]
    sha256: str


def profile_marker(files: SafeFiles) -> ProfileMarker:
    """Read only our bounded marker; never inspect Chromium cookies/profile data."""
    root = os.fstat(files.fd)
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid() or root.st_mode & 0o077:
        raise ContextError("unsafe_login_profile", "独立登录目录应仅当前用户可读写。")
    parent, name = files._parent(PROFILE_MARKER)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        body = files.read(PROFILE_MARKER, max_bytes=1024, private=True)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ContextError("browser_ownership_changed", "登录目录标记在校验时变化。")
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "owner"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["owner"] != "collection-context"
        ):
            raise ValueError
        files.check_root()
        return ProfileMarker((before.st_dev, before.st_ino), hashlib.sha256(body).hexdigest())
    except (FileNotFoundError, ValueError, TypeError):
        raise ContextError("foreign_login_profile", "登录目录身份不符，不导入其他项目账号。") from None
    finally:
        os.close(parent)


class BrowserLease(_FileLease):
    """One profile, one browser lifecycle; no stale-PID/age takeover policy.

    ``path`` validates the captured identity before _FileLease creates/opens
    its fixed lock. This reuses the existing kernel lease without widening its
    shared constructor or allowing a replaced directory to receive lock writes.
    """

    busy = "browser_busy"
    not_owned = "browser_not_owned"
    changed = "browser_ownership_changed"
    unavailable = "browser_unavailable"
    label = "独立浏览器"

    def __init__(
        self,
        root: Path,
        *,
        root_identity: tuple[int, int] | None = None,
        marker: ProfileMarker | None = None,
    ):
        with SafeFiles(root) as files:
            actual_marker = profile_marker(files)  # Must succeed before any lease file write.
            if (root_identity is not None and root_identity != files.identity) or (
                marker is not None and marker != actual_marker
            ):
                raise ContextError("browser_ownership_changed", "独立登录目录在取得所有权前改变。")
            self._profile_identity = files.identity
            self._marker = actual_marker
        require_ownership_runtime()
        import fcntl

        # Keep contention attached to the profile inode even if somebody
        # replaces the metadata lock file. This is advisory OS ownership for
        # cooperating product processes, not a defence against the OS owner.
        self._directory_files = SafeFiles(root)
        try:
            if (
                self._directory_files.identity != self._profile_identity
                or profile_marker(self._directory_files) != self._marker
            ):
                raise ContextError("browser_ownership_changed", "账号目录在锁定前发生变化。")
            try:
                fcntl.flock(self._directory_files.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ContextError(
                    "browser_busy", "此账号目录已有独立浏览器在使用，请等待退出后重试。", retryable=True
                ) from None
            super().__init__(root)
        except OSError:
            self._directory_files.close()
            raise ContextError("browser_unavailable", "账号目录的内核所有权不可用，未启动浏览器。") from None
        except BaseException:
            self._directory_files.close()
            raise

    @property
    def path(self) -> str:
        self._profile_check()
        return ".collection-context-browser.lock"

    @path.setter
    def path(self, _: str) -> None:
        # _FileLease declares a writable attribute; keep its type contract
        # without permitting clients to choose a different lock location.
        raise ContextError("forbidden_path", "浏览器所有权文件位置是固定的。")

    def _profile_check(self) -> None:
        try:
            self.files.check_root()
            if self.files.identity != self._profile_identity or profile_marker(self.files) != self._marker:
                raise ContextError("browser_ownership_changed", "独立登录目录或原账号标记发生变化。")
        except ContextError as error:
            if error.code == "unsupported_platform":
                raise
            raise ContextError(
                "browser_ownership_changed", "独立登录目录或原账号标记无法继续核对。"
            ) from None
        except OSError:
            raise ContextError("browser_ownership_changed", "独立登录目录无法继续核对。") from None

    def check(self) -> None:
        self._profile_check()
        try:
            super().check()
        except ContextError as error:
            if error.code in {"forbidden_path", "storage_unavailable", "not_found"}:
                raise ContextError("browser_ownership_changed", "独立浏览器所有权文件已变化。") from None
            raise

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._directory_files.close()
