"""Run native safety checks on the actual host and filesystem.

Usage:
    python -m collection_context.infrastructure.platform_acceptance

There is deliberately no ``--platform`` override.  A report is evidence only
for the runtime identity embedded in that report, not for another OS/CPU.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ownership import WriterLease
from collection_context.infrastructure.platform_safety import (
    current_runtime_identity,
    detect_platform_safety,
)


def _expect_code(code: str, action: Callable[[], object]) -> None:
    try:
        result = action()
    except ContextError as error:
        if error.code == code:
            return
        raise AssertionError(f"expected {code}, got {error.code}") from error
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise AssertionError(f"expected {code}, operation succeeded")


def _file_boundary_check(base: Path) -> None:
    root = base / "中文 空格资料库"
    root.mkdir()
    outside = base / "outside.txt"
    outside.write_bytes(b"outside")
    with SafeFiles(root) as files:
        files.write("子目录/内容.txt", b"first")
        assert files.read("子目录/内容.txt") == b"first"
        _expect_code("write_conflict", lambda: files.write("子目录/内容.txt", b"wrong"))
        assert files.read("子目录/内容.txt") == b"first"
        files.write("子目录/内容.txt", b"second", replace=True)
        assert files.read("子目录/内容.txt") == b"second"

        os.symlink(outside, root / "linked")
        _expect_code("forbidden_path", lambda: files.read("linked"))
        os.link(outside, root / "hardlinked")
        _expect_code("forbidden_path", lambda: files.read("hardlinked"))

    alias = base / "root-alias"
    os.symlink(root, alias, target_is_directory=True)
    _expect_code("storage_unavailable", lambda: SafeFiles(alias))


def _mount_identity_check(base: Path) -> None:
    root = base / "mount"
    root.mkdir()
    files = SafeFiles(root)
    try:
        detached = base / "detached"
        root.rename(detached)
        root.mkdir()
        _expect_code("storage_unavailable", lambda: files.write("must-not-appear", b"x"))
        assert not list(root.iterdir())
    finally:
        files.close()


def _lease_check(base: Path) -> None:
    root = base / "lease"
    root.mkdir()
    child_code = (
        "from pathlib import Path; import sys; "
        "from collection_context.infrastructure.ownership import WriterLease; "
        "lease=WriterLease(Path(sys.argv[1])); print('owned', flush=True); "
        "sys.stdin.read(1); lease.close()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        if not select.select([child.stdout], [], [], 10)[0]:
            raise AssertionError("ownership child did not become ready")
        if child.stdout.readline().strip() != "owned":
            stderr = child.stderr.read(2_000) if child.stderr is not None else ""
            raise AssertionError(f"ownership child failed: {stderr}")
        _expect_code("writer_busy", lambda: WriterLease(root))
        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.flush()
        if child.wait(timeout=10) != 0:
            raise AssertionError("ownership child did not exit cleanly")
        with WriterLease(root) as lease:
            lease.check()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def _lease_replacement_check(base: Path) -> None:
    root = base / "lease-replacement"
    root.mkdir()
    with WriterLease(root) as lease:
        lease.files.write(WriterLease.path, b"{}", replace=True)
        _expect_code("lock_changed", lease.check)


def run_native_acceptance(parent: Path | None = None) -> tuple[dict[str, object], int]:
    capability = detect_platform_safety()
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "native_runtime_and_temporary_filesystem_only",
        "runtime": current_runtime_identity(),
        "capability": capability.json(),
        "release_support_claim": False,
        "full_product_regression": False,
        "filesystem_parent": "user_selected_existing_directory" if parent else "system_temporary_directory",
        "checks": [],
    }
    if not capability.runtime_supported:
        report["status"] = "unsupported"
        report["next_action"] = "Implement and review an equivalent native adapter before product tests."
        return report, 2

    checks: list[dict[str, str]] = []
    report["checks"] = checks
    with tempfile.TemporaryDirectory(prefix="context-platform-", dir=parent) as temp:
        base = Path(temp)
        for name, action in (
            ("file_boundaries_and_atomic_publish", _file_boundary_check),
            ("mount_identity_change", _mount_identity_check),
            ("cross_process_kernel_lease", _lease_check),
            ("lease_replacement_detection", _lease_replacement_check),
        ):
            try:
                case_root = base / name
                case_root.mkdir()
                action(case_root)
            except Exception as error:
                checks.append({"name": name, "status": "failed", "error": type(error).__name__})
            else:
                checks.append({"name": name, "status": "passed"})
    if all(check["status"] == "passed" for check in checks):
        report["status"] = "native_safety_checks_passed"
        return report, 0
    report["status"] = "failed"
    return report, 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run native file-safety checks on this real host; this is not a release support claim."
    )
    parser.add_argument(
        "--parent",
        type=Path,
        help="Existing directory in the target filesystem under which a temporary test directory is created.",
    )
    arguments = parser.parse_args()
    if arguments.parent is not None and not arguments.parent.is_dir():
        parser.error("--parent must be an existing directory")
    report, code = run_native_acceptance(arguments.parent)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
