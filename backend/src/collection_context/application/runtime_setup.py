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
from collection_context.infrastructure.runtime_media_layout import (
    MEDIA_BYTES,
    MEDIA_ID,
    MEDIA_PAYLOAD_BYTES,
    MEDIA_SHA256,
    MEDIA_SOURCE,
    MEDIA_TOOLS,
    bundled_media_state,
)
from collection_context.infrastructure.runtime_ocr import OCR_BUILD, OCR_MODELS, OCR_SOURCE, ocr_engine_state
from collection_context.infrastructure.runtime_ocr_layout import (
    OCR_BYTES,
    OCR_ID,
    OCR_PAYLOAD_BYTES,
    OCR_SHA256,
    bundled_ocr_state,
)

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
        MEDIA_ID: ArtifactPlan(
            id=MEDIA_ID,
            host_system="Darwin",
            host_arch="arm64",
            version="9.0.2",
            source_url=MEDIA_SOURCE,
            sha256=MEDIA_SHA256,
            bytes=MEDIA_BYTES,
            archive_type="zip",
            tools=tuple(
                ToolSpec(
                    role=role,
                    relative_path="bin/" + role,
                    bytes=size,
                    sha256=digest,
                    version="9.0.2",
                    license_id="LGPL-2.1-or-later",
                    build_version="source-macos-arm64-1",
                )
                for role, (size, digest) in MEDIA_TOOLS.items()
            ),
        ),
        OCR_ID: ArtifactPlan(
            id=OCR_ID,
            host_system="Darwin",
            host_arch="arm64",
            version="3.9.2",
            source_url=OCR_SOURCE,
            sha256=OCR_SHA256,
            bytes=OCR_BYTES,
            archive_type="zip",
            tools=tuple(
                ToolSpec(
                    role=role,
                    relative_path="models/" + filename,
                    bytes=size,
                    sha256=digest,
                    version="3.9.2",
                    license_id="LicenseRef-OCR-Development",
                    build_version=OCR_BUILD,
                )
                for role, (filename, size, digest) in OCR_MODELS.items()
            ),
        ),
    }
)


def _host_availability() -> str:
    arch = platform.machine().lower()
    if platform.system() != "Darwin" or arch not in {"arm64", "aarch64"}:
        return "host_not_available"
    try:
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            return "os_version_not_available"
    except (ValueError, IndexError):
        return "os_version_not_verified"
    return "available"


def _availability(*, browser: bool = True) -> str:
    state = _host_availability()
    if state != "available":
        return state
    if not browser:
        return bundled_media_state()
    try:
        if metadata.version("playwright") != "1.63.0":
            return "sdk_version_mismatch"
    except metadata.PackageNotFoundError:
        return "sdk_missing"
    return "available"


def _ocr_availability() -> str:
    host = _host_availability()
    if host != "available":
        return host
    engine = ocr_engine_state()
    if engine != "available_not_functionally_verified":
        return engine
    return bundled_ocr_state()


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
            {
                "id": MEDIA_ID,
                "name": "视频／音频处理工具（随包源码构建开发候选）",
                "state": _availability(browser=False),
                "host_system": "Darwin",
                "host_arch": "arm64",
                "minimum_macos": "14",
                "download_bytes": 0,
                "archive_bytes": MEDIA_BYTES,
                "payload_bytes": MEDIA_PAYLOAD_BYTES,
                "delivery": "bundled",
                "source_url": MEDIA_SOURCE,
                "source_url_kind": "upstream_source_not_binary_download",
                "license_notice": "随包保留完整FFmpeg源码、LGPL许可及构建／签名材料；不联网下载，发行许可与签名仍待完成",
                "functional_verified": False,
            },
            {
                "id": OCR_ID,
                "name": "本地CPU OCR（固定运行库及随包权重开发候选）",
                "state": _ocr_availability(),
                "host_system": "Darwin",
                "host_arch": "arm64",
                "minimum_macos": "14",
                "download_bytes": 0,
                "archive_bytes": OCR_BYTES,
                "payload_bytes": OCR_PAYLOAD_BYTES,
                "delivery": "bundled",
                "source_url": OCR_SOURCE,
                "source_url_kind": "upstream_package_not_binary_download",
                "license_notice": "保留RapidOCR与PaddleOCR原许可及权重来源；转换权重的公开再分发审查未完成",
                "functional_verified": False,
            },
        ],
        "not_available": ([] if _availability(browser=False) == "available" else ["ffmpeg_pair"])
        + ([] if _ocr_availability() == "available" else ["ocr_weights"])
        + ["Windows", "Linux"],
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
    state = _ocr_availability() if artifact_id == OCR_ID else _availability(browser=artifact_id != MEDIA_ID)
    if state != "available":
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
