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
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

VERSIONS = {"pyinstaller": "6.22.3", "mcp": "2.2.0", "fastapi": "0.142.2", "uvicorn": "0.54.0"}


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
        "requires_user_python_or_node": False,
        "bundled_roles": ["cli", "stdio_mcp", "authenticated_http", "terminal_launcher"]
        + (["desktop_picker"] if desktop else []),
        "not_bundled": ["source_browser_runtime", "ffmpeg", "ocr_runtime_and_weights"],
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
