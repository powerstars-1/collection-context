"""Beginner-facing launcher for the loopback management page."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from collection_context.application.contracts import ContextError
from collection_context.diagnostics import default_workspace, startup_report, workspace_report
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore

LOOPBACK = "127.0.0.1"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="收藏上下文：小白启动器（开发候选版）")
    result.add_argument(
        "--workspace",
        type=Path,
        default=default_workspace(),
        help="产品资料库目录；默认使用当前系统的用户数据目录",
    )
    result.add_argument("--port", type=int, default=8787, help="本机管理页端口，默认 8787")
    result.add_argument(
        "--initialize-empty",
        action="store_true",
        help="明确同意只初始化不存在或空的目录；绝不覆盖非空目录",
    )
    result.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，适合无桌面 Linux")
    result.add_argument("--diagnose", action="store_true", help="只输出脱敏检查报告，不创建或修改资料库")
    return result


def _desktop_available() -> bool:
    if sys.platform in {"darwin", "win32"}:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _prepare_workspace(path: Path, *, initialize_empty: bool) -> tuple[LibraryStore, bool]:
    report = workspace_report(path)
    state = report["state"]
    if state in {"missing", "empty"}:
        if not initialize_empty:
            raise ContextError(
                "workspace_initialization_required",
                "目标目录尚未初始化。确认目录正确后，增加 --initialize-empty 再启动；不会覆盖非空目录。",
            )
        store = LibraryStore.initialize(path)
        FileIndex(store).rebuild()
        return store, True
    if state != "initialized_candidate":
        raise ContextError(
            "workspace_not_usable",
            "目标不是可识别的产品资料库；非空目录、符号链接和普通文件都不会被初始化或迁移。",
        )
    return LibraryStore(path), False


def _ensure_owner_access(
    store: LibraryStore,
    output: TextIO,
    *,
    owner_presenter: Callable[[str], bool] | None = None,
) -> None:
    registry = AccessRegistry(store)
    records = registry.records()
    if any("ui:manage" in record["permissions"] for record in records):
        return
    labels = {record["label"] for record in records}
    label = "本机管理启动器"
    suffix = 2
    while label in labels:
        label = f"本机管理启动器 {suffix}"
        suffix += 1
    access = registry.create(label, ui=True, manage=True)
    if owner_presenter is not None:
        accepted = False
        failed = False
        try:
            accepted = owner_presenter(access["token"]) is True
        except BaseException:
            # The exception can contain widget contents. Roll back without quoting it.
            failed = True
        finally:
            if not accepted:
                try:
                    registry.revoke(access["principal"])
                except BaseException:
                    raise ContextError(
                        "owner_rollback_failed",
                        "首次口令未确认，撤销未能完成；服务未启动，请检查资料库访问规则。",
                    ) from None
        if not accepted:
            raise ContextError(
                "owner_presentation_failed" if failed else "owner_confirmation_cancelled",
                "首次口令未能安全展示；本次新权限已撤销。"
                if failed
                else "首次口令保存已取消；本次新权限已撤销。",
            ) from None
        print("本机管理口令已通过本机窗口确认保存。", file=output)
        return
    print("已创建本机管理口令。它只显示这一次，请现在保存：", file=output)
    print(access["token"], file=output)
    print("这个口令不是模型 API Key，不要粘贴到聊天、日志或公共仓库。", file=output)


def _wait_for_ready(
    url: str,
    stop: threading.Event,
    *,
    output: TextIO,
    open_page: Callable[[str], Any] | None,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    health = url + "/health"
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=0.5) as response:
                if response.status == 200 and not stop.is_set():
                    print(f"管理页已就绪：{url}", file=output)
                    if open_page is not None:
                        open_page(url)
                    return
        except (OSError, urllib.error.URLError):
            pass
        stop.wait(0.1)
    if not stop.is_set():
        print("管理页未在 30 秒内确认就绪；服务若已退出，请查看上方错误。", file=output)


def _print_capability_summary(report: dict[str, Any], output: TextIO) -> None:
    optional = {
        item["role"]: item["available"]
        for item in report["capabilities"]["dependencies"]
        if not item["required_for_start"]
    }
    print(
        "启动检查：管理页依赖已就绪；"
        "来源浏览器=待独立运行验证，"
        f"FFmpeg={'已发现工具，待解码验证' if optional.get('media_ffmpeg') else '未发现工具'}，"
        f"本地OCR={'已发现模块，待权重和推理验证' if optional.get('local_ocr') else '未发现模块'}。",
        file=output,
    )
    print("这些可选能力不会自动安装，也不会自动获得来源同步或模型调用授权。", file=output)


@contextmanager
def _ordered_exit_signals(server):
    """Let Uvicorn drain requests without re-raising the terminating signal afterward."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = [signal.SIGINT, signal.SIGTERM]
    previous = {item: signal.getsignal(item) for item in watched}

    def request_exit(*_):
        server.should_exit = True

    try:
        for item in watched:
            signal.signal(item, request_exit)
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def _watch_stop_request(server: Any, requested: threading.Event, finished: threading.Event) -> None:
    """Bridge the desktop stop control to Uvicorn without killing another process."""
    while not finished.is_set():
        if requested.wait(0.1):
            finished.set()
            server.should_exit = True
            return


def launch(
    workspace: Path,
    *,
    port: int,
    initialize_empty: bool,
    no_browser: bool,
    output: TextIO,
    open_page: Callable[[str], Any] = webbrowser.open,
    owner_presenter: Callable[[str], bool] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    if stop_event is not None and stop_event.is_set():
        return 0
    report = startup_report(workspace, port)
    if not report["capabilities"]["ready_for_management_page"]:
        raise ContextError("dependency_required", "管理页依赖未就绪；请安装本产品的 launcher 可选依赖。")
    if not report["listen"]["available"]:
        raise ContextError("port_unavailable", "本机端口不可用，请停止占用进程或通过 --port 选择其他端口。")

    store, created = _prepare_workspace(workspace, initialize_empty=initialize_empty)
    try:
        if created:
            print(f"已初始化新的产品资料库：{store.files.root}", file=output)
        _ensure_owner_access(store, output, owner_presenter=owner_presenter)
        if stop_event is not None and stop_event.is_set():
            print("启动已取消，资料库保持原位。", file=output)
            return 0
        registry = AccessRegistry(store)
        origin = f"http://{LOOPBACK}:{port}"
        policy = AccessPolicy(origin, registry.credentials())
        print("正在加载本机管理页；首次启动可能需要几秒。", file=output)
        app = create_app(store.files.root, policy, refresh=lambda: registry.refresh(policy))
        try:
            import uvicorn
        except ImportError:
            raise ContextError("dependency_required", "管理页服务依赖不可用。") from None

        _print_capability_summary(report, output)
        should_open = not no_browser and _desktop_available()
        if not should_open:
            print(f"浏览器未自动打开，请访问：{origin}", file=output)
        print("按 Ctrl+C 可有序停止；资料库不会随服务退出而删除。", file=output)
        config = uvicorn.Config(
            app,
            host=LOOPBACK,
            port=port,
            proxy_headers=False,
            access_log=False,
            log_level="warning",
            limit_concurrency=20,
            timeout_keep_alive=5,
        )
        server = uvicorn.Server(config)
        stopped = threading.Event()
        ready = threading.Thread(
            target=_wait_for_ready,
            args=(origin, stopped),
            kwargs={"output": output, "open_page": open_page if should_open else None},
            name="collection-context-ready",
            daemon=True,
        )
        ready.start()
        watcher = None
        if stop_event is not None:
            watcher = threading.Thread(
                target=_watch_stop_request,
                args=(server, stop_event, stopped),
                name="collection-context-desktop-stop",
                daemon=True,
            )
            watcher.start()
        try:
            with _ordered_exit_signals(server):
                server.run()
        finally:
            stopped.set()
            ready.join(timeout=2)
            if watcher is not None:
                watcher.join(timeout=2)
        if not server.started and not (stop_event is not None and stop_event.is_set()):
            raise ContextError("startup_failed", "管理页服务未成功启动。")
        print("管理页已停止，资料库保持原位。", file=output)
        return 0
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.diagnose:
        report = startup_report(args.workspace, args.port)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        ready = report["capabilities"]["ready_for_management_page"] and report["listen"]["available"]
        return 0 if ready else 1
    try:
        return launch(
            args.workspace,
            port=args.port,
            initialize_empty=args.initialize_empty,
            no_browser=args.no_browser,
            output=sys.stdout,
        )
    except ContextError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
