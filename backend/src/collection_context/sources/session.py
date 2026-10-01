"""Shared original source-session boundary for CLI and owned execution threads."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.browser import BrowserSession
from collection_context.library.store import LibraryStore
from collection_context.sources.browser_source import DouyinBrowserSource


@contextmanager
def source_session(
    store: LibraryStore, profile: Path, *, headless: bool = True, runtime_dir: Path | None = None
):
    profile = profile.absolute()
    root = store.files.root.resolve()
    resolved = profile.resolve()
    if resolved.is_relative_to(root) or root.is_relative_to(resolved):
        raise ContextError("unsafe_login_profile", "登录目录必须与可导出资料库分开。")
    options = {"runtime_dir": runtime_dir, "library_dir": store.files.root} if runtime_dir is not None else {}
    with BrowserSession(profile, headless=headless, **options) as browser:
        yield DouyinBrowserSource(browser)
