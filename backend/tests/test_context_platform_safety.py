"""Native platform checks do not simulate another operating system."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.platform_safety import detect_platform_safety

NATIVE = detect_platform_safety()
needs_safe_files = pytest.mark.skipif(
    NATIVE.safe_files_backend is None, reason="native safe-files adapter is intentionally unavailable"
)


def error_code(action) -> str:
    with pytest.raises(ContextError) as caught:
        action()
    return caught.value.code


def test_capability_report_is_for_actual_runtime_only():
    report = detect_platform_safety()
    assert report.os_name == os.name
    assert report.system == (platform.system() or "unknown")
    assert report.machine == (platform.machine() or "unknown")
    if os.name != "posix":
        assert not report.runtime_supported
        assert "safe_files_backend_not_implemented" in report.blockers


@needs_safe_files
def test_unpublished_new_file_is_removed_when_file_sync_fails(tmp_path, monkeypatch):
    root = tmp_path / "atomic"
    root.mkdir()
    files = SafeFiles(root)
    real_fsync = os.fsync
    calls = 0

    def fail_first_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic interrupted file flush")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_sync)
    try:
        assert error_code(lambda: files.write("未发布.txt", b"partial")) == "storage_unavailable"
        assert not (root / "未发布.txt").exists()
        assert not list(root.glob(".tmp-*"))
    finally:
        files.close()


@needs_safe_files
def test_no_replace_publication_is_atomic_and_preserves_existing_target(tmp_path):
    root = tmp_path / "atomic"
    root.mkdir()
    with SafeFiles(root) as files:
        files.write("内容.txt", b"old")
        assert os.stat(root / "内容.txt").st_nlink == 1
        assert error_code(lambda: files.write("内容.txt", b"new")) == "write_conflict"
        assert files.read("内容.txt") == b"old"
        assert not list(root.glob(".tmp-*"))


@needs_safe_files
def test_interrupted_publish_cleanup_leaves_complete_target_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "publish-window"
    root.mkdir()
    files = SafeFiles(root)
    real_unlink = os.unlink

    def interrupt_temp_cleanup(path, *args, **kwargs):
        if str(path).startswith(".tmp-"):
            raise OSError("synthetic crash window")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", interrupt_temp_cleanup)
    try:
        assert error_code(lambda: files.write("target.txt", b"complete")) == "storage_unavailable"
        monkeypatch.setattr(os, "unlink", real_unlink)
        assert (root / "target.txt").read_bytes() == b"complete"
        assert error_code(lambda: files.read("target.txt")) == "forbidden_path"
        temporary = list(root.glob(".tmp-*"))
        assert len(temporary) == 1 and os.stat(temporary[0]).st_ino == os.stat(root / "target.txt").st_ino
    finally:
        files.close()


@needs_safe_files
def test_read_detects_hardlink_added_during_open_descriptor_read(tmp_path, monkeypatch):
    root = tmp_path / "race"
    root.mkdir()
    files = SafeFiles(root)
    files.write("source.txt", b"stable")
    real_fstat = os.fstat
    calls = 0

    def add_link_before_second_snapshot(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.link(root / "source.txt", root / "racing-link")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", add_link_before_second_snapshot)
    try:
        assert error_code(lambda: files.read("source.txt")) == "version_changed"
    finally:
        files.close()


def test_native_acceptance_module_reports_only_this_runtime():
    source_root = Path(__file__).parents[1] / "src"
    environment = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run(
        [sys.executable, "-m", "collection_context.infrastructure.platform_acceptance"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    report = json.loads(result.stdout)
    assert report["runtime"]["system"] == (platform.system() or "unknown")
    assert report["runtime"]["machine"] == (platform.machine() or "unknown")
    assert report["release_support_claim"] is False
    assert report["full_product_regression"] is False
    if NATIVE.runtime_supported:
        assert result.returncode == 0
        assert report["status"] == "native_safety_checks_passed"
        assert all(check["status"] == "passed" for check in report["checks"])
    else:
        assert result.returncode == 2
        assert report["status"] == "unsupported"
