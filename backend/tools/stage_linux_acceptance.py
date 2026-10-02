"""Whitelist development evidence into a fresh Docker context; never copy the workspace wholesale."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

TOOLS = (
    "query_smoke.py",
    "writer_smoke.py",
    "mcp_smoke.py",
    "web_smoke.py",
    "extraction_smoke.py",
    "media_smoke.py",
    "stage_linux_acceptance.py",
    "linux_acceptance/runner.py",
    "linux_acceptance/Dockerfile",
    "linux_acceptance/README.md",
)

# Test-only dependencies are explicit. Never copy tools/ wholesale: it also
# contains opt-in cloud/provider and development operations outside this scope.
TEST_TOOLS = {
    "test_context_ffmpeg_source_build.py": ("native_distribution/ffmpeg_source_build.py",),
    "test_context_native_build.py": (
        "native_distribution/build.py",
        "native_distribution/desktop_entry.py",
        "native_distribution/licenses.py",
        "native_distribution/requirements-build.in",
    ),
    "test_context_media_package.py": (
        "native_distribution/media_package.py",
        "native_distribution/ffmpeg_source_build.py",
    ),
    "test_context_native_licenses.py": ("native_distribution/licenses.py",),
    "test_context_native_runtime_smoke.py": ("native_distribution/runtime_smoke.py",),
    "test_context_ocr_pipeline_assessment.py": (
        "native_distribution/ocr_pipeline_smoke.py",
        "native_distribution/media_runtime_smoke.py",
        "native_distribution/runtime_smoke.py",
    ),
    "test_context_startup_timing.py": ("startup_timing_smoke.py", "remote_https_smoke.py"),
    "test_context_remote_https_smoke.py": ("remote_https_smoke.py",),
    "test_windows_native_acceptance_tool.py": ("windows_native_acceptance.py",),
}

# Named cross-platform protocol tests outside the test_context* family.
# Their Windows native entry point still refuses Linux before product imports.
EXTRA_TESTS = ("test_linux_acceptance.py", "test_windows_native_acceptance_tool.py")

ASSET_NOTICE = "THIRD_PARTY_NOTICES.txt"
FRONTEND_FILES = (
    "index.html",
    "THIRD_PARTY_NOTICES.md",
    "src/Access.jsx",
    "src/App.jsx",
    "src/FrameGallery.jsx",
    "src/Library.jsx",
    "src/LibraryTools.jsx",
    "src/api.js",
    "src/main.jsx",
    "src/components/Sidebar.tsx",
    "src/components/SubNav.tsx",
    "src/components/TopNav.tsx",
    "src/components/workbench/MaterialList.tsx",
    "src/components/workbench/WorkbenchLayout.tsx",
    "src/management/LegacyPanels.jsx",
    "src/management/management.js",
)


def ordinary_file(root: Path, path: Path) -> Path:
    """Reject aliases in every selected path without reading unrelated files."""
    if not path.is_relative_to(root):
        raise ValueError("Acceptance input is outside the approved tree")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Acceptance inputs must not contain aliases")
        if part == root:
            break
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Acceptance inputs must be ordinary single-link files")
    return path


def support_members(backend: Path) -> dict[str, Path]:
    """Keep repository-relative paths required by tests, not the whole checkout."""
    repository = backend.parent
    tests = sorted((backend / "tests").glob("test_context*.py"))
    if not tests:
        raise ValueError("No original context tests found")
    tests.extend(backend / "tests" / name for name in EXTRA_TESTS)
    members = {"backend/tests/" + path.name: path for path in tests}
    required = set(TOOLS)
    for path in tests:
        required.update(TEST_TOOLS.get(path.name, ()))
    members.update({"backend/tools/" + name: backend / "tools" / name for name in required})
    if any(p.name in {"test_context_access.py", "test_context_frontend.py"} for p in tests):
        frontend = repository / "frontend"
        authored = [frontend / name for name in FRONTEND_FILES]
        if any(not path.is_file() for path in authored):
            raise ValueError("Authored frontend sources are required by the selected tests")
        members.update({p.relative_to(repository).as_posix(): p for p in authored})
    for path in members.values():
        ordinary_file(repository, path)
    return members


def source_members(backend: Path) -> dict[str, Path]:
    namespace = backend / "src/collection_context"
    members = {str(file.relative_to(backend / "src")): file for file in namespace.rglob("*.py")}
    for file in (namespace / "interfaces/assets").iterdir():
        if file.suffix in {".html", ".css", ".js"} or file.name == ASSET_NOTICE:
            members[str(file.relative_to(backend / "src"))] = file
    return members


def validate_candidate(wheel: Path, backend: Path) -> dict[str, Path]:
    members = source_members(backend)
    metadata = "collection_context-0.2.0.dev0.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            name not in members and not (name.startswith(metadata) and "/" not in name[len(metadata) :])
            for name in names
        ):
            raise ValueError("Candidate contains unexpected package members")
        if {name for name in names if name.startswith("collection_context/")} != set(members):
            raise ValueError("Candidate is missing source files or assets; rebuild before staging")
        for name, source in members.items():
            try:
                ordinary_file(backend, source)
            except (ValueError, OSError):
                raise ValueError(
                    "Candidate does not match the source under test; rebuild before staging"
                ) from None
            if archive.read(name) != source.read_bytes():
                raise ValueError("Candidate does not match the source under test; rebuild before staging")
    return members


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args(argv)
    wheel = args.wheel.absolute()
    backend = Path(__file__).resolve().parents[1]
    if wheel.is_symlink() or wheel.name != "collection_context-0.2.0.dev0-py3-none-any.whl":
        raise ValueError("Choose an explicit original candidate wheel, not an alias or legacy package")
    members = validate_candidate(wheel, backend)
    support = support_members(backend)  # Fail before creating a partial context.
    ordinary_file(wheel.parent, wheel)
    args.output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="linux-context-", dir=args.output.absolute()))
    shutil.copy2(wheel, stage / wheel.name)
    shutil.copy2(backend / "tools/linux_acceptance/Dockerfile", stage / "Dockerfile")
    for name, source in members.items():
        target = stage / "backend/src" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name, source in support.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    report = {
        "context": str(stage),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "test_files": sorted(file.name for file in (stage / "backend/tests").glob("*.py")),
        "support_files": sorted(support),
        "native_linux_execution_verified": False,
        "includes_legacy_or_private_runtime": False,
        "note": "Local development validation, no approval for deployment or public release",
    }
    (stage / "staging-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
