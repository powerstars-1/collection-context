"""Windows directory-anchored read/write facade draft, not a selected backend.

Own the root HANDLE and every traversed ancestor; no pathlib IO, CWD/PATH
resolution, follow-links fallback, or integer POSIX descriptor impersonation.
Root reopens compare native volume/file identities. All writes check attachment
before staging, before native publication and after verification. Still missing
kernel-lease/consumer migration and actual Windows acceptance; the
public platform gate intentionally remains closed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import ExitStack, contextmanager

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.file_stream import MAX_STREAM_BYTES, validate_stream
from collection_context.infrastructure.windows_deletion import WindowsDeletion
from collection_context.infrastructure.windows_native import MAX_NATIVE_READ, NativeHandle, WindowsNative
from collection_context.infrastructure.windows_publication import WindowsPublication


class WindowsFiles:
    def __init__(self, root: str, *, _native: WindowsNative | None = None):
        self.root = root
        self.native = _native if _native is not None else WindowsNative()
        self._lock = threading.RLock()
        self.handle = self.native.open_root_directory(root)
        self.identity = self.handle.identity
        self._publication = WindowsPublication(self.native)
        self._deletion = WindowsDeletion(self.native)

    @staticmethod
    def parts(relative: str) -> list[str]:
        from collection_context.infrastructure.windows_native import validate_component

        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise ContextError("forbidden_path", "文件位置无效。")
        parts = relative.split("/")
        for part in parts:
            validate_component(part)
        return parts

    def close(self) -> None:
        with self._lock:
            self.handle.close()

    def __enter__(self) -> WindowsFiles:
        self.check_root()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check_root(self) -> None:
        with self._lock:
            try:
                if self.native.information(self.handle).identity != self.identity:
                    raise ContextError("storage_unavailable", "资料库句柄身份变化。")
                with self.native.open_root_directory(self.root) as current:
                    if current.identity != self.identity:
                        raise ContextError("storage_unavailable", "资料库路径身份变化。")
                self.native.information(self.handle)
            except ContextError:
                raise ContextError(
                    "storage_unavailable", "资料库路径已变更或磁盘已断开；停止读写。"
                ) from None

    def require_private_root(self) -> None:
        with self._lock:
            self.check_root()
            self.native.require_private_security(self.handle)
            self.check_root()

    def _child(self, parent: NativeHandle, component: str, *, create: bool) -> NativeHandle:
        try:
            child = self.native.open_relative(parent, component, role="directory")
        except ContextError as error:
            if not create or error.code != "not_found":
                raise
            self.check_root()
            try:
                child = self.native.create_directory(parent, component)
            except ContextError as collision:
                if collision.code != "write_conflict":
                    raise
                # A confirmed competing creation can be opened once. It still
                # needs type/identity/private checks; no repair or blind retry.
                child = self.native.open_relative(parent, component, role="directory")
        try:
            if create:
                self.native.require_private_security(child)
            self.check_root()
            return child
        except BaseException:
            child.close()
            raise

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False) -> Iterator[tuple[NativeHandle, str]]:
        parts = self.parts(relative)
        if type(create) is not bool:
            raise ContextError("invalid_argument", "目录创建开关无效。")
        with self._lock, ExitStack() as stack:
            self.check_root()
            parent = self.handle  # Borrowed; never close the root via this stack.
            for component in parts[:-1]:
                parent = stack.enter_context(self._child(parent, component, create=create))
            try:
                yield parent, parts[-1]
            finally:
                self.check_root()

    def mkdir(self, relative: str) -> None:
        parts = self.parts(relative)
        with self._lock, ExitStack() as stack:
            self.check_root()
            parent = self.handle
            for component in parts:
                parent = stack.enter_context(self._child(parent, component, create=True))
            self.check_root()

    def entry_exists(self, relative: str) -> bool:
        """Only confirmed absence returns false; unsafe objects/parents fail.

        Metadata handles can represent a file or directory but have no body,
        write, delete or lock authority. Reparse/linked objects are not followed
        or silently interpreted as absent. No directory/file is created.
        """
        try:
            with self._parent(relative) as (parent, component):
                with self.native.open_relative(parent, component, role="metadata"):
                    self.check_root()
                    return True
        except ContextError as error:
            if error.code != "not_found":
                raise
            self.check_root()
            return False

    def file_size(self, relative: str) -> int:
        with self._parent(relative) as (parent, component):
            with self.native.open_relative(parent, component, role="metadata") as handle:
                before = self.native.information(handle)
                if before.directory:
                    raise ContextError("forbidden_path", "计量只允许非链接普通文件。")
                self.check_root()
                if self.native.information(handle) != before:
                    raise ContextError("version_changed", "计量期间文件版本变化；未返回旧大小。")
                self.check_root()
                return before.size

    def read(self, relative: str, *, max_bytes: int = MAX_NATIVE_READ, private: bool = False) -> bytes:
        # Reject before opening a directory or creating any object.
        if type(max_bytes) is not int or not 0 <= max_bytes <= (1 << 63) - 2 or type(private) is not bool:
            raise ContextError("invalid_argument", "读取上限或权限开关无效。")
        with self._parent(relative) as (parent, component):
            with self.native.open_relative(parent, component, role="read_file") as handle:
                result = self.native.read_file(handle, max_bytes=max_bytes, private=private)
                self.check_root()
                return result

    @contextmanager
    def read_chunks(
        self,
        relative: str,
        *,
        max_bytes: int = MAX_STREAM_BYTES,
        private: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> Iterator[Iterator[bytes]]:
        if (
            type(max_bytes) is not int
            or not 0 <= max_bytes <= MAX_STREAM_BYTES
            or type(private) is not bool
            or not callable(check_cancel)
        ):
            raise ContextError("invalid_argument", "分块读取上限、权限或取消检查无效。")
        check_cancel()
        with self._parent(relative) as (parent, component):
            with self.native.open_relative(parent, component, role="read_file") as handle:
                with self.native.read_chunks(
                    handle, max_bytes=max_bytes, private=private, check_cancel=check_cancel
                ) as chunks:

                    def attached() -> Generator[bytes, None, None]:
                        for chunk in chunks:
                            self.check_root()
                            yield chunk
                        self.check_root()

                    source = attached()
                    try:
                        yield source
                    finally:
                        source.close()

    def write(self, relative: str, data: bytes, *, replace: bool = False) -> None:
        if type(data) is not bytes or type(replace) is not bool:
            raise ContextError("invalid_argument", "文件内容须为字节，覆盖须明确指定。")
        with self._parent(relative, create=True) as (parent, component):
            self._publication.write(
                parent, component, data, replace=replace, _check_attachment=self.check_root
            )

    def unlink(self, relative: str) -> None:
        with self._parent(relative) as (parent, component):
            self._deletion.unlink(parent, component, check_attachment=self.check_root)

    def write_chunks(
        self,
        relative: str,
        chunks: Iterable[bytes],
        *,
        expected_size: int,
        expected_sha256: str,
        replace: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> None:
        validate_stream(expected_size, expected_sha256, replace)
        if not callable(check_cancel):
            raise ContextError("invalid_argument", "分块取消检查无效。")
        try:
            source = iter(chunks)
        except TypeError:
            raise ContextError("invalid_argument", "分块来源无效。") from None
        check_cancel()
        self.require_private_root()
        with self._parent(relative, create=True) as (parent, component):
            self._publication.write_chunks(
                parent,
                component,
                source,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                replace=replace,
                check_cancel=check_cancel,
                _check_attachment=self.require_private_root,
            )
