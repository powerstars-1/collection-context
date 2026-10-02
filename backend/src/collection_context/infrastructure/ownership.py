"""OS-backed ownership. Metadata alone never proves a live or dead owner."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

from collection_context.application.contracts import ContextError, canonical_bytes, utc_now
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.platform_safety import require_ownership_runtime


def worker_capabilities(body: bytes) -> dict[str, bool] | None:
    """Same bounded canonical worker metadata on POSIX and Windows; not liveness."""
    if type(body) is not bytes or len(body) > 65_536:
        raise ValueError("invalid worker metadata")
    value = json.loads(body)
    if (
        not isinstance(value, dict)
        or set(value) not in ({"nonce", "pid"}, {"nonce", "pid", "capabilities"})
        or not isinstance(value.get("nonce"), str)
        or re.fullmatch(r"e_[0-9a-f]{32}", value["nonce"]) is None
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
        or canonical_bytes(value) != body
    ):
        raise ValueError("invalid worker metadata")
    capabilities = value.get("capabilities")
    if "capabilities" in value and (
        not isinstance(capabilities, dict)
        or set(capabilities) != {"model_calls", "source_sync"}
        or any(type(v) is not bool for v in capabilities.values())
    ):
        raise ValueError("invalid worker capabilities")
    return capabilities


class _FileLease:
    path: str
    busy: str
    not_owned: str
    changed: str
    unavailable: str
    label: str

    def __init__(self, root: Path, *, metadata: dict | None = None, _expected_identity: object | None = None):
        self.files = SafeFiles(root)
        self.fd = -1
        self.nonce = "e_" + uuid.uuid4().hex
        self.body = canonical_bytes({"nonce": self.nonce, "pid": os.getpid(), **(metadata or {})})
        try:
            if _expected_identity is not None and self.files.identity != _expected_identity:
                raise ContextError("storage_unavailable", "资料库根身份已变化；没有写入所有权元数据。")
            require_ownership_runtime()
            import fcntl

            self.lock_api = fcntl
            parent, name = self.files._parent(self.path, create=True)
            try:
                self.fd = os.open(
                    name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
                )
                info = os.fstat(self.fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65_536:
                    raise ContextError("forbidden_path", "所有权文件不是受控普通文件。")
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ContextError(
                        self.busy, f"已有{self.label}持有此库；未接管运行中任务。", retryable=True
                    ) from None
                self.identity = (info.st_dev, info.st_ino)
                body = self.body
                os.ftruncate(self.fd, 0)
                if os.write(self.fd, body) != len(body):
                    raise OSError("Incomplete ownership metadata")
                os.fsync(self.fd)
            finally:
                os.close(parent)
            self.check()
        except OSError:
            self.close()
            raise ContextError(self.unavailable, f"无法安全取得{self.label}所有权。") from None
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self.fd < 0:
            raise ContextError(self.not_owned, f"当前没有{self.label}所有权。")
        parent, name = self.files._parent(self.path)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != self.identity
            ):
                raise ContextError(self.changed, "所有权文件被替换，停止提交和派发。")
            body = self.files.read(self.path, max_bytes=65_536)
            if body != self.body:
                raise ContextError(self.changed, "所有权标识变化，停止恢复和提交。")
        except ContextError as error:
            if error.code == "not_found":
                raise ContextError(self.changed, "所有权文件缺失，停止恢复和提交。") from None
            raise
        except OSError:
            raise ContextError(self.changed, "所有权文件不可访问。") from None
        finally:
            os.close(parent)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(
                self.fd
            )  # Kernel releases ownership after close/process death, not after a stale-file guess.
            self.fd = -1
        self.files.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ExecutorLease(_FileLease):
    path = ".context/执行所有权.lock"
    busy = "executor_busy"
    not_owned = "executor_not_owned"
    changed = "executor_ownership_changed"
    unavailable = "executor_unavailable"
    label = "执行器"


class WriterLease(_FileLease):
    path = ".context/写入所有权.lock"
    busy = "writer_busy"
    not_owned = "writer_not_owned"
    changed = "lock_changed"
    unavailable = "writer_unavailable"
    label = "写入任务"


class WorkerLease(_FileLease):
    path = ".context/后台所有权.lock"
    busy = "worker_busy"
    not_owned = "worker_not_owned"
    changed = "worker_ownership_changed"
    unavailable = "worker_unavailable"
    label = "后台调度器"

    def __init__(
        self,
        root: Path,
        *,
        allow_model_calls: bool | None = None,
        allow_source_sync: bool | None = None,
        _expected_identity: object | None = None,
    ):
        metadata = None
        if allow_model_calls is not None or allow_source_sync is not None:
            if type(allow_model_calls) is not bool or type(allow_source_sync) is not bool:
                raise ContextError("invalid_argument", "后台运行权限必须是明确布尔值。")
            metadata = {"capabilities": {"model_calls": allow_model_calls, "source_sync": allow_source_sync}}
        super().__init__(root, metadata=metadata, _expected_identity=_expected_identity)

    @classmethod
    def observe(cls, files: SafeFiles) -> dict:
        """Sample live kernel ownership without changing metadata, creating files or taking over work.

        A busy lease is not a heartbeat or proof of progress. Shared-lock probes are
        nonblocking and immediately released when acquired. No PID/age-based liveness inference.
        """
        result: dict[str, object] = {
            "online": None,
            "observed_at": utc_now(),
            "evidence": "os_exclusive_lease",
            "capabilities": {"model_calls": None, "source_sync": None},
            "progress_verified": False,
        }
        parent = fd = -1
        try:
            import fcntl

            parent, name = files._parent(cls.path)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                files.check_root()
                result["online"] = False
                return result
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65_536:
                return result
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                path_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                files.check_root()
                if (
                    not stat.S_ISREG(path_info.st_mode)
                    or path_info.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (path_info.st_dev, path_info.st_ino)
                ):
                    return result
                result["online"] = False
                return result
            body = os.pread(fd, 65_537, 0)
            capabilities = worker_capabilities(body)
            after = os.fstat(fd)
            path_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            files.check_root()
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or not stat.S_ISREG(path_info.st_mode)
                or path_info.st_nlink != 1
                or (after.st_dev, after.st_ino) != (path_info.st_dev, path_info.st_ino)
            ):
                return result
            # Recheck: metadata read cannot turn an already exited owner into "online".
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if os.pread(fd, 65_537, 0) != body:
                    return result
                path_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                files.check_root()
                if (
                    not stat.S_ISREG(path_info.st_mode)
                    or path_info.st_nlink != 1
                    or (after.st_dev, after.st_ino) != (path_info.st_dev, path_info.st_ino)
                ):
                    return result
                result["online"] = True
                if capabilities is not None:
                    result["capabilities"] = capabilities
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                result["online"] = False
            return result
        except (ImportError, OSError, ContextError, ValueError, TypeError, RecursionError):
            return result
        finally:
            if fd >= 0:
                os.close(fd)
            if parent >= 0:
                os.close(parent)
