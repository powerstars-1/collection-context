"""Rooted Windows lease composition; not a selected/accepted product backend.

Own native root/ancestor/file handles, bootstrap privately only if absent,
acquire a nonblocking kernel range lock before writing metadata, and compare
current path identity and canonical metadata before permitting further work.
No PID-age takeover, stale-file deletion, native retries or POSIX fd emulation.
Actual Windows kernel/process-death acceptance and consumer selection remain
required; public platform_safety does not select this draft.

https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile
"""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import ExitStack

from collection_context.application.contracts import ContextError, canonical_bytes, utc_now
from collection_context.infrastructure.ownership import worker_capabilities
from collection_context.infrastructure.windows_files import WindowsFiles
from collection_context.infrastructure.windows_native import NativeHandle, NativeLock, WindowsNative


class _WindowsFileLease:
    path: str
    busy: str
    not_owned: str
    changed: str
    unavailable: str
    label: str

    def __init__(
        self,
        root: str,
        *,
        _native: WindowsNative | None = None,
        _metadata: dict | None = None,
        _expected_identity: object | None = None,
    ):
        self._mutex = threading.RLock()
        self._parents = ExitStack()
        self._closed = False
        self.handle: NativeHandle | None = None
        self.lock: NativeLock | None = None
        self.files = WindowsFiles(root, _native=_native)
        self.nonce = "e_" + uuid.uuid4().hex
        self.body = canonical_bytes({"nonce": self.nonce, "pid": os.getpid(), **(_metadata or {})})
        try:
            if _expected_identity is not None and self.files.identity != _expected_identity:
                raise ContextError("storage_unavailable", "资料库根身份已变化；没有写入所有权元数据。")
            # Keep ancestor handles, but do not retain a thread-owned RLock for
            # the lease lifetime. Check/close may be called by another thread.
            with self.files._lock:
                self.files.check_root()
                parts = self.files.parts(self.path)
                self.parent = self.files.handle
                for component in parts[:-1]:
                    self.parent = self._parents.enter_context(
                        self.files._child(self.parent, component, create=True)
                    )
                self.component = parts[-1]
                self.files.native.require_private_security(self.parent)
                try:
                    self.handle = self.files.native.open_relative(
                        self.parent, self.component, role="lease_file"
                    )
                except ContextError as error:
                    if error.code != "not_found":
                        raise
                    self.files.check_root()
                    try:
                        self.handle = self.files.native.create_lease_file(self.parent, self.component)
                    except ContextError as collision:
                        if collision.code != "write_conflict":
                            raise
                        self.handle = self.files.native.open_relative(
                            self.parent, self.component, role="lease_file"
                        )
                self.files.native.require_private_security(self.handle)
                self.identity = self.handle.identity
                self._check_path(check_body=False)
                try:
                    self.lock = self.files.native.try_lock(self.handle, exclusive=True)
                except ContextError as error:
                    if error.code == "lock_busy":
                        raise ContextError(
                            self.busy, f"已有{self.label}持有此库；未接管运行中任务。", retryable=True
                        ) from None
                    raise
                self._check_path(check_body=False)
                self.files.native.write_lease_metadata(self.lock, self.body)
                self.check()
        except BaseException:
            try:
                self.close()
            except ContextError:
                pass  # Preserve original failure; never retry cleanup/native IO.
            raise

    def _check_path(self, *, check_body: bool) -> None:
        self.files.check_root()
        self.files.native.information(self.parent)
        with self.files._parent(self.path) as (current_parent, component):
            if current_parent.identity != self.parent.identity:
                raise ContextError(self.changed, "所有权目录被替换；停止提交和派发。")
            with self.files.native.open_relative(
                current_parent, component, role="lease_observer"
            ) as observer:
                if observer.identity != self.identity:
                    raise ContextError(self.changed, "所有权文件被替换；停止提交和派发。")
                self.files.native.require_private_security(observer)
                if (
                    check_body
                    and self.files.native.read_file(observer, max_bytes=65_536, private=True) != self.body
                ):
                    raise ContextError(self.changed, "所有权标识变化；停止提交和派发。")
        self.files.check_root()

    def check(self) -> None:
        with self._mutex:
            if self._closed or self.handle is None or self.lock is None:
                raise ContextError(self.not_owned, f"当前没有{self.label}所有权。")
            try:
                if (
                    self.handle.closed
                    or self.lock.closed
                    or self.handle._active_lock is not self.lock
                    or self.lock.identity != self.identity
                ):
                    raise ContextError(self.changed, "内核所有权已失效；停止提交和派发。")
                self._check_path(check_body=True)
                if self.files.native.read_file(self.handle, max_bytes=65_536, private=True) != self.body:
                    raise ContextError(self.changed, "所有权内容变化；停止提交和派发。")
                self._check_path(check_body=True)
                if self.handle._active_lock is not self.lock or self.lock.closed:
                    raise ContextError(self.changed, "内核所有权在检查期间失效。")
            except ContextError as error:
                if error.code == "not_found":
                    raise ContextError(self.changed, "所有权路径缺失；停止提交和派发。") from None
                raise

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            failure: BaseException | None = None
            for resource in (self.lock, self.handle, self._parents, self.files):
                if resource is None:
                    continue
                try:
                    resource.close()
                except BaseException as error:
                    # Finish attempting every owned close even on control-flow
                    # interruption; preserve interruption rather than retrying.
                    if failure is None or not isinstance(error, Exception):
                        failure = error
            if failure is not None:
                if not isinstance(failure, Exception):
                    raise failure
                raise ContextError(
                    self.unavailable, "所有权关闭结果不能完全确认；没有重新打开或接管。"
                ) from None

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WindowsExecutorLease(_WindowsFileLease):
    path = ".context/执行所有权.lock"
    busy = "executor_busy"
    not_owned = "executor_not_owned"
    changed = "executor_ownership_changed"
    unavailable = "executor_unavailable"
    label = "执行器"


class WindowsWriterLease(_WindowsFileLease):
    path = ".context/写入所有权.lock"
    busy = "writer_busy"
    not_owned = "writer_not_owned"
    changed = "lock_changed"
    unavailable = "writer_unavailable"
    label = "写入任务"


class WindowsWorkerLease(_WindowsFileLease):
    path = ".context/后台所有权.lock"
    busy = "worker_busy"
    not_owned = "worker_not_owned"
    changed = "worker_ownership_changed"
    unavailable = "worker_unavailable"
    label = "后台调度器"

    def __init__(
        self,
        root: str,
        *,
        allow_model_calls: bool | None = None,
        allow_source_sync: bool | None = None,
        _native: WindowsNative | None = None,
        _expected_identity: object | None = None,
    ):
        metadata = None
        if allow_model_calls is not None or allow_source_sync is not None:
            if type(allow_model_calls) is not bool or type(allow_source_sync) is not bool:
                raise ContextError("invalid_argument", "后台运行权限必须是明确布尔值。")
            metadata = {"capabilities": {"model_calls": allow_model_calls, "source_sync": allow_source_sync}}
        super().__init__(root, _native=_native, _metadata=metadata, _expected_identity=_expected_identity)

    @classmethod
    def observe(cls, files: WindowsFiles) -> dict:
        """Only sample kernel ownership; no creation, takeover, heartbeat or PID probe."""
        result: dict[str, object] = {
            "online": None,
            "observed_at": utc_now(),
            "evidence": "os_exclusive_lease",
            "capabilities": {"model_calls": None, "source_sync": None},
            "progress_verified": False,
        }
        try:
            with files._parent(cls.path) as (parent, component):
                with files.native.open_relative(parent, component, role="lease_observer") as handle:
                    files.native.require_private_security(handle)
                    before = files.native.information(handle)

                    def attached() -> None:
                        with files._parent(cls.path) as (current_parent, current_component):
                            if current_parent.identity != parent.identity:
                                raise ContextError("lock_changed", "观察时所有权目录变化。")
                            with files.native.open_relative(
                                current_parent, current_component, role="lease_observer"
                            ) as current:
                                if current.identity != handle.identity:
                                    raise ContextError("lock_changed", "观察时所有权文件变化。")
                        files.check_root()

                    try:
                        probe = files.native.try_lock(handle, exclusive=False)
                    except ContextError as error:
                        if error.code != "lock_busy":
                            raise
                    else:
                        probe.close()
                        attached()
                        result["online"] = False
                        return result
                    body = files.native.read_file(handle, max_bytes=65_536, private=True)
                    capabilities = worker_capabilities(body)
                    if files.native.information(handle) != before:
                        return result
                    attached()
                    try:
                        probe = files.native.try_lock(handle, exclusive=False)
                    except ContextError as error:
                        if error.code != "lock_busy":
                            raise
                        if files.native.read_file(handle, max_bytes=65_536, private=True) != body:
                            return result
                        attached()
                        result["online"] = True
                        if capabilities is not None:
                            result["capabilities"] = capabilities
                    else:
                        probe.close()
                        attached()
                        result["online"] = False
                    return result
        except ContextError as error:
            result["online"] = None
            result["capabilities"] = {"model_calls": None, "source_sync": None}
            if error.code == "not_found":
                try:
                    files.check_root()
                except ContextError:
                    pass
                else:
                    result["online"] = False
            return result
        except (ValueError, TypeError, RecursionError):
            return result
