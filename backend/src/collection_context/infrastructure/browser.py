"""Owned, isolated browser lifecycle; never imports cookies from another application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.browser_ownership import (
    PROFILE_MARKER,
    PROFILE_MARKER_BODY,
    BrowserLease,
    profile_marker,
)
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.platform_safety import require_ownership_runtime
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies, ToolDependency

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright


class _LaunchOptions(TypedDict, total=False):
    executable_path: str


def _owned_browser_tool(dependencies: RuntimeDependencies, *, headless: bool) -> ToolDependency:
    # Only an absent role in a validated receipt permits full Chromium in
    # headless mode. A declared but damaged shell must keep its original error.
    tools = dependencies._receipt()["tools"]
    role = "chromium_headless_shell" if headless and "chromium_headless_shell" in tools else "chromium"
    return dependencies.resolve(role)


class BrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = True,
        runtime_dir: Path | None = None,
        library_dir: Path | None = None,
    ):
        self.profile_dir = profile_dir.absolute()
        self.headless = headless
        self.runtime_dir = runtime_dir
        self.library_dir = library_dir
        self.runtime: Playwright | None = None
        self._context: BrowserContext | None = None
        self._lease: BrowserLease | None = None

    @property
    def context(self) -> BrowserContext | None:
        if self._context is not None:
            self.check()
        return self._context

    def check(self) -> None:
        if self._lease is None:
            raise ContextError("browser_not_owned", "当前没有独立浏览器所有权。")
        self._lease.check()

    def __enter__(self):
        if self._lease is not None or self.runtime is not None or self._context is not None:
            raise ContextError("browser_busy", "此浏览器生命周期仍在运行或退出中，未重复启动。")
        require_ownership_runtime()  # Windows stays fail-closed, before profile creation.
        dependencies = None
        expected_tool = None
        if self.runtime_dir is not None:
            if self.library_dir is None:
                raise ContextError("invalid_argument", "显式运行依赖必须绑定资料库边界。")
            runtime, profile = self.runtime_dir.resolve(), self.profile_dir.resolve()
            if runtime.is_relative_to(profile) or profile.is_relative_to(runtime):
                raise ContextError("runtime_directory_overlap", "运行依赖与独立账号目录必须分开。")
            dependencies = RuntimeDependencies(self.runtime_dir, library_dir=self.library_dir)
            # Fail before profile writes or a browser process.
            expected_tool = _owned_browser_tool(dependencies, headless=self.headless)
        if self.profile_dir.is_symlink():
            raise ContextError("unsafe_login_profile", "登录目录不能为链接。")
        existed = self.profile_dir.exists()
        empty = not existed or (self.profile_dir.is_dir() and not any(self.profile_dir.iterdir()))
        marker = PROFILE_MARKER
        if not empty and not (self.profile_dir / marker).is_file():
            raise ContextError(
                "foreign_login_profile", "不能借用其他项目或日常浏览器的账号目录，请指定新目录。"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and empty:
            self.profile_dir.chmod(0o700)
        if os.name != "nt":
            stat = self.profile_dir.stat()
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise ContextError("unsafe_login_profile", "独立登录目录应仅当前用户可读写。")
        with SafeFiles(self.profile_dir) as files:
            if empty:
                try:
                    files.write(marker, PROFILE_MARKER_BODY)
                except ContextError as error:
                    # A simultaneous first initialization may have published
                    # the same product marker. Validate it; never overwrite it.
                    if error.code != "write_conflict":
                        raise
            checked_marker = profile_marker(files)
            self._lease = BrowserLease(self.profile_dir, root_identity=files.identity, marker=checked_marker)
        try:
            from playwright.sync_api import sync_playwright

            self.check()
            if (
                dependencies is not None
                and _owned_browser_tool(dependencies, headless=self.headless) != expected_tool
            ):
                raise ContextError("runtime_dependency_integrity", "启动期间浏览器依赖版本改变，未切换执行。")
            self.runtime = sync_playwright().start()
            self.check()
            options: _LaunchOptions = {}
            if dependencies is not None:
                # Recheck just before launch; a configured failure never falls back to SDK caches.
                actual_tool = _owned_browser_tool(dependencies, headless=self.headless)
                if actual_tool != expected_tool:
                    raise ContextError(
                        "runtime_dependency_integrity", "启动期间浏览器依赖版本改变，未切换执行。"
                    )
                options["executable_path"] = str(actual_tool.path)
            self.check()
            self._context = self.runtime.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1200, "height": 850},
                accept_downloads=False,
                **options,
            )
            self.check()
            return self
        except ImportError:
            self.close()
            raise ContextError("browser_unavailable", "未安装浏览器接入依赖。") from None
        except ContextError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ContextError(
                "browser_start_failed", "独立浏览器未启动，请检查浏览器文件和系统依赖。"
            ) from None
        except BaseException:
            self.close()
            raise

    def clear_login(self) -> None:
        """Forget this product's Douyin login, without deleting its profile or library."""
        if self.context is None:
            raise ContextError("browser_not_running", "独立浏览器尚未启动。")
        self.check()
        self.context.clear_cookies()
        page = self.context.new_page()
        session = None
        try:
            session = self.context.new_cdp_session(page)
            self.check()
            session.send("Storage.clearDataForOrigin", {
                "origin": "https://www.douyin.com", "storageTypes": "all",
            })
        except Exception:
            raise ContextError("source_logout_failed", "退出登录未完成，请重试。") from None
        finally:
            if session is not None:
                session.detach()
            page.close()

    def close(self):
        failed = False
        if self._context is not None:
            try:
                self._context.close()
            except BaseException:
                failed = True
            else:
                self._context = None
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except BaseException:
                failed = True
            else:
                self.runtime = None
        # Do not tell another session it is safe to use the same profile when
        # shutdown did not confirm both SDK/context resources have exited.
        if self._context is None and self.runtime is None and self._lease is not None:
            self._lease.close()
            self._lease = None
        if failed:
            raise ContextError(
                "browser_stop_failed", "浏览器退出未确认，仍保留账号目录所有权；不会启动第二个实例。"
            ) from None

    def __exit__(self, *_):
        try:
            if self._lease is not None:
                self.check()
        finally:
            self.close()

    def probe_public_site(self) -> dict:
        """Read a public page only; lack of a login button does not prove authenticated access."""
        if self.context is None:
            raise ContextError("browser_not_running", "请先启动独立浏览器。")
        page = self.context.new_page()
        try:
            response = page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30_000)
            page.locator("body").wait_for(timeout=10_000)
            login_controls = page.get_by_text("登录", exact=True).count()
            return {
                "reachable": bool(response and response.ok),
                "http_status": response.status if response else None,
                "headless": self.headless,
                "login_state": "login_required" if login_controls else "unknown",
                "authenticated_sources_verified": False,
                "note": "公共页浏览器预检，未登录、未同步私人列表；不证明无桌面登录或五来源可用。",
            }
        except Exception:
            raise ContextError(
                "source_site_unavailable",
                "公共页面未完成加载；未读取私人列表或绕过平台验证。",
                retryable=True,
            ) from None
        finally:
            page.close()
