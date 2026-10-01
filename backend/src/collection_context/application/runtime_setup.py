"""Local installation selection; deliberately not an HTTP/MCP/AI capability.

Only this product's compiled catalog is selectable. No update URL, archive path,
hash, shell command, or mirror can be supplied through the public CLI. Fixed Mac
ARM64 packages cover headless and visible browsing. Platform login and OCR are
separate requirements, not proved by installation or a local fixture.
"""

from __future__ import annotations

import platform
import threading
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.browser import BrowserSession
from collection_context.infrastructure.runtime_browser_layout import (
    HEADED_EXECUTABLE,
    HEADED_EXECUTABLE_SHA256,
    HEADED_ID,
    HEADED_SHA256,
    HEADED_SOURCE,
)
from collection_context.infrastructure.runtime_download import RuntimeDownloads
from collection_context.infrastructure.runtime_installation import ArtifactPlan, RuntimeInstaller, ToolSpec

HEADLESS_ID = "chromium-headless-macos-arm64-1243"
CATALOG = MappingProxyType(
    {
        HEADLESS_ID: ArtifactPlan(
            id=HEADLESS_ID,
            host_system="Darwin",
            host_arch="arm64",
            version="153.0.8010.12",
            source_url=(
                "https://cdn.playwright.dev/builds/cft/153.0.8010.12/mac-arm64/"
                "chrome-headless-shell-mac-arm64.zip"
            ),
            sha256="89d80a6d26ccd0ccfd51e22d9e1297283862af2b0cd91dce07459b35ca0059f2",
            bytes=98_831_293,
            archive_type="zip",
            tools=(
                ToolSpec(
                    role="chromium_headless_shell",
                    relative_path="chrome-headless-shell-mac-arm64/chrome-headless-shell",
                    bytes=169_253_248,
                    sha256="a0bfe7b4da4787b66058477d696cd1d09065d25f06a548947722b9af77ee8282",
                    version="153.0.8010.12",
                    license_id="LicenseRef-Chromium-ThirdParty",
                    playwright_package_version="1.63.0",
                    playwright_revision="1243",
                ),
            ),
        ),
        HEADED_ID: ArtifactPlan(
            id=HEADED_ID,
            host_system="Darwin",
            host_arch="arm64",
            version="153.0.8010.12",
            source_url=HEADED_SOURCE,
            sha256=HEADED_SHA256,
            bytes=190_970_181,
            archive_type="zip",
            tools=(
                ToolSpec(
                    role="chromium",
                    relative_path=HEADED_EXECUTABLE,
                    bytes=52_112,
                    sha256=HEADED_EXECUTABLE_SHA256,
                    version="153.0.8010.12",
                    license_id="LicenseRef-Chrome-for-Testing",
                    playwright_package_version="1.63.0",
                    playwright_revision="1243",
                ),
            ),
        ),
    }
)


def _availability() -> str:
    arch = platform.machine().lower()
    if platform.system() != "Darwin" or arch not in {"arm64", "aarch64"}:
        return "host_not_available"
    try:
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            return "os_version_not_available"
    except (ValueError, IndexError):
        return "os_version_not_verified"
    try:
        if metadata.version("playwright") != "1.63.0":
            return "sdk_version_mismatch"
    except metadata.PackageNotFoundError:
        return "sdk_missing"
    return "available"


def runtime_options() -> dict[str, Any]:
    """Read-only software selection; does not inspect credentials or create dirs."""
    return {
        "artifacts": [
            {
                "id": HEADLESS_ID,
                "name": "无桌面来源浏览器（不含可见登录窗口）",
                "state": _availability(),
                "host_system": "Darwin",
                "host_arch": "arm64",
                "minimum_macos": "14",
                "playwright_version": "1.63.0",
                "browser_revision": "1243",
                "download_bytes": CATALOG[HEADLESS_ID].bytes,
                "payload_bytes": 204_662_008,
                "source_url": CATALOG[HEADLESS_ID].source_url,
                "license_notice": "Chromium 及第三方完整许可说明随原始包保留；发行审查仍待完成",
                "functional_verified": False,
            },
            {
                "id": HEADED_ID,
                "name": "可见来源浏览器（Chrome for Testing 开发候选）",
                "state": _availability(),
                "host_system": "Darwin",
                "host_arch": "arm64",
                "minimum_macos": "14",
                "playwright_version": "1.63.0",
                "browser_revision": "1243",
                "download_bytes": CATALOG[HEADED_ID].bytes,
                "payload_bytes": 375_624_614,
                "source_url": HEADED_SOURCE,
                "license_notice": "保留原 ABOUT／Widevine许可与内置 credits／terms；公开发行许可及签名仍待核验",
                "functional_verified": False,
            },
        ],
        "not_available": ["ffmpeg_pair", "ocr_weights", "Windows", "Linux"],
        "auto_install": False,
        "grants_sync_or_model_authority": False,
    }


def install_runtime(
    runtime_dir: Path,
    *,
    library_dir: Path,
    artifact_id: str,
    installation_confirmed: bool = False,
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    if artifact_id not in CATALOG:
        raise ContextError("runtime_install_catalog", "安装项不属于固定产品清单；未下载。")
    if installation_confirmed is not True:
        raise ContextError("runtime_install_confirmation", "请先查看清单并明确确认组件安装。")
    if _availability() != "available":
        raise ContextError("runtime_install_unavailable", "当前系统、版本或运行库不满足此安装项。")
    with RuntimeDownloads(runtime_dir, catalog=CATALOG, stop=stop) as source:
        installer = RuntimeInstaller(
            runtime_dir, library_dir=library_dir, catalog=CATALOG, _archive_source=source
        )
        return installer.install(artifact_id, installation_confirmed=True, stop=stop)


def probe_runtime(
    runtime_dir: Path, *, library_dir: Path, browser_dir: Path, headless: bool = True
) -> dict[str, Any]:
    """Explicit local rendering in a new, empty owned profile; no platform login.

    Successful exit proves this browser/driver started and rendered this fixture,
    not that Douyin login, sync, every media codec, or visual extraction works.
    Never reuse an account profile merely for a dependency test.
    """
    if browser_dir.exists() or browser_dir.is_symlink():
        raise ContextError(
            "runtime_probe_profile_exists", "依赖探测须使用不存在的新浏览器目录，不能借用登录态。"
        )
    with BrowserSession(
        browser_dir, headless=headless, runtime_dir=runtime_dir, library_dir=library_dir
    ) as session:
        context = session.context
        assert context is not None
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content(
            "<!doctype html><title>CollectionContext runtime probe</title><p id='probe'>original fixture</p>"
        )
        rendered = page.locator("#probe").text_content() == "original fixture"
        title = page.title()
        page.close()
    if not rendered or title != "CollectionContext runtime probe":
        raise ContextError("runtime_probe_failed", "浏览器启动后未完成本机样例渲染。")
    return {
        "state": "verified",
        "role": "chromium_headless_shell" if headless else "chromium",
        "functional_verified": True,
        "verification_scope": "owned_headless_local_fixture_only"
        if headless
        else "owned_headed_local_fixture_only",
        "browser_closed": True,
        "platform_login_verified": False,
        "sync_verified": False,
        "model_calls": 0,
    }
