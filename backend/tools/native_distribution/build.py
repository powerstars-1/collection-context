"""Build a local current-OS onedir candidate in a new library-external stage.

Install the explicit build dependencies in an isolated environment first.  This
tool never installs dependencies, browses personal data, or uses a developer signing
identity or publishes a build. PyInstaller may add a local ad-hoc Mach-O signature.
"""

from __future__ import annotations

import argparse
import hashlib
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


def build(output: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    external = checked_output(output, root.parent.resolve())
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
    entry = stage / "entry.py"
    shutil.copy2(Path(__file__).with_name("entry.py"), entry)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",
        "--console",
        "--noupx",
        "--name",
        "CollectionContext",
        "--distpath",
        str(stage / "dist"),
        "--workpath",
        str(stage / "build"),
        "--specpath",
        str(stage),
        "--paths",
        str(stage / "src"),
        "--add-data",
        str(source / "interfaces" / "assets") + ":collection_context/interfaces/assets",
        "--collect-submodules",
        "uvicorn",
        "--collect-submodules",
        "mcp.server",
        "--collect-submodules",
        "mcp.shared",
        "--recursive-copy-metadata",
        "fastapi",
        "--recursive-copy-metadata",
        "mcp",
        "--recursive-copy-metadata",
        "uvicorn",
        str(entry),
    ]
    subprocess.run(command, cwd=stage, check=True)
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
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": VERSIONS,
        "requires_user_python_or_node": False,
        "bundled_roles": ["cli", "stdio_mcp", "authenticated_http", "terminal_launcher"],
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
    args = parser.parse_args()
    report = build(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    Path(report["stage"], "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
