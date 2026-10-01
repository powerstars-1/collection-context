"""Owned, isolated browser lifecycle; never imports cookies from another application."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright


class _LaunchOptions(TypedDict, total=False):
    executable_path: str


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
        self.context: BrowserContext | None = None

    def __enter__(self):
        dependencies = None
        expected_tool = None
        role = "chromium_headless_shell" if self.headless else "chromium"
        if self.runtime_dir is not None:
            if self.library_dir is None:
                raise ContextError("invalid_argument", "显式运行依赖必须绑定资料库边界。")
            runtime, profile = self.runtime_dir.resolve(), self.profile_dir.resolve()
            if runtime.is_relative_to(profile) or profile.is_relative_to(runtime):
                raise ContextError("runtime_directory_overlap", "运行依赖与独立账号目录必须分开。")
            dependencies = RuntimeDependencies(self.runtime_dir, library_dir=self.library_dir)
            expected_tool = dependencies.resolve(role)  # Fail before profile writes or a browser process.
        if self.profile_dir.is_symlink():
            raise ContextError("unsafe_login_profile", "登录目录不能为链接。")
        existed = self.profile_dir.exists()
        empty = not existed or (self.profile_dir.is_dir() and not any(self.profile_dir.iterdir()))
        marker = "collection-browser-profile.json"
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
                files.write(marker, b'{"schema_version":1,"owner":"collection-context"}\n')
            else:
                try:
                    config = json.loads(files.read(marker, max_bytes=1024, private=True))
                    if config != {"schema_version": 1, "owner": "collection-context"}:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ContextError(
                        "foreign_login_profile", "登录目录身份不符，不导入其他项目账号。"
                    ) from None
        try:
            from playwright.sync_api import sync_playwright

            if dependencies is not None and dependencies.resolve(role) != expected_tool:
                raise ContextError("runtime_dependency_integrity", "启动期间浏览器依赖版本改变，未切换执行。")
            self.runtime = sync_playwright().start()
            options: _LaunchOptions = {}
            if dependencies is not None:
                # Recheck just before launch; a configured failure never falls back to SDK caches.
                actual_tool = dependencies.resolve(role)
                if actual_tool != expected_tool:
                    raise ContextError(
                        "runtime_dependency_integrity", "启动期间浏览器依赖版本改变，未切换执行。"
                    )
                options["executable_path"] = str(actual_tool.path)
            self.context = self.runtime.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1200, "height": 850},
                accept_downloads=False,
                **options,
            )
            return self
        except ImportError:
            raise ContextError("browser_unavailable", "未安装浏览器接入依赖。") from None
        except ContextError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ContextError(
                "browser_start_failed", "独立浏览器未启动，请检查浏览器文件和系统依赖。"
            ) from None

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.runtime:
                self.runtime.stop()
            self.context = self.runtime = None

    def __exit__(self, *_):
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
