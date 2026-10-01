"""Explicit local desktop startup with an in-process, main-thread GUI bridge.

No GUI operation runs on the service thread.  Secrets only cross this process's
queue; no subprocess, clipboard, environment, file, URL, or log is used.  This
source module does not imply that a complete desktop distribution was built.
"""

from __future__ import annotations

import argparse
import io
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

from collection_context.application.contracts import ContextError
from collection_context.diagnostics import default_workspace, startup_report
from collection_context.native_bootstrap import _TokenWindow, present_owner_token

_SAFE_ERRORS = {
    "workspace_initialization_required": "请明确同意建立一个空资料库。",
    "workspace_not_usable": "资料库无法使用；原有目录未被迁移。",
    "workspace_not_initialized": "此目录不是可用资料库。",
    "dependency_required": "启动依赖未就绪；没有自动安装。",
    "port_unavailable": "端口不可用，请选择其他端口。",
    "owner_rollback_failed": "首次权限撤销失败；请停止使用该库并检查权限状态。",
    "owner_presentation_failed": "口令展示失败，本次新权限已撤销。",
    "owner_confirmation_cancelled": "已取消首次口令确认，本次新权限已撤销。",
    "desktop_display_required": "本机窗口不可用；未继续创建后台服务。",
}


@dataclass
class DesktopStatus:
    state: str
    message: str
    code: str | None = None


@dataclass(eq=False)
class OwnerPrompt:
    """A single pending callback; repr and status must never expose the token."""

    token: str = field(repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    accepted: bool = False
    failed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def resolve(self, accepted: bool, *, failed: bool = False) -> None:
        with self._lock:
            if not self.done.is_set():
                self.accepted = accepted is True
                self.failed = failed
                self.token = ""
                self.done.set()


class _DiscardOutput(io.TextIOBase):
    """Do not keep arbitrary launcher output; only signal verified readiness."""

    def __init__(self, ready: Callable[[], None]) -> None:
        self.ready = ready

    def write(self, text: str) -> int:
        if text.startswith("管理页已就绪："):
            self.ready()
        return len(text)


class DesktopController:
    """GUI-independent lifecycle. Construction and selection never start work."""

    def __init__(self, *, launch_service: Callable[..., int] | None = None) -> None:
        self.events: queue.Queue[DesktopStatus | OwnerPrompt] = queue.Queue()
        self._launch_service = launch_service
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending: set[OwnerPrompt] = set()
        self.stop_event = threading.Event()
        self.state = "idle"

    @property
    def active(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _status(self, state: str, message: str, code: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.events.put(DesktopStatus(state, message, code))

    def start(self, workspace: Path, *, port: int, initialize_empty: bool) -> None:
        if self.active:
            raise ContextError("desktop_already_running", "请先停止当前服务。")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ContextError("invalid_port", "请填写 1 到 65535 的端口。")
        self.stop_event = threading.Event()
        self._status("starting", "正在启动本机服务；不会同步来源、安装依赖或调用模型。")
        self._worker = threading.Thread(
            target=self._run,
            args=(workspace, port, initialize_empty),
            name="collection-context-desktop-service",
            daemon=False,
        )
        self._worker.start()

    def _ready(self) -> None:
        with self._lock:
            if not self.stop_event.is_set() and self.state == "starting":
                self.state = "running"
                self.events.put(DesktopStatus("running", "管理页已就绪；请在本机浏览器登录。"))

    def _present_from_worker(self, token: str) -> bool:
        prompt = OwnerPrompt(token)
        with self._lock:
            if self.stop_event.is_set():
                prompt.resolve(False)
                return False
            self._pending.add(prompt)
            self.events.put(prompt)
        try:
            while not prompt.done.wait(0.1):
                if self.stop_event.is_set():
                    prompt.resolve(False)
            if prompt.failed:
                raise ContextError("owner_presentation_failed", "首次口令窗口未能完成展示。")
            return prompt.accepted is True and not self.stop_event.is_set()
        finally:
            prompt.token = ""
            with self._lock:
                self._pending.discard(prompt)

    def present_on_main_thread(self, prompt: OwnerPrompt, presenter: Callable[[str], bool]) -> None:
        if threading.current_thread() is not threading.main_thread():
            prompt.resolve(False)
            raise ContextError("desktop_display_required", "口令必须在本机界面主线程展示。")
        if prompt.done.is_set() or self.stop_event.is_set():
            prompt.resolve(False)
            return
        try:
            accepted = presenter(prompt.token) is True
        except BaseException:
            prompt.resolve(False, failed=True)
            return
        prompt.resolve(accepted and not self.stop_event.is_set())

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            for prompt in self._pending:
                prompt.resolve(False)
            if self.active:
                self.state = "stopping"
                self.events.put(DesktopStatus("stopping", "正在停止，请等待后台服务退出。"))

    def join(self, timeout: float | None = None) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    def _run(self, workspace: Path, port: int, initialize_empty: bool) -> None:
        try:
            service = self._launch_service
            if service is None:
                from collection_context.launcher import launch

                service = launch
            result = service(
                workspace,
                port=port,
                initialize_empty=initialize_empty,
                no_browser=False,
                output=cast(TextIO, _DiscardOutput(self._ready)),
                owner_presenter=self._present_from_worker,
                stop_event=self.stop_event,
            )
            if result == 0:
                self._status("stopped", "后台服务已停止；可以重新启动或退出。")
            else:
                self._status("failed", "启动未完成；请检查资料库、依赖和端口。", "startup_failed")
        except ContextError as error:
            code = error.code if error.code in _SAFE_ERRORS else "startup_failed"
            self._status("failed", _SAFE_ERRORS.get(code, "启动未完成；未显示内部异常。"), code)
        except BaseException:
            self._status("failed", "启动未完成；未显示内部异常，请检查本机运行环境。", "startup_failed")
        finally:
            with self._lock:
                for prompt in self._pending:
                    prompt.resolve(False)


def capability_description(report: dict[str, Any]) -> str:
    """Fixed labels only; diagnostics/errors cannot inject secrets into the UI."""
    labels = {
        "python_runtime": "Python 运行时",
        "management_api": "管理接口",
        "web_server": "本机服务",
        "source_browser": "来源浏览器",
        "media_ffmpeg": "视频处理工具",
        "local_ocr": "本地 OCR（运行库检查，权重与精度另验）",
    }
    descriptions = []
    for item in report["capabilities"]["dependencies"]:
        name = labels.get(item["role"], "其他依赖")
        if item["role"] == "source_browser":
            state = "功能未验证"
            if item.get("package_available"):
                state += "，已发现模块"
        elif item["role"] in {"media_ffmpeg", "local_ocr"}:
            state = "已发现，待功能验证" if item["available"] else "未发现"
        else:
            state = "可用" if item["available"] else "未发现"
        suffix = "（启动必需）" if item["required_for_start"] else "（可降级，未自动安装）"
        descriptions.append(f"{name}：{state}{suffix}")
    return "\n".join(descriptions)


class _DesktopWindow:
    def __init__(self, workspace: Path, port: int) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.root = tk.Tk()
        self.controller = DesktopController()
        self.close_requested = False
        self.destroyed = False
        self.token_window: _TokenWindow | None = None
        self.filedialog = filedialog
        self.messagebox = messagebox
        try:
            self.root.report_callback_exception = self._callback_failed
            self.root.title("收藏上下文 · 本机启动")
            self.root.geometry("760x560")
            frame = ttk.Frame(self.root, padding=24)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="选择资料库，明确启动", font=("TkDefaultFont", 18)).pack(anchor="w")
            ttk.Label(
                frame,
                text="只启动本机管理页。不自动同步、不调用模型、不安装组件。",
            ).pack(anchor="w", pady=10)
            self.workspace = tk.StringVar(master=self.root, value=str(workspace))
            self.port = tk.StringVar(master=self.root, value=str(port))
            self.initialize = tk.BooleanVar(master=self.root, value=False)
            path_row = ttk.Frame(frame)
            path_row.pack(fill="x", pady=5)
            self.path_entry = ttk.Entry(path_row, textvariable=self.workspace)
            self.path_entry.pack(side="left", fill="x", expand=True)
            self.choose_button = ttk.Button(path_row, text="选资料库", command=self._choose)
            self.choose_button.pack(side="right", padx=8)
            self.init_check = ttk.Checkbutton(
                frame, text="同意建立新的空资料库（不覆盖非空目录）", variable=self.initialize
            )
            self.init_check.pack(anchor="w", pady=8)
            port_row = ttk.Frame(frame)
            port_row.pack(fill="x")
            ttk.Label(port_row, text="本机端口：").pack(side="left")
            self.port_entry = ttk.Entry(port_row, textvariable=self.port, width=8)
            self.port_entry.pack(side="left")
            self.dependencies = tk.StringVar(master=self.root)
            ttk.Label(frame, textvariable=self.dependencies, justify="left").pack(anchor="w", pady=16)
            self.status = tk.StringVar(master=self.root, value="尚未启动。选择目录不会写入资料。")
            ttk.Label(frame, textvariable=self.status, wraplength=680).pack(anchor="w", pady=12)
            actions = ttk.Frame(frame)
            actions.pack(fill="x", side="bottom")
            self.start_button = ttk.Button(actions, text="启动并打开管理页", command=self._start)
            self.start_button.pack(side="left")
            self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
            self.stop_button.pack(side="left", padx=10)
            ttk.Button(actions, text="退出", command=self._close).pack(side="right")
            self.root.protocol("WM_DELETE_WINDOW", self._close)
            self._refresh_dependencies()
            self.root.after(75, self._poll)
        except BaseException:
            try:
                self.root.destroy()
            except BaseException:
                pass
            raise ContextError("desktop_display_required", "本机启动窗口未能建立。") from None

    def _callback_failed(self, *_exception_details: Any) -> None:
        # Tk's default reporter formats arbitrary exception text to stderr.
        # This handler never retains or interpolates those details.
        self.close_requested = True
        self._stop()
        try:
            self.status.set("界面操作未完成，正在停止后台服务；未显示内部异常。")
            self.root.after(75, self._poll)
        except BaseException:
            pass

    def _choose(self) -> None:
        selected = self.filedialog.askdirectory(parent=self.root, mustexist=False)
        if selected:
            self.workspace.set(selected)
            self._refresh_dependencies()

    def _parameters(self) -> tuple[Path, int]:
        if not self.workspace.get().strip():
            raise ValueError
        port = int(self.port.get())
        if not 1 <= port <= 65_535:
            raise ValueError
        return Path(self.workspace.get()).expanduser(), port

    def _refresh_dependencies(self) -> dict[str, Any] | None:
        try:
            workspace, port = self._parameters()
            report = startup_report(workspace, port)
            self.dependencies.set(capability_description(report))
            return report
        except Exception:
            self.dependencies.set("依赖检查未完成；没有安装或下载任何组件。")
            return None

    def _start(self) -> None:
        if self.controller.active or self.close_requested:
            return
        report = self._refresh_dependencies()
        if report is None:
            self.status.set("请检查资料库路径和端口。")
            return
        if not report["capabilities"]["ready_for_management_page"]:
            self.status.set("启动依赖未就绪；请配置运行环境后重试。")
            return
        if not report["listen"]["available"]:
            self.status.set("端口不可用，请选择其他端口。")
            return
        state = report["workspace"]["state"]
        if state in {"missing", "empty"}:
            if not self.initialize.get():
                self.status.set("此位置没有资料库，请先明确同意建立新的空资料库。")
                return
            if not self.messagebox.askyesno(
                "建立空资料库", "确认在所选位置建立新的空资料库？不会导入旧资料或调用模型。", parent=self.root
            ):
                self.status.set("已取消，未建立资料库。")
                return
        elif state != "initialized_candidate":
            self.status.set("目录不可用，或含有未识别的资料；不会覆盖或自动迁移。")
            return
        workspace, port = self._parameters()
        self.controller.start(workspace, port=port, initialize_empty=self.initialize.get())
        self._controls(True)

    def _controls(self, active: bool) -> None:
        for widget in (
            self.path_entry,
            self.choose_button,
            self.init_check,
            self.port_entry,
            self.start_button,
        ):
            widget.configure(state="disabled" if active or self.close_requested else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")

    def _stop(self) -> None:
        self.controller.request_stop()
        if self.token_window is not None:
            self.token_window.clear_and_close()

    def _close(self) -> None:
        self.close_requested = True
        self._stop()
        self._controls(self.controller.active)
        if self.controller.active:
            self.status.set("正在停止后台服务；退出前会保持窗口可见。")

    def _present(self, token: str) -> bool:
        try:
            self.token_window = _TokenWindow(parent=self.root)
            return present_owner_token(token, window_factory=lambda: self.token_window)
        finally:
            self.token_window = None

    def _poll(self) -> None:
        if self.destroyed:
            return
        # Schedule before a nested token dialog so Stop/Close remains responsive.
        self.root.after(75, self._poll)
        while True:
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, OwnerPrompt):
                self.controller.present_on_main_thread(event, self._present)
                if self.destroyed:
                    return
            else:
                self.status.set(event.message)
        self._controls(self.controller.active)
        if self.close_requested and not self.controller.active:
            self.destroyed = True
            self.root.destroy()

    def show(self) -> None:
        self.root.mainloop()

    def recover_stopping(self) -> None:
        """Keep an abnormal GUI exit visible until the real service finishes."""
        self.close_requested = True
        self.status.set("停止尚未完成；请等待服务退出。当前进程没有完成退出。")
        self.root.deiconify()
        self.root.after(75, self._poll)
        self.root.mainloop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式打开本机选库/启动/停止窗口；不自动初始化。")
    parser.add_argument("--workspace", type=Path, default=default_workspace())
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    window = None
    result = 0
    try:
        window = _DesktopWindow(args.workspace, args.port)
        window.show()
    except BaseException:
        print("desktop_display_required: 本机 GUI 不可用或运行中断，未显示内部异常。", file=sys.stderr)
        result = 2
    finally:
        if window is not None:
            window.controller.request_stop()
            window.controller.join(timeout=5)
            if window.controller.active:
                print(
                    "desktop_stop_pending: 后台服务尚未完成停止，进程并未退出。",
                    file=sys.stderr,
                )
                try:
                    window.recover_stopping()
                except BaseException:
                    # Destroyed/broken display cannot be repaired by pretending
                    # the live non-daemon service thread is already terminated.
                    print("desktop_stop_pending: 窗口未能恢复，服务仍须完成停止。", file=sys.stderr)
                if window.controller.active:
                    result = 3
    return result


if __name__ == "__main__":
    raise SystemExit(main())
