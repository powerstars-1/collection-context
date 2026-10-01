"""Build a local current-OS onedir candidate in a new library-external stage.

Install the explicit build dependencies in an isolated environment first.  This
tool never installs dependencies, browses personal data, or uses a developer signing
identity or publishes a build. PyInstaller may add a local ad-hoc Mach-O signature.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

VERSIONS = {
    "pyinstaller": "6.22.3",
    "mcp": "2.2.0",
    "fastapi": "0.142.2",
    "uvicorn": "0.54.0",
    "playwright": "1.63.0",
}


def checked_browser_sdk() -> dict:
    """Inspect only the fixed installed wheel; never install a browser or query caches.

    The wheel's registered official sync_api hook collects its driver Node/JS:
    https://github.com/microsoft/playwright-python/tree/main/playwright/_impl/__pyinstaller
    Local browser installs inside that collected directory must not enter the
    candidate, even if empty or linked. Reject contamination without deleting it.
    """
    distribution = metadata.distribution("playwright")
    records = {str(path).replace("\\", "/") for path in distribution.files or ()}
    driver = Path(distribution.locate_file("playwright/driver"))
    if driver.is_symlink() or not driver.is_dir() or driver.parent.is_symlink():
        raise ValueError("Playwright SDK driver directory must be real")
    for relative in ("playwright/_impl", "playwright/_impl/__pyinstaller"):
        directory = Path(distribution.locate_file(relative))
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Playwright SDK hook directory must be real")
    if any(path.name == ".local-browsers" for path in driver.rglob("*")):
        raise ValueError("Playwright SDK contains .local-browsers; use a clean build environment")
    for path in driver.rglob("*"):
        if path.is_symlink():
            raise ValueError("Playwright SDK driver cannot contain links")
        if not path.is_dir() and (
            not path.is_file() or "playwright/driver/" + path.relative_to(driver).as_posix() not in records
        ):
            raise ValueError("Playwright SDK driver contains unregistered resources")
    node_name = "node.exe" if sys.platform == "win32" else "node"
    required = (
        "playwright/driver/" + node_name,
        "playwright/driver/package/cli.js",
        "playwright/driver/package/browsers.json",
        "playwright/driver/LICENSE",
        "playwright/driver/package/LICENSE",
        "playwright/driver/package/NOTICE",
        "playwright/driver/package/ThirdPartyNotices.txt",
        "playwright/_impl/__pyinstaller/hook-playwright.sync_api.py",
        "playwright/_impl/__pyinstaller/__init__.py",
    )
    for relative in required:
        path = Path(distribution.locate_file(relative))
        if relative not in records or path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Playwright SDK driver/hook/license resources are incomplete")
    node = driver / node_name
    if sys.platform != "win32" and not node.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise ValueError("Playwright SDK Node executable is not executable")
    with node.open("rb") as stream:
        node_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "package_version": VERSIONS["playwright"],
        "driver_node_input_sha256": node_hash,
        "collection": "official_playwright_sync_api_hook",
        "browser_binaries_included": False,
        "frozen_driver_execution_verified": False,
        "license_closure": "unresolved",
    }


def validate_source(source: Path) -> None:
    """Reject links before copying; arbitrary text is not a product resource."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Product source must be a real directory")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Product source cannot contain links")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            relative = path.relative_to(source).as_posix()
            if path.suffix not in {".py", ".html", ".css", ".js"} and relative != (
                "interfaces/assets/THIRD_PARTY_NOTICES.txt"
            ):
                raise ValueError("Product source file is outside the native whitelist")


def checked_output(output: Path, repository: Path) -> Path:
    resolved = output.absolute().resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError("Output must be a specific external build directory")
    if resolved == repository or repository in resolved.parents:
        raise ValueError("Build output must be outside the repository")
    return resolved


def freeze_command(stage: Path, source: Path, entry: Path, *, desktop: bool = False) -> list[str]:
    """Fixed modes only: the console companion is retained for stdio AI clients."""
    if desktop and sys.platform != "darwin":
        raise ValueError("Desktop application packaging is only verified on the current Mac candidate")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--windowed" if desktop else "--console",
        "--noupx",
        "--name",
        "CollectionContextDesktop" if desktop else "CollectionContext",
        "--distpath",
        str(stage / "dist"),
        "--workpath",
        str(stage / ("build-desktop" if desktop else "build-console")),
        "--specpath",
        str(stage),
        "--paths",
        str(stage / "src"),
        "--add-data",
        str(source / "interfaces" / "assets") + ":collection_context/interfaces/assets",
    ]
    if desktop:
        command.extend(
            ["--osx-bundle-identifier", "local.collectioncontext.desktop", "--disable-windowed-traceback"]
        )
    if (stage / "licenses").is_dir():
        command.extend(["--add-data", str(stage / "licenses") + ":native_licenses"])
    for package in ("uvicorn", "mcp.server", "mcp.shared"):
        command.extend(["--collect-submodules", package])
    for package in ("fastapi", "mcp", "uvicorn"):
        command.extend(["--recursive-copy-metadata", package])
    # Explicit import triggers Playwright's installed official hook, which
    # collects its internal Node and JS assets. No host Node/cache is copied.
    command.extend(["--hidden-import", "playwright.sync_api", "--copy-metadata", "playwright"])
    command.append(str(entry))
    return command


def license_inventory(stage: Path) -> dict:
    """Read only the explicit build environment; keep native closure unresolved."""
    helper = Path(__file__).with_name("licenses.py")
    if helper.is_symlink() or not helper.is_file():
        raise ValueError("Expected owned license inventory helper")
    spec = importlib.util.spec_from_file_location("collection_context_build_license_inventory", helper)
    if spec is None or spec.loader is None:
        raise ValueError("License inventory helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_licenses(Path(sys.prefix).absolute(), stage / "licenses")


def build(output: Path, *, desktop: bool = False) -> dict:
    root = Path(__file__).resolve().parents[2]
    external = checked_output(output, root.parent.resolve())
    if desktop and sys.platform != "darwin":
        raise ValueError("Desktop application packaging is not verified for this build host")
    for package, expected in VERSIONS.items():
        if metadata.version(package) != expected:
            raise ValueError(f"Build dependency version mismatch: {package}")
    browser_sdk = checked_browser_sdk()  # Reject before creating/copying any candidate.
    external.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="native-candidate-", dir=external))
    source = stage / "src" / "collection_context"
    original = root / "src" / "collection_context"
    validate_source(original)
    shutil.copytree(
        original,
        source,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    validate_source(source)
    licenses = license_inventory(stage)
    entry = stage / "entry.py"
    shutil.copy2(Path(__file__).with_name("entry.py"), entry)
    command = freeze_command(stage, source, entry)
    subprocess.run(command, cwd=stage, check=True)
    desktop_bundle = None
    if desktop:
        desktop_entry = stage / "desktop_entry.py"
        shutil.copy2(Path(__file__).with_name("desktop_entry.py"), desktop_entry)
        subprocess.run(freeze_command(stage, source, desktop_entry, desktop=True), cwd=stage, check=True)
        desktop_bundle = stage / "dist" / "CollectionContextDesktop.app"
        if not desktop_bundle.is_dir():
            raise ValueError("Desktop bundle was not created")
    binary = (
        stage
        / "dist"
        / "CollectionContext"
        / ("CollectionContext.exe" if sys.platform == "win32" else "CollectionContext")
    )
    return {
        "stage": str(stage),
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "desktop_bundle": str(desktop_bundle) if desktop_bundle is not None else None,
        "desktop_startup": "explicit_picker_no_workspace_mutation_until_start" if desktop else "not_built",
        "console_companion_retained": True,
        "license_inventory": licenses,
        "runtime_sbom_verified": False,
        "native_license_closure": "unresolved",
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": VERSIONS,
        "source_browser_sdk": browser_sdk,
        "requires_user_python_or_node": False,
        "bundled_roles": ["cli", "stdio_mcp", "authenticated_http", "terminal_launcher", "source_browser_sdk"]
        + (["desktop_picker"] if desktop else []),
        "not_bundled": ["chromium_browser_runtime", "ffmpeg", "ocr_runtime_and_weights"],
        "developer_signed": False,
        "notarized": False,
        "tool_generated_ad_hoc_signature": sys.platform == "darwin",
        "public_release_authorized": False,
        "verification": "build_only_until_native_process_smoke",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--desktop-app", action="store_true", help="本机Mac开发候选 .app；同时保留console程序"
    )
    args = parser.parse_args()
    report = build(args.output, desktop=args.desktop_app)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    Path(report["stage"], "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
