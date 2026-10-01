"""Explicit local desktop startup with an in-process, main-thread GUI bridge.

No GUI operation runs on the service thread.  Secrets only cross this process's
queue; no subprocess, automatic clipboard export, environment, file, URL, or log is used.  This
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
    "system_secret_unsupported": "当前系统凭据库尚不可用；未改用明文文件。",
    "system_secret_directory_invalid": "系统凭据目录身份不符；没有迁移或读取旧密钥。",
    "credential_backend_mismatch": "凭据目录后端不匹配；未把元数据当成密钥。",
    "credential_backend_unsupported": "当前系统凭据库尚不可用；未改用明文文件。",
    "credential_backend_conflict": "凭据后端身份不匹配；没有迁移或读取旧密钥。",
    "credential_denied": "系统拒绝凭据访问；未自动授权或改用明文文件。",
    "credential_locked": "系统凭据库已锁定；请自行解锁后重试。",
    "credential_unavailable": "系统凭据库不可用；未自动重试或改用明文文件。",
    "launcher_capabilities_invalid": "启动能力配置不完整；执行必须先启用对应配置或登录能力。",
    "launcher_directory_unsafe": "产品配置目录不符合隔离要求；没有修改原目录权限。",
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


@dataclass(frozen=True)
class DiagnosticParameters:
    workspace: str
    port: int
    initialize_empty: bool
    permissions: tuple[bool, bool, bool, bool] = (False, False, False, False)


@dataclass(frozen=True)
class DiagnosticResult:
    generation: int
    parameters: DiagnosticParameters
    workspace: Path | None = field(default=None, repr=False)
    report: dict[str, Any] | None = field(default=None, repr=False)
    runtime_options: dict[str, Any] | None = field(default=None, repr=False)


class DesktopDiagnostics:
    """One bounded, read-only worker; cancellation invalidates results, not syscalls.

    Directory access can wait indefinitely for OS permission or a disconnected
    volume. This daemon never touches Tk, initializes a library, grants access,
    or starts services. Closing the window does not join it or claim its syscall
    returned. At most one check runs; a newer request replaces the pending one.
    Service workers remain non-daemon and have their separate stop lifecycle.
    """

    def __init__(
        self,
        *,
        checker: Callable[..., dict[str, Any]] = startup_report,
        options_checker: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.events: queue.Queue[DiagnosticResult] = queue.Queue()
        self._checker = checker
        self._options_checker = options_checker
        self._lock = threading.Lock()
        self._generation = 0
        self._current: DiagnosticParameters | None = None
        self._pending: tuple[int, DiagnosticParameters] | None = None
        self._running = False
        self._worker: threading.Thread | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._running

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def request(self, parameters: DiagnosticParameters) -> int:
        with self._lock:
            if self._running and parameters == self._current:
                return self._generation
            self._generation += 1
            self._current = parameters
            self._pending = (self._generation, parameters)
            if not self._running:
                self._running = True
                self._worker = threading.Thread(
                    target=self._run, name="collection-context-readonly-diagnostics", daemon=True
                )
                self._worker.start()
            return self._generation

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._current = None
            self._pending = None

    def _run(self) -> None:
        while True:
            with self._lock:
                request = self._pending
                self._pending = None
                if request is None:
                    self._running = False
                    return
            generation, parameters = request
            workspace = None
            report = None
            options = None
            try:
                # Even home expansion and dependency imports belong off Tk's thread.
                workspace = Path(parameters.workspace).expanduser()
                report = self._checker(workspace, parameters.port)
            except BaseException:
                # Never retain or render filesystem/model/OS exception details.
                pass
            try:
                checker = self._options_checker
                if checker is None:
                    from collection_context.application.runtime_setup import runtime_options

                    checker = runtime_options
                options = checker()
            except BaseException:
                # Catalog inspection is independent from management-page readiness.
                pass
            with self._lock:
                if generation == self._generation and parameters == self._current:
                    self.events.put(DiagnosticResult(generation, parameters, workspace, report, options))


_INSTALL_ERRORS = {
    "runtime_install_confirmation": "安装未确认，没有下载组件。",
    "runtime_install_catalog": "此安装项不属于产品固定清单，没有下载。",
    "runtime_install_unavailable": "当前主机或运行库不满足此安装项，没有下载。",
    "runtime_install_cancelled": "安装线程已结束取消处理；未继续发布安装收据。",
    "runtime_install_outcome_unknown": "安装结果未能确认；请检查组件状态，未自动重试。",
    "runtime_install_busy": "组件安装正在被其他操作使用，没有自动重试。",
    "runtime_install_integrity": "组件校验未通过，没有将其声明为可用。",
    "runtime_install_unsafe": "安装位置不符合产品隔离要求，没有修改目录权限。",
    "runtime_download_integrity": "下载内容与固定清单不符，没有安装。",
    "runtime_download_failed": "组件下载未完成，没有自动重试。",
}


class DesktopInstallation:
    """Explicit fixed-catalog installation; no GUI work or arbitrary paths/URLs.

    Stop is cooperative. An in-progress network or filesystem operation may
    still be waiting; the non-daemon thread must really finish before exit.
    A shared operation lock excludes concurrent desktop service startup.
    """

    def __init__(
        self,
        *,
        installer: Callable[..., dict[str, Any]] | None = None,
        options_checker: Callable[[], dict[str, Any]] | None = None,
        operation_lock: Any = None,
    ) -> None:
        self.events: queue.Queue[DesktopStatus] = queue.Queue()
        self._installer = installer
        self._options_checker = options_checker
        self._operation_lock = operation_lock if operation_lock is not None else threading.Lock()
        self._worker: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = "idle"

    @property
    def active(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _status(self, state: str, message: str, code: str | None = None) -> None:
        with self._state_lock:
            self.state = state
            self.events.put(DesktopStatus(state, message, code))

    def start(self, workspace: Path, *, artifact_id: str, installation_confirmed: bool = False) -> None:
        if installation_confirmed is not True:
            raise ContextError(
                "runtime_install_confirmation", _INSTALL_ERRORS["runtime_install_confirmation"]
            )
        if not isinstance(artifact_id, str) or not artifact_id or self.active:
            raise ContextError("runtime_install_busy", "请先等待当前安装完成，再重新选择固定安装项。")
        if not self._operation_lock.acquire(blocking=False):
            raise ContextError("desktop_operation_busy", "请先停止服务或等待组件安装结束。")
        self.stop_event = threading.Event()
        self._status("installing", "正在安装已确认的单项组件；没有启动浏览器、同步或模型任务。")
        try:
            self._worker = threading.Thread(
                target=self._run,
                args=(workspace, artifact_id),
                name="collection-context-runtime-install",
                daemon=False,
            )
            self._worker.start()
        except BaseException:
            self._operation_lock.release()
            self._status("failed", "安装线程未能启动；未显示内部异常。", "runtime_install_failed")

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._state_lock:
            if self.active and self.state == "installing":
                self.state = "stopping"
                self.events.put(
                    DesktopStatus("stopping", "已请求停止安装；底层下载可能仍在等待，须等真实安装线程结束。")
                )

    def join(self, timeout: float | None = None) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    def _run(self, workspace: Path, artifact_id: str) -> None:
        try:
            from collection_context.application.runtime_setup import install_runtime, runtime_options

            options = (self._options_checker or runtime_options)()
            choice = next((item for item in options["artifacts"] if item["id"] == artifact_id), None)
            if choice is None:
                raise ContextError("runtime_install_catalog", "未选择固定清单项。")
            if choice["state"] != "available":
                raise ContextError("runtime_install_unavailable", "此主机不满足固定安装项。")
            if self.stop_event.is_set():
                raise ContextError("runtime_install_cancelled", "已请求停止。")
            # Resolve paths off Tk; callers cannot select an installation directory.
            result = (self._installer or install_runtime)(
                default_workspace().parent / "runtime",
                library_dir=workspace.expanduser(),
                artifact_id=artifact_id,
                installation_confirmed=True,
                stop=self.stop_event,
            )
            if result.get("state") != "installed" or result.get("static_verified") is not True:
                self._status(
                    "failed", "安装结果未完成静态校验；未将组件声明为可用。", "runtime_install_failed"
                )
            else:
                self._status(
                    "installed",
                    "组件已安装并完成静态校验；浏览器启动、登录、同步和识别功能均未验证，未自动执行。",
                )
        except ContextError as error:
            code = error.code if error.code in _INSTALL_ERRORS else "runtime_install_failed"
            self._status(
                "cancelled" if code == "runtime_install_cancelled" else "failed",
                _INSTALL_ERRORS.get(code, "组件安装未完成；未显示内部异常，没有自动重试。"),
                code,
            )
        except BaseException:
            self._status("failed", "组件安装未完成；未显示内部异常，没有自动重试。", "runtime_install_failed")
        finally:
            self._operation_lock.release()


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

    def __init__(
        self, *, launch_service: Callable[..., int] | None = None, operation_lock: Any = None
    ) -> None:
        self.events: queue.Queue[DesktopStatus | OwnerPrompt] = queue.Queue()
        self._launch_service = launch_service
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._operation_lock = operation_lock if operation_lock is not None else threading.Lock()
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

    def start(
        self,
        workspace: Path,
        *,
        port: int,
        initialize_empty: bool,
        desktop_permissions: tuple[bool, bool, bool, bool] | None = None,
    ) -> None:
        if self.active:
            raise ContextError("desktop_already_running", "请先停止当前服务。")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ContextError("invalid_port", "请填写 1 到 65535 的端口。")
        if desktop_permissions is not None and (
            type(desktop_permissions) is not tuple
            or len(desktop_permissions) != 4
            or any(type(flag) is not bool for flag in desktop_permissions)
        ):
            raise ContextError("launcher_capabilities_invalid", "请选择明确的启动能力。")
        if not self._operation_lock.acquire(blocking=False):
            raise ContextError("desktop_operation_busy", "请先等待组件安装结束或停止当前服务。")
        self.stop_event = threading.Event()
        self._status("starting", "正在按已确认的能力启动本机服务；不会自动安装依赖。")
        try:
            self._worker = threading.Thread(
                target=self._run,
                args=(workspace, port, initialize_empty, desktop_permissions),
                name="collection-context-desktop-service",
                daemon=False,
            )
            self._worker.start()
        except BaseException:
            self._operation_lock.release()
            self._status("failed", "本机服务线程未能启动。", "desktop_display_required")
            raise ContextError("desktop_display_required", "本机服务线程未能启动。") from None

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

    def _run(
        self,
        workspace: Path,
        port: int,
        initialize_empty: bool,
        desktop_permissions: tuple[bool, bool, bool, bool] | None,
    ) -> None:
        try:
            service = self._launch_service
            if service is None:
                from collection_context.launcher import launch

                service = launch
            options: dict[str, Any] = {}
            if desktop_permissions is not None:
                from collection_context.application.launcher_capabilities import default_desktop_capabilities

                # Path checks may wait for OS permission: never perform these on Tk.
                options["capabilities"] = default_desktop_capabilities(
                    workspace.absolute(),
                    allow_model_config=desktop_permissions[0],
                    allow_source_connect=desktop_permissions[1],
                    allow_model_calls=desktop_permissions[2],
                    allow_source_sync=desktop_permissions[3],
                )
            if self.stop_event.is_set():
                self._status("stopped", "启动已取消；未继续建立服务。")
                return
            result = service(
                workspace,
                port=port,
                initialize_empty=initialize_empty,
                no_browser=False,
                output=cast(TextIO, _DiscardOutput(self._ready)),
                owner_presenter=self._present_from_worker,
                stop_event=self.stop_event,
                **options,
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
            self._operation_lock.release()


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


def runtime_catalog_description(options: dict[str, Any]) -> str:
    """Render only compiled catalog fields; installation does not prove functionality."""
    states = {
        "available": "本机满足安装条件（功能未验证）",
        "host_not_available": "本机系统/架构不支持",
        "os_version_not_available": "本机系统版本不满足",
        "os_version_not_verified": "本机系统版本尚未确认",
        "sdk_version_mismatch": "浏览器运行库版本不匹配",
        "sdk_missing": "缺少浏览器运行库",
        "ocr_runtime_missing": "缺少固定CPU OCR运行库",
        "ocr_runtime_version_mismatch": "CPU OCR运行库版本与固定权重组合不匹配",
        "bundled_component_missing": "此程序未携带该组件，请使用含组件的安装包",
        "bundled_component_invalid": "随包组件校验前置条件不符，不可安装",
    }
    missing = {
        "desktop_browser": "可见登录浏览器",
        "ffmpeg_pair": "视频/音频处理工具",
        "ocr_weights": "OCR 识别权重",
        "Windows": "Windows 固定安装包",
        "Linux": "Linux 固定安装包",
    }
    lines = ["安装仅处理固定清单中的单项，不授予同步或模型权限。\n"]
    for item in options.get("artifacts", ()):
        size = item.get("download_bytes")
        volume = f"{size:,} 字节" if type(size) is int else "尚未提供"
        lines.extend(
            (
                str(item["name"]),
                "主机状态：" + states.get(item.get("state"), "状态尚未确认，不可选择安装"),
                "目标："
                + str(item.get("host_system", "未提供"))
                + " / "
                + str(item.get("host_arch", "未提供")),
                "下载体积：" + volume,
                *(
                    (
                        f"随包归档：{item['archive_bytes']:,} 字节",
                        f"展开体积：{item['payload_bytes']:,} 字节",
                    )
                    if item.get("delivery") == "bundled"
                    and type(item.get("archive_bytes")) is int
                    and type(item.get("payload_bytes")) is int
                    else ()
                ),
                "交付方式："
                + (
                    "随包携带，无网络下载；来源地址是"
                    + (
                        "上游软件包页面"
                        if item.get("source_url_kind") == "upstream_package_not_binary_download"
                        else "上游源码"
                    )
                    if item.get("delivery") == "bundled"
                    else "固定HTTPS下载"
                ),
                "来源：" + str(item.get("source_url", "未提供")),
                "许可：" + str(item.get("license_notice", "许可尚未提供")),
                "静态安装成功仍不等于登录、同步或识别可用。\n",
            )
        )
    lines.append(
        "缺项：" + "、".join(missing.get(name, str(name)) for name in options.get("not_available", ()))
    )
    return "\n".join(lines)


class _DesktopWindow:
    def __init__(self, workspace: Path, port: int) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.root = tk.Tk()
        operation_lock = threading.Lock()
        self.controller = DesktopController(operation_lock=operation_lock)
        self.installation = DesktopInstallation(operation_lock=operation_lock)
        self.diagnostics = DesktopDiagnostics()
        self._diagnostic_cache: DiagnosticResult | None = None
        self._start_intent: int | None = None
        self._install_generation = 0
        self.install_window: Any = None
        self.close_requested = False
        self.destroyed = False
        self.token_window: _TokenWindow | None = None
        self.filedialog = filedialog
        self.messagebox = messagebox
        try:
            self.root.report_callback_exception = self._callback_failed
            self.root.title("收藏上下文 · 本机启动")
            self.root.geometry("760x720")
            frame = ttk.Frame(self.root, padding=24)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="选择资料库，明确启动", font=("TkDefaultFont", 18)).pack(anchor="w")
            ttk.Label(
                frame,
                text="默认只打开管理页。以下能力独立选择；不会自动安装组件。",
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
            self.permission_variables = tuple(tk.BooleanVar(master=self.root, value=False) for _ in range(4))
            self.permission_checks = []
            for label, variable in zip(
                (
                    "允许配置模型（优先系统凭据库；不可用时不自动降级）",
                    "允许在独立浏览器登录抖音（不等于允许同步）",
                    "执行已确认的模型任务（需允许配置模型，可能计费）",
                    "执行已确认的来源任务（需允许登录，可能下载媒体）",
                ),
                self.permission_variables,
                strict=True,
            ):
                checkbox = ttk.Checkbutton(frame, text=label, variable=variable)
                checkbox.pack(anchor="w", pady=3)
                self.permission_checks.append(checkbox)
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
            self.install_button = ttk.Button(actions, text="安装运行组件", command=self._open_installation)
            self.install_button.pack(side="left")
            ttk.Button(actions, text="退出", command=self._close).pack(side="right")
            self.root.protocol("WM_DELETE_WINDOW", self._close)
            for observed_variable in (self.workspace, self.port, self.initialize, *self.permission_variables):
                observed_variable.trace_add("write", self._parameters_changed)
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
        if self.controller.active or self._installation_active() or self.close_requested:
            return
        # The directory dialog runs a nested GUI loop. Invalidate a prior Start
        # intent before opening it, not only after the user chooses a new path.
        self._parameters_changed()
        selected = self.filedialog.askdirectory(parent=self.root, mustexist=False)
        if selected and not self.close_requested:
            self.workspace.set(selected)
            self._refresh_dependencies()

    def _parameters(self) -> tuple[Path, int]:
        if not self.workspace.get().strip():
            raise ValueError
        port = int(self.port.get())
        if not 1 <= port <= 65_535:
            raise ValueError
        return Path(self.workspace.get()), port

    def _diagnostic_parameters(self) -> DiagnosticParameters:
        workspace, port = self._parameters()
        return DiagnosticParameters(str(workspace), port, self.initialize.get() is True, self._permissions())

    def _permissions(self) -> tuple[bool, bool, bool, bool]:
        variables = getattr(self, "permission_variables", None)
        if variables is None:
            return False, False, False, False
        return (
            variables[0].get() is True,
            variables[1].get() is True,
            variables[2].get() is True,
            variables[3].get() is True,
        )

    def _parameters_changed(self, *_trace_details: Any) -> None:
        self.diagnostics.cancel()
        self._diagnostic_cache = None
        self._start_intent = None
        self._install_generation = getattr(self, "_install_generation", 0) + 1
        self.dependencies.set("参数已更新，需重新检测；没有安装或下载任何组件。")
        self.status.set("尚未启动；请重新检测当前资料库和端口。")
        self._controls(self.controller.active)

    def _refresh_dependencies(self) -> dict[str, Any] | None:
        if self.close_requested:
            return None
        try:
            parameters = self._diagnostic_parameters()
            cached = self._diagnostic_cache
            if (
                cached is not None
                and cached.generation == self.diagnostics.generation
                and cached.parameters == parameters
            ):
                return cached.report
            self.diagnostics.request(parameters)
            self.dependencies.set("正在后台只读检测；系统可能正在等待目录授权。没有安装或下载组件。")
            self._controls(self.controller.active)
            return None
        except Exception:
            self.diagnostics.cancel()
            self._diagnostic_cache = None
            self._start_intent = None
            self.dependencies.set("依赖检查未完成；没有安装或下载任何组件。")
            return None

    def _start(self) -> None:
        if self.controller.active or self._installation_active() or self.close_requested:
            return
        report = self._refresh_dependencies()
        if report is None:
            try:
                self._diagnostic_parameters()
            except Exception:
                self.status.set("请检查资料库路径和端口。")
                return
            self._start_intent = self.diagnostics.generation
            self.status.set("检测完成后才会启动；等待期间可修改参数、停止或退出。")
            self._controls(False)
            return
        diagnostics = getattr(self, "diagnostics", None)
        generation = diagnostics.generation if diagnostics is not None else None
        parameters = self._parameters()
        initialize_empty = self.initialize.get() is True
        permissions = self._permissions()
        if permissions[2] and not permissions[0] or permissions[3] and not permissions[1]:
            self.status.set("执行任务前，请同时启用对应的模型配置或抖音登录能力。")
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
        if any(permissions) and not self.messagebox.askyesno(
            "确认本次启动能力",
            "仅启用勾选的能力。模型密钥使用系统凭据库；不可用时不会改存明文。\n"
            "启用执行会处理本库已确认的排队任务；模型任务可能计费，来源任务可能下载。\n"
            "仅配置或登录不会执行任务。确认继续？",
            parent=self.root,
        ):
            self.status.set("已取消本次能力启用，未启动服务。")
            return
        # askyesno has a nested Tk event loop: Stop/Close/parameter edits can run
        # during the dialog. Consent never authorizes an obsolete check.
        if (
            self.close_requested
            or self.controller.active
            or self._installation_active()
            or (diagnostics is not None and diagnostics.generation != generation)
            or parameters != self._parameters()
            or initialize_empty != (self.initialize.get() is True)
            or permissions != self._permissions()
        ):
            return
        workspace, port = parameters
        cached = getattr(self, "_diagnostic_cache", None)
        if cached is not None and cached.workspace is not None:
            workspace = cached.workspace
        self._start_intent = None
        options = {"desktop_permissions": permissions} if any(permissions) else {}
        self.controller.start(workspace, port=port, initialize_empty=initialize_empty, **options)
        self._controls(True)

    def _installation_active(self) -> bool:
        installation = getattr(self, "installation", None)
        return installation is not None and installation.active

    def _open_installation(self) -> None:
        """Main-thread fixed-catalog picker; never accepts an installation URL/path."""
        if self.close_requested or self.controller.active or self._installation_active():
            return
        self._start_intent = None
        self._refresh_dependencies()
        cached = self._diagnostic_cache
        if cached is None or cached.runtime_options is None:
            self.status.set("组件清单仍在只读检测；检测结束后请重新点击安装运行组件。没有下载。")
            return
        if self.install_window is not None:
            self.install_window.lift()
            return
        import tkinter as tk
        from tkinter import ttk

        choices = tuple(cached.runtime_options.get("artifacts", ()))
        generation = self._install_generation
        parameters = cached.parameters
        dialog = tk.Toplevel(self.root)
        self.install_window = dialog
        dialog.title("安装运行组件 · 单项确认")
        dialog.geometry("730x660")
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="固定产品清单 · 不自动下载", font=("TkDefaultFont", 16)).pack(anchor="w")
        details = ttk.Frame(frame)
        details.pack(fill="both", expand=True, pady=12)
        text = tk.Text(details, wrap="word", height=18, font=("TkDefaultFont", 11))
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(details, orient="vertical", command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", runtime_catalog_description(cached.runtime_options))
        text.configure(state="disabled")
        choice = tk.StringVar(master=dialog, value="")
        ttk.Label(frame, text="请选择一项（默认不安装任何组件）：").pack(anchor="w", pady=(8, 4))
        selection_frame = ttk.Frame(frame)
        selection_frame.pack(fill="x", pady=4)
        install_button = ttk.Button(frame, text="确认所选单项安装", state="disabled")
        install_button.pack(side="left")
        ttk.Button(frame, text="关闭清单（不安装）", command=self._dismiss_installation).pack(side="right")

        def selected() -> None:
            item = next((item for item in choices if item["id"] == choice.get()), None)
            install_button.configure(
                state="normal" if item is not None and item["state"] == "available" else "disabled"
            )

        def install() -> None:
            item = next((item for item in choices if item["id"] == choice.get()), None)
            if item is None or item["state"] != "available":
                return
            self._confirm_installation(item, parameters, generation)

        first_available = None
        for item in choices:
            radio = ttk.Radiobutton(
                selection_frame,
                text=item["name"],
                variable=choice,
                value=item["id"],
                command=selected,
                state="normal" if item["state"] == "available" else "disabled",
            )
            radio.pack(anchor="w", pady=2)
            if first_available is None and item["state"] == "available":
                first_available = radio
        install_button.configure(command=install)
        dialog.protocol("WM_DELETE_WINDOW", self._dismiss_installation)
        self._controls(False)
        # Focus does not select a component or grant installation authority.
        # Visible radio options avoid an inaccessible native popup menu on Aqua.
        if first_available is not None:
            first_available.focus_set()

    def _dismiss_installation(self) -> None:
        dialog = getattr(self, "install_window", None)
        self.install_window = None
        self._install_generation = getattr(self, "_install_generation", 0) + 1
        if dialog is not None:
            dialog.destroy()

    def _confirm_installation(
        self, choice: dict[str, Any], parameters: DiagnosticParameters, generation: int
    ) -> None:
        if (
            self.close_requested
            or self.controller.active
            or self._installation_active()
            or generation != self._install_generation
            or parameters != self._diagnostic_parameters()
            or choice.get("state") != "available"
        ):
            self.status.set("资料库或当前操作已变化，请重新打开组件清单；没有下载。")
            return
        if not self.messagebox.askyesno(
            "确认单项组件安装",
            f"仅安装：{choice['name']}\n"
            f"下载体积：{choice['download_bytes']:,} 字节\n来源：{choice['source_url']}\n"
            + (
                "随包携带，无网络下载；以上地址是"
                + (
                    "上游软件包页面"
                    if choice.get("source_url_kind") == "upstream_package_not_binary_download"
                    else "上游源码"
                )
                + "，不是组件下载地址。\n"
                if choice.get("delivery") == "bundled"
                else ""
            )
            + f"许可：{choice['license_notice']}\n"
            "只写入产品固定运行组件目录。安装不会启用任何权限，不启动浏览器、同步或模型。\n"
            "取消或退出须等待底层下载/安装线程真实结束。确认继续？",
            parent=getattr(self, "install_window", None) or self.root,
        ):
            self.status.set("已取消单项安装，没有下载。")
            return
        # A confirmation dialog runs a nested Tk loop: selections/Stop/Close can change.
        if (
            self.close_requested
            or self.controller.active
            or self._installation_active()
            or generation != self._install_generation
            or parameters != self._diagnostic_parameters()
        ):
            self.status.set("旧安装意图已失效，没有下载；请重新选择。")
            return
        self._start_intent = None
        self.diagnostics.cancel()
        self._diagnostic_cache = None
        self._dismiss_installation()
        try:
            self.installation.start(
                Path(parameters.workspace), artifact_id=choice["id"], installation_confirmed=True
            )
        except ContextError:
            self.status.set("安装未能开始，请等待当前服务或安装操作结束；没有自动重试。")
        self._controls(self.controller.active)

    def _controls(self, active: bool) -> None:
        installing = self._installation_active()
        busy = active or installing
        for widget in (
            self.path_entry,
            self.choose_button,
            self.init_check,
            self.port_entry,
            *getattr(self, "permission_checks", ()),
        ):
            widget.configure(state="disabled" if busy or self.close_requested else "normal")
        self.start_button.configure(
            state="disabled" if busy or self.close_requested or self._start_intent is not None else "normal"
        )
        self.stop_button.configure(
            state="normal"
            if busy or self.diagnostics.active or self._start_intent is not None
            else "disabled"
        )
        install_button = getattr(self, "install_button", None)
        if install_button is not None:
            install_button.configure(state="disabled" if busy or self.close_requested else "normal")

    def _stop(self) -> None:
        self._install_generation = getattr(self, "_install_generation", 0) + 1
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            waiting = diagnostics.active
            diagnostics.cancel()
            self._diagnostic_cache = None
            self._start_intent = None
            if waiting and not self.controller.active:
                self.status.set("已取消使用检测结果，未启动服务；系统目录检查可能仍在等待返回。")
        self.controller.request_stop()
        installation = getattr(self, "installation", None)
        if installation is not None:
            installation.request_stop()
        if self.token_window is not None:
            self.token_window.clear_and_close()

    def _close(self) -> None:
        self.close_requested = True
        self._dismiss_installation()
        self._stop()
        self._controls(self.controller.active)
        if self.controller.active or self._installation_active():
            self.status.set("正在停止后台服务或等待安装线程结束；退出前会保持窗口可见。")

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
        self._poll_diagnostics()
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
        installation = getattr(self, "installation", None)
        if installation is not None:
            while True:
                try:
                    event = installation.events.get_nowait()
                except queue.Empty:
                    break
                self.status.set(event.message)
        self._controls(self.controller.active)
        if self.close_requested and not self.controller.active and not self._installation_active():
            self.destroyed = True
            self.root.destroy()

    def _poll_diagnostics(self) -> None:
        while True:
            try:
                event = self.diagnostics.events.get_nowait()
            except queue.Empty:
                return
            try:
                parameters = self._diagnostic_parameters()
            except Exception:
                continue
            if (
                self.close_requested
                or self.controller.active
                or self._installation_active()
                or event.generation != self.diagnostics.generation
                or event.parameters != parameters
            ):
                continue
            if event.report is None:
                self._start_intent = None
                self.dependencies.set("只读检测未完成；未显示内部异常，没有安装或下载组件。")
                self.status.set("检测失败，未启动服务；请检查目录授权、运行环境或修改参数后重试。")
                continue
            try:
                description = capability_description(event.report)
            except Exception:
                self._start_intent = None
                self.status.set("检测结果不可用，未启动服务；未显示内部异常。")
                continue
            self._diagnostic_cache = event
            self.dependencies.set(description)
            if self._start_intent == event.generation:
                self._start_intent = None
                self._start()
            else:
                self.status.set("只读检测已完成；尚未启动，请明确点击启动。")

    def show(self) -> None:
        self.root.mainloop()

    def recover_stopping(self) -> None:
        """Keep an abnormal GUI exit visible until the real service finishes."""
        self.close_requested = True
        self.status.set("停止尚未完成；请等待服务或安装线程退出。当前进程没有完成退出。")
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
            diagnostics = getattr(window, "diagnostics", None)
            if diagnostics is not None:
                diagnostics.cancel()
                if diagnostics.active:
                    print(
                        "desktop_diagnostics_abandoned: 已放弃只读检测结果；系统调用尚未确认返回，未等待其退出。",
                        file=sys.stderr,
                    )
            window.controller.request_stop()
            window.controller.join(timeout=5)
            installation = getattr(window, "installation", None)
            if installation is not None:
                installation.request_stop()
                installation.join(timeout=5)
            if window.controller.active or (installation is not None and installation.active):
                print(
                    "desktop_stop_pending: 后台服务或安装线程尚未完成停止，进程并未退出。",
                    file=sys.stderr,
                )
                try:
                    window.recover_stopping()
                except BaseException:
                    # Destroyed/broken display cannot be repaired by pretending
                    # the live non-daemon service thread is already terminated.
                    print("desktop_stop_pending: 窗口未能恢复，服务或安装线程仍须完成停止。", file=sys.stderr)
                if window.controller.active or (installation is not None and installation.active):
                    result = 3
    return result


if __name__ == "__main__":
    raise SystemExit(main())
