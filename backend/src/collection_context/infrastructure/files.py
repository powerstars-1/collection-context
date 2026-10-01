"""Descriptor-anchored file access. Windows backend is an explicit pending adapter."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from collection_context.application.contracts import ContextError


class SafeFiles:
    def __init__(self, root: Path):
        self.root = root.absolute()
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise ContextError("unsupported_platform", "本适配器尚未通过 Windows 文件安全验收。")
        try:
            self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
        except OSError:
            raise ContextError("storage_unavailable", "资料库未挂载或不是安全目录。") from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def check_root(self) -> None:
        try:
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
                raise OSError
        except OSError:
            raise ContextError("storage_unavailable", "资料库路径已变更或磁盘已断开；停止写入。") from None

    @staticmethod
    def parts(relative: str) -> list[str]:
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise ContextError("forbidden_path", "文件位置无效。")
        parts = relative.split("/")
        if any(p in {"", ".", ".."} or "\\" in p or "\x00" in p or ":" in p for p in parts):
            raise ContextError("forbidden_path", "文件位置不在允许范围。")
        return parts

    def _parent(self, relative: str, *, create: bool = False) -> tuple[int, str]:
        self.check_root()
        parts = self.parts(relative)
        parent = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = next_fd
            return parent, parts[-1]
        except OSError:
            os.close(parent)
            raise ContextError("forbidden_path", "目录不可访问或包含链接。") from None

    def read(self, relative: str, *, max_bytes: int = 16_000_000, private: bool = False) -> bytes:
        parent, name = self._parent(relative)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                    raise ContextError("forbidden_path", "只允许大小受限的非链接文件。")
                if private and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
                    raise ContextError("unsafe_secret_permissions", "凭据文件须仅允许当前运行用户访问。")
                result = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
                if len(result) > max_bytes or (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ContextError("version_changed", "读取时内容发生变化，请重新读取。", retryable=True)
                return result
        except FileNotFoundError:
            raise ContextError("not_found", "文件尚未提交或不存在。") from None
        except OSError:
            raise ContextError("forbidden_path", "文件不可安全读取。") from None
        finally:
            os.close(parent)

    def write(self, relative: str, data: bytes, *, replace: bool = False) -> None:
        parent, name = self._parent(relative, create=True)
        temp = ".tmp-" + uuid.uuid4().hex if replace else name
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                self.check_root()
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            raise ContextError("write_conflict", "目标已存在，未覆盖原文件。") from None
        except OSError:
            raise ContextError("storage_unavailable", "写入未完成，请检查磁盘与权限。") from None
        finally:
            if replace:
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)

    def unlink(self, relative: str) -> None:
        parent, name = self._parent(relative)
        try:
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
