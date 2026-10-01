"""Frozen native dispatch and an in-memory, local-only owner-token presenter.

The native console candidate is not an unattended desktop application.  No-argument
execution shows help rather than silently creating a workspace or an access key.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any

from collection_context.application.contracts import ContextError

MODES = frozenset({"cli", "mcp", "web", "launch", "desktop"})


def present_owner_token(token: str, *, window_factory: Callable[[], Any] | None = None) -> bool:
    """Display one fresh token on the main GUI thread, never through a subprocess.

    True means the user explicitly acknowledged saving it.  Closing or cancelling
    returns False; the launcher must revoke its newly created credential in either
    cancellation or failure.  Python/Tk memory is cleared best-effort, not promised
    cryptographic zeroization.  No clipboard operation, file, URL, or logging occurs.
    """
    if not isinstance(token, str) or not token.startswith("scc_") or len(token) < 32:
        raise ContextError("invalid_argument", "只能展示新建的产品访问口令。")
    if threading.current_thread() is not threading.main_thread():
        raise ContextError("desktop_display_required", "首次口令展示须在本机界面主线程完成。")
    window = None
    try:
        if window_factory is None:
            window = _TokenWindow()
        else:
            window = window_factory()
        return window.show(token) is True
    except BaseException:
        # A GUI exception may contain widget values: never include or chain it.
        raise ContextError(
            "desktop_display_required", "首次口令未能安全展示；本次新权限必须撤销后再尝试。"
        ) from None
    finally:
        if window is not None:
            try:
                window.clear_and_close()
            except Exception:
                pass


class _TokenWindow:
    def __init__(self, parent: Any = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.parent = parent
        self.root: Any = tk.Tk() if parent is None else tk.Toplevel(parent)
        self.secret: Any = None
        self.closed = False
        self.result = False
        try:
            if parent is None:
                # Tk normally prints callback exceptions, which may contain
                # widget values. Cancel instead, without formatting the error.
                setattr(self.root, "report_callback_exception", lambda *_: self.clear_and_close())
            self._build(tk, ttk)
        except BaseException:
            # __init__ may fail before the presenter receives this object.
            self.clear_and_close()
            raise ContextError("desktop_display_required", "本机口令窗口未能建立。") from None

    def _build(self, tk: Any, ttk: Any) -> None:
        self.root.title("收藏上下文 · 保存本机管理口令")
        self.root.geometry("640x280")
        self.root.resizable(False, False)
        self.secret = tk.StringVar(master=self.root)
        saved = tk.BooleanVar(master=self.root, value=False)
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="本机管理口令只展示一次", font=("TkDefaultFont", 16)).pack(anchor="w")
        ttk.Label(
            frame,
            text="请自行保存，随后用它登录本机管理页。\n它不是模型 API Key，请不要发到聊天或公开仓库。",
            justify="left",
        ).pack(anchor="w", pady=12)
        # A label has no Tk copy/selection binding (a readonly Entry still has one).
        self.entry = ttk.Label(frame, textvariable=self.secret, font="TkFixedFont", wraplength=590)
        self.entry.pack(fill="x", pady=4)
        ttk.Checkbutton(frame, text="我已自行保存这个口令", variable=saved).pack(anchor="w", pady=12)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")

        def confirm() -> None:
            if saved.get():
                self.result = True
                self.clear_and_close()

        ttk.Button(actions, text="已保存，继续", command=confirm).pack(side="right")
        ttk.Button(actions, text="取消（撤销本次新权限）", command=self.clear_and_close).pack(
            side="right", padx=12
        )
        self.root.protocol("WM_DELETE_WINDOW", self.clear_and_close)

    def show(self, token: str) -> bool:
        if self.closed:
            return False
        self.secret.set(token)
        self.entry.focus_set()
        if self.parent is None:
            self.root.mainloop()
        else:
            self.root.transient(self.parent)
            # Nested Tk processing keeps the parent Stop/Close callbacks live.
            self.parent.wait_window(self.root)
        return self.result

    def clear_and_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.secret is not None:
            try:
                self.secret.set("")
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


def dispatch(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"--help", "-h"}:
        print("收藏上下文 · 原生基础候选（仅当前构建系统验收）")
        print("用法：CollectionContext <cli|mcp|web|launch|desktop> [对应入口参数]")
        print("cli：资料库命令；mcp：stdio AI工具；web：显式认证服务；launch：终端本机启动器。")
        print("desktop：本机选库与启动/停止窗口（需要可用的本地 GUI）。")
        print("浏览器、FFmpeg、OCR 权重不在这个基础包中；未配置模型不会提供识别服务。")
        return 0
    if args[0] not in MODES:
        print("invalid_mode: 请选择 cli、mcp、web、launch 或 desktop；不会启动其他程序。", file=sys.stderr)
        return 2
    mode, rest = args[0], args[1:]
    # Explicit imports are intentional: PyInstaller must see every supported entry.
    if mode == "cli":
        from collection_context.cli import main as cli_main

        return cli_main(rest)
    if mode == "mcp":
        from collection_context.interfaces.mcp import main as mcp_main

        return mcp_main(rest)
    if mode == "web":
        from collection_context.interfaces.server import main as web_main

        return web_main(rest)
    if mode == "desktop":
        from collection_context.native_desktop import main as desktop_main

        return desktop_main(rest)
    from collection_context.launcher import main as launch_main

    return launch_main(rest)


if __name__ == "__main__":
    raise SystemExit(dispatch())
