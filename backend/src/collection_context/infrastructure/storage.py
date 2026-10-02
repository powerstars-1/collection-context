"""One business storage contract; native handles stay inside their adapters.

The default selector still refuses Windows until the remaining browser/runtime
consumers and actual Windows acceptance are complete. WindowsStorage is the
explicit native composition for integration tests and that future selector;
it is not a pathlib fallback or a declaration of release support.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.file_stream import MAX_STREAM_BYTES
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ownership import ExecutorLease, WorkerLease, WriterLease
from collection_context.infrastructure.platform_safety import require_safe_files_runtime

if TYPE_CHECKING:
    from collection_context.infrastructure.windows_files import WindowsFiles
    from collection_context.infrastructure.windows_native import WindowsNative
    from collection_context.infrastructure.windows_ownership import _WindowsFileLease


class FileAccess(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def identity(self) -> object: ...

    def check_root(self) -> None: ...
    def require_private_root(self) -> None: ...
    def entry_exists(self, relative: str) -> bool: ...
    def file_size(self, relative: str) -> int: ...
    def list_directory(self, relative: str, *, max_entries: int = 10_000) -> list[dict[str, str]]: ...
    def read(self, relative: str, *, max_bytes: int = 16_000_000, private: bool = False) -> bytes: ...
    def write(self, relative: str, data: bytes, *, replace: bool = False) -> None: ...
    def unlink(self, relative: str) -> None: ...
    def read_chunks(
        self,
        relative: str,
        *,
        max_bytes: int = MAX_STREAM_BYTES,
        private: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> AbstractContextManager[Iterator[bytes]]: ...

    def write_chunks(
        self,
        relative: str,
        chunks: Iterable[bytes],
        *,
        expected_size: int,
        expected_sha256: str,
        replace: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> None: ...

    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *_: object) -> None: ...


class KernelLease(Protocol):
    @property
    def files(self) -> FileAccess: ...

    def check(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *_: object) -> None: ...


class StorageBackend(Protocol):
    def open_files(self, root: Path) -> FileAccess: ...
    def initialize(self, root: Path, *, allow_empty: bool) -> AbstractContextManager[FileAccess]: ...
    def writer(self, root: Path, *, expected_identity: object | None = None) -> KernelLease: ...
    def executor(self, root: Path, *, expected_identity: object | None = None) -> KernelLease: ...
    def worker(
        self,
        root: Path,
        *,
        model_calls: bool,
        source_sync: bool,
        expected_identity: object | None = None,
    ) -> KernelLease: ...
    def observe_worker(self, files: FileAccess) -> dict: ...


class PosixStorage:
    """Preserve the existing dirfd/flock implementation and error semantics."""

    def open_files(self, root: Path) -> SafeFiles:
        return SafeFiles(root)

    @contextmanager
    def initialize(self, root: Path, *, allow_empty: bool) -> Iterator[FileAccess]:
        require_safe_files_runtime()  # Never create a directory on an unsupported runtime.
        if type(allow_empty) is not bool:
            raise ContextError("invalid_argument", "目录初始化范围必须明确指定。")
        root = root.absolute()
        if root.is_symlink() or (
            root.exists() and (not allow_empty or not root.is_dir() or any(root.iterdir()))
        ):
            raise _existing(allow_empty)
        root.mkdir(parents=True, exist_ok=allow_empty, mode=0o700)
        with SafeFiles(root) as files:
            yield files

    def writer(self, root: Path, *, expected_identity: object | None = None) -> WriterLease:
        return WriterLease(root, _expected_identity=expected_identity)

    def executor(self, root: Path, *, expected_identity: object | None = None) -> ExecutorLease:
        return ExecutorLease(root, _expected_identity=expected_identity)

    def worker(
        self,
        root: Path,
        *,
        model_calls: bool,
        source_sync: bool,
        expected_identity: object | None = None,
    ) -> WorkerLease:
        return WorkerLease(
            root,
            allow_model_calls=model_calls,
            allow_source_sync=source_sync,
            _expected_identity=expected_identity,
        )

    def observe_worker(self, files: FileAccess) -> dict:
        if not isinstance(files, SafeFiles):
            raise ContextError("invalid_workspace", "文件后端与后台所有权后端不一致。")
        return WorkerLease.observe(files)


def _existing(allow_empty: bool) -> ContextError:
    return ContextError(
        "workspace_not_empty" if allow_empty else "secret_directory_exists",
        "只初始化已确认的空目录，不覆盖已有资料。"
        if allow_empty
        else "只创建新的独立凭据目录，不修改已有目录权限。",
    )


def storage_backend() -> StorageBackend:
    """Real-runtime selection only; no OS override from config or a request."""
    report = require_safe_files_runtime()
    if report.safe_files_backend != "posix_dirfd_v1":
        raise ContextError("unsupported_platform", "此运行环境的完整存储接线尚未验收。")
    return PosixStorage()


class WindowsFileAccess:
    """Expose Path and opaque identity, never an integer pretending to be an fd."""

    def __init__(
        self, root: Path, *, _native: WindowsNative | None = None, _files: WindowsFiles | None = None
    ):
        from collection_context.infrastructure.windows_files import WindowsFiles

        self.root = root
        self._files = _files if _files is not None else WindowsFiles(str(root), _native=_native)
        if self._files.root != str(root):
            raise ContextError("invalid_workspace", "文件后端不属于指定资料根。")
        self.identity = self._files.identity

    def check_root(self) -> None:
        self._files.check_root()

    def require_private_root(self) -> None:
        self._files.require_private_root()

    def entry_exists(self, relative: str) -> bool:
        return self._files.entry_exists(relative)

    def file_size(self, relative: str) -> int:
        return self._files.file_size(relative)

    def list_directory(self, relative: str, *, max_entries: int = 10_000) -> list[dict[str, str]]:
        return self._files.list_directory(relative, max_entries=max_entries)

    def read(self, relative: str, *, max_bytes: int = 16_000_000, private: bool = False) -> bytes:
        return self._files.read(relative, max_bytes=max_bytes, private=private)

    def write(self, relative: str, data: bytes, *, replace: bool = False) -> None:
        self._files.write(relative, data, replace=replace)

    def unlink(self, relative: str) -> None:
        self._files.unlink(relative)

    def read_chunks(
        self,
        relative: str,
        *,
        max_bytes: int = MAX_STREAM_BYTES,
        private: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> AbstractContextManager[Iterator[bytes]]:
        return self._files.read_chunks(
            relative, max_bytes=max_bytes, private=private, check_cancel=check_cancel
        )

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
        self._files.write_chunks(
            relative,
            chunks,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            replace=replace,
            check_cancel=check_cancel,
        )

    def close(self) -> None:
        self._files.close()

    def __enter__(self) -> Self:
        self.check_root()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _WindowsLease:
    def __init__(self, root: Path, native_lease: _WindowsFileLease):
        self._lease = native_lease
        try:
            self.files = WindowsFileAccess(root, _files=native_lease.files)
        except BaseException:
            native_lease.close()
            raise

    def check(self) -> None:
        self._lease.check()

    def close(self) -> None:
        self._lease.close()

    def __enter__(self) -> Self:
        self.check()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WindowsStorage:
    """Native files and kernel leases composed behind the business contract.

    Not selected by storage_backend yet. _native is only an offline test seam;
    omitting it delegates platform and ABI checks to WindowsNative itself.
    """

    def __init__(self, *, _native: WindowsNative | None = None):
        from collection_context.infrastructure.windows_native import WindowsNative

        self._native = _native if _native is not None else WindowsNative()

    def open_files(self, root: Path) -> WindowsFileAccess:
        return WindowsFileAccess(root, _native=self._native)

    @contextmanager
    def initialize(self, root: Path, *, allow_empty: bool) -> Iterator[FileAccess]:
        if type(allow_empty) is not bool:
            raise ContextError("invalid_argument", "目录初始化范围必须明确指定。")
        if allow_empty:
            try:
                existing = self.open_files(root)
            except ContextError as error:
                if error.code != "not_found":
                    raise
            else:
                with existing:
                    existing.require_private_root()
                    with self._native.open_root_listing(str(root)) as listing:
                        before = self._native.information(listing)
                        if before.identity != existing.identity:
                            raise ContextError("storage_unavailable", "空目录路径身份发生变化。")
                        if self._native.list_directory(listing, max_entries=100_000):
                            raise _existing(True)
                        if self._native.information(listing) != before:
                            raise ContextError("version_changed", "空目录检查期间发生变化；未初始化。")
                    existing.check_root()
                    yield existing
                return
        from collection_context.infrastructure.windows_initialization import initialize_windows_root

        try:
            created = initialize_windows_root(str(root), _native=self._native)
        except ContextError as error:
            if error.code in {"directory_exists", "write_conflict", "workspace_not_empty"}:
                raise _existing(allow_empty) from None
            raise
        with created:
            with self.open_files(root) as files:
                created.check()
                if files.identity != created.identity:
                    raise ContextError("storage_unavailable", "新目录身份变化；未初始化资料或凭据。")
                files.require_private_root()
                yield files
                created.check()

    def writer(self, root: Path, *, expected_identity: object | None = None) -> _WindowsLease:
        from collection_context.infrastructure.windows_ownership import WindowsWriterLease

        return _WindowsLease(
            root, WindowsWriterLease(str(root), _native=self._native, _expected_identity=expected_identity)
        )

    def executor(self, root: Path, *, expected_identity: object | None = None) -> _WindowsLease:
        from collection_context.infrastructure.windows_ownership import WindowsExecutorLease

        return _WindowsLease(
            root, WindowsExecutorLease(str(root), _native=self._native, _expected_identity=expected_identity)
        )

    def worker(
        self,
        root: Path,
        *,
        model_calls: bool,
        source_sync: bool,
        expected_identity: object | None = None,
    ) -> _WindowsLease:
        from collection_context.infrastructure.windows_ownership import WindowsWorkerLease

        return _WindowsLease(
            root,
            WindowsWorkerLease(
                str(root),
                allow_model_calls=model_calls,
                allow_source_sync=source_sync,
                _native=self._native,
                _expected_identity=expected_identity,
            ),
        )

    def observe_worker(self, files: FileAccess) -> dict:
        from collection_context.infrastructure.windows_ownership import WindowsWorkerLease

        if not isinstance(files, WindowsFileAccess) or files._files.native is not self._native:
            raise ContextError("invalid_workspace", "文件后端与后台所有权后端不一致。")
        return WindowsWorkerLease.observe(files._files)
