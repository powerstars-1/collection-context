"""Owner-triggered bounded browser observation, not synchronization or model execution."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError, utc_now
from collection_context.infrastructure.browser import BrowserSession
from collection_context.library.store import LibraryStore
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.workflows.connection import ConnectionWorkflow


def separate_browser(workspace: Path, profile: Path) -> None:
    library, private = workspace.resolve(), profile.resolve()
    if library.is_relative_to(private) or private.is_relative_to(library):
        raise ContextError("login_directory_overlap", "独立登录目录不能与资料库重叠，不随资料导出。")


class ConnectionRunner:
    """One live thread per server; each observation owns its store and browser lifecycle."""

    def __init__(
        self,
        workspace: Path,
        profile: Path,
        *,
        headless: bool = False,
        operation: Callable[[str, threading.Event], None] | None = None,
    ):
        if type(headless) is not bool:
            raise ContextError("invalid_argument", "浏览器方式必须明确。")
        separate_browser(workspace, profile)
        self.workspace, self.profile = workspace.absolute(), profile.absolute()
        self.headless = headless
        self._operation = operation or self._observe
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._status: dict[str, Any] = {
            "run_id": None,
            "mode": None,
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "error_code": None,
            "browser_ready_at": None,
        }

    def _observe(self, mode: str, stop: threading.Event) -> None:
        # Never share a Playwright object across threads or use the request store after shutdown.
        store = LibraryStore(self.workspace)
        try:
            self._check_stop(stop)
            with BrowserSession(self.profile, headless=self.headless) as browser:
                with self._lock:
                    if not stop.is_set():
                        self._status.update(state="running", browser_ready_at=utc_now())
                ConnectionWorkflow(store, DouyinBrowserSource(browser)).observe(
                    interactive=mode == "login",
                    timeout=300 if mode == "login" else 15,
                    discover=mode == "folders",
                    cancelled=stop.is_set,
                )
        finally:
            store.close()

    @staticmethod
    def _check_stop(stop: threading.Event) -> None:
        if stop.is_set():
            raise ContextError("connection_cancelled", "已取消本次连接观察，未启动同步或模型。")

    def _public(self) -> dict:
        return {
            **self._status,
            "enabled": not self._closed,
            "headless": self.headless,
            "active": bool(self._thread and self._thread.is_alive()),
            "model_requests": 0,
        }

    def status(self) -> dict:
        with self._lock:
            return self._public()

    def start(self, *, mode: str, source_confirmed: bool) -> dict:
        if source_confirmed is not True:
            raise ContextError("source_confirmation_required", "请确认允许本次独立浏览器访问本人账号页面。")
        if not isinstance(mode, str) or mode not in {"login", "check", "folders"}:
            raise ContextError("invalid_argument", "连接方式只支持登录、验证或发现收藏夹。")
        if mode == "login" and self.headless:
            raise ContextError(
                "desktop_login_required", "后台无可见桌面，只能验证已有独立登录；远程登录仍需另验。"
            )
        with self._lock:
            if self._closed:
                raise ContextError("connection_disabled", "后台连接入口已关闭。")
            if self._thread and self._thread.is_alive():
                raise ContextError("connection_busy", "已有连接观察进行中，请查看或取消，不重复弹窗口。")
            self._stop = threading.Event()
            self._status = {
                "run_id": "c_" + secrets.token_hex(16),
                "mode": mode,
                "state": "starting",
                "started_at": utc_now(),
                "finished_at": None,
                "error_code": None,
                "browser_ready_at": None,
            }
            self._thread = threading.Thread(target=self._run, args=(mode, self._stop), daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._status.update(
                    state="failed", finished_at=utc_now(), error_code="connection_start_failed"
                )
                raise ContextError("connection_start_failed", "连接任务未启动，请刷新后明确重试。") from None
            return self._public()

    def _run(self, mode: str, stop: threading.Event) -> None:
        error_code = None
        state = "completed"
        try:
            self._check_stop(stop)
            self._operation(mode, stop)
        except ContextError as error:
            state = "cancelled" if error.code == "connection_cancelled" else "failed"
            error_code = error.code
        except Exception:
            state, error_code = "failed", "connection_observation_failed"
        finally:
            with self._lock:
                self._status.update(state=state, error_code=error_code, finished_at=utc_now())

    def cancel(self, *, run_id: str) -> dict:
        with self._lock:
            if not isinstance(run_id, str) or run_id != self._status["run_id"]:
                raise ContextError("connection_changed", "连接观察已改变，请刷新后取消。")
            if self._thread and self._thread.is_alive():
                self._stop.set()
                self._status["state"] = "cancelling"
            return self._public()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread and thread.ident is not None:
            thread.join(timeout=35)
        # A still-finishing thread has its own resources. No new observation is admitted.
