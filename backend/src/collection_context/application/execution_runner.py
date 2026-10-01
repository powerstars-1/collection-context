"""Explicit in-process serial execution; not a service, signal handler, or fee default."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import Any, Protocol

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.session import source_session
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import FATAL_CODES, WAIT_CODES, BackgroundWorker

_SAFE_CODES = (
    FATAL_CODES
    | WAIT_CODES
    | {
        "worker_busy",
        "worker_unavailable",
        "browser_busy",
        "browser_unavailable",
        "browser_start_failed",
        "runtime_dependency_missing",
        "runtime_dependency_integrity",
        "unsafe_secret_permissions",
        "unsafe_login_profile",
        "foreign_login_profile",
        "runtime_directory_overlap",
        "execution_directory_overlap",
        "execution_failed",
    }
)
_JOB_STATES = {"succeeded", "partial", "failed", "blocked", "cancelled"}
SourceFactory = Callable[[], AbstractContextManager[DouyinBrowserSource]]


class _Operation(Protocol):
    def serve(self, **kwargs: Any) -> dict[str, Any]: ...


OperationFactory = Callable[[LibraryStore, FileSecrets | None, SourceFactory | None, Path | None], _Operation]
Directories = tuple[Path, Path | None, Path | None, Path | None]


def _deny_models(_: str) -> str:
    raise ContextError("processing_authorization_required", "来源同步未授权模型请求或秘密读取。")


class ExecutionRunner:
    """Fixed paths and explicit permissions; all runtime handles belong to one thread.

    Construction only resolves directory boundaries; it does not open the library
    or secrets, initialize directories, contact a source, or invoke a model. Path
    resolution can still wait for OS access, so callers must not put it on Tk's
    main thread. `_operation_factory` is a private offline-test seam, never a
    request/job input. Stop prevents later polling/admission through the existing
    worker contract; an in-flight stage may finish and retain its checkpoints.
    No automatic restart or retry is performed by this lifecycle wrapper.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        credential_dir: Path | None = None,
        browser_dir: Path | None = None,
        runtime_dir: Path | None = None,
        _operation_factory: OperationFactory | None = None,
    ) -> None:
        self._directories = self._check_paths(workspace, credential_dir, browser_dir, runtime_dir)
        self._operation_factory = _operation_factory or self._operation
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False
        self._state = "idle"
        self._allow_models = False
        self._allow_sources = False
        self._handled = 0
        self._error_code: str | None = None

    @staticmethod
    def _check_paths(
        workspace: Path, credential_dir: Path | None, browser_dir: Path | None, runtime_dir: Path | None
    ) -> Directories:
        paths = (workspace, credential_dir, browser_dir, runtime_dir)
        if not isinstance(workspace, Path) or any(
            path is not None and (not isinstance(path, Path) or not path.is_absolute()) for path in paths
        ):
            raise ContextError("invalid_argument", "执行器目录须为固定的绝对路径。")
        try:
            resolved = tuple(path.resolve() if path is not None else None for path in paths)
        except Exception:
            raise ContextError("execution_directory_unavailable", "执行器目录边界无法确认。") from None
        present = [path for path in resolved if path is not None]
        if any(
            left.is_relative_to(right) or right.is_relative_to(left)
            for index, left in enumerate(present)
            for right in present[index + 1 :]
        ):
            raise ContextError(
                "execution_directory_overlap", "资料库、凭据、账号和运行依赖目录必须彼此分开。"
            )
        assert resolved[0] is not None
        return resolved[0], resolved[1], resolved[2], resolved[3]

    @property
    def workspace(self) -> Path:
        return self._directories[0]

    @property
    def credential_dir(self) -> Path | None:
        return self._directories[1]

    @property
    def browser_dir(self) -> Path | None:
        return self._directories[2]

    @property
    def runtime_dir(self) -> Path | None:
        return self._directories[3]

    def _public(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "active": bool(self._thread and self._thread.is_alive()),
            "closed": self._closed,
            "allow_model_calls": self._allow_models,
            "allow_source_sync": self._allow_sources,
            "handled": self._handled,
            "error_code": self._error_code,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._public()

    def start(
        self,
        *,
        allow_model_calls: bool = False,
        allow_source_sync: bool = False,
        execution_confirmed: bool = False,
    ) -> dict[str, Any]:
        if any(
            type(flag) is not bool for flag in (allow_model_calls, allow_source_sync, execution_confirmed)
        ):
            raise ContextError("invalid_argument", "执行和来源、模型授权须为明确布尔值。")
        if execution_confirmed is not True or not (allow_model_calls or allow_source_sync):
            raise ContextError("execution_confirmation_required", "请明确确认本次后台执行及允许的能力。")
        if allow_model_calls and self.credential_dir is None:
            raise ContextError("credential_setup_required", "模型执行需要固定的独立凭据目录。")
        if allow_source_sync and self.browser_dir is None:
            raise ContextError("source_setup_required", "来源执行需要固定的独立账号目录。")
        with self._lock:
            if self._closed:
                raise ContextError("execution_closed", "此执行器已关闭，不能重新启动。")
            if self._thread and self._thread.is_alive():
                raise ContextError("execution_busy", "已有后台执行进行中，请先停止并等待实际结束。")
            self._stop = threading.Event()
            self._state = "starting"
            self._allow_models, self._allow_sources = allow_model_calls, allow_source_sync
            self._handled, self._error_code = 0, None
            self._thread = threading.Thread(
                target=self._run,
                args=(allow_model_calls, allow_source_sync, self._stop),
                name="collection-context-owned-execution",
                daemon=False,
            )
            try:
                self._thread.start()
            except BaseException:
                self._state, self._error_code = "failed", "execution_start_failed"
                raise ContextError("execution_start_failed", "后台执行未启动；未显示内部异常。") from None
            return self._public()

    @staticmethod
    def _operation(
        store: LibraryStore,
        secrets: FileSecrets | None,
        source_factory: SourceFactory | None,
        runtime_dir: Path | None,
    ) -> BackgroundWorker:
        workflow = ExtractionWorkflow(store, secrets.get if secrets is not None else _deny_models)
        sync = (
            SynchronizationWorkflow(store, source_factory, runtime_dir=runtime_dir)
            if source_factory is not None
            else None
        )
        return BackgroundWorker(workflow, sync, addition_authority=AccessRegistry(store).authorize_add)

    def _event(self, event: dict[str, Any]) -> None:
        # Consume only fixed lifecycle tags. Never keep payloads, IDs, bodies,
        # upstream details, arbitrary error codes, or an event/log buffer.
        with self._lock:
            if event.get("event") == "worker_started" and not self._stop.is_set():
                self._state = "running"
            elif event.get("event") == "job_finished" and event.get("state") in _JOB_STATES:
                self._handled += 1

    def _run(self, allow_models: bool, allow_sources: bool, stop: threading.Event) -> None:
        state, error_code = "stopped", None
        try:
            if not stop.is_set():
                # Recheck boundaries after explicit authorization; filesystem
                # aliases may have changed since construction. Never initialize.
                self._check_paths(self.workspace, self.credential_dir, self.browser_dir, self.runtime_dir)
                with ExitStack() as handles:
                    if stop.is_set():
                        return
                    store = LibraryStore(self.workspace)
                    handles.callback(store.close)
                    if stop.is_set():
                        return
                    secrets = None
                    if allow_models:
                        assert self.credential_dir is not None
                        secrets = FileSecrets(self.credential_dir)
                        handles.callback(secrets.close)
                    if stop.is_set():
                        return
                    source_factory: SourceFactory | None = None
                    if allow_sources:
                        profile = self.browser_dir
                        assert profile is not None

                        def source_factory():
                            return source_session(store, profile, headless=True, runtime_dir=self.runtime_dir)

                    operation = self._operation_factory(store, secrets, source_factory, self.runtime_dir)
                    if not stop.is_set():
                        operation.serve(
                            allow_model_calls=allow_models,
                            allow_source_sync=allow_sources,
                            once=False,
                            poll_seconds=5,
                            max_jobs=1,
                            stop=stop,
                            emit=self._event,
                        )
        except ContextError as error:
            state = "failed"
            error_code = error.code if error.code in _SAFE_CODES else "execution_failed"
        except BaseException:
            state, error_code = "failed", "execution_failed"
        finally:
            with self._lock:
                self._state, self._error_code = state, error_code

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            if self._thread and self._thread.is_alive():
                self._state = "stopping"
            return self._public()

    def close(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None and (
            type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0
        ):
            raise ContextError("invalid_argument", "关闭等待时间须为非负有限秒数。")
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
            if thread and thread.is_alive():
                self._state = "stopping"
        if thread and thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        # A timeout never means the non-daemon worker or its handles terminated.
        return self.status()
