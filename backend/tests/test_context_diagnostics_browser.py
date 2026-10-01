"""Static diagnostics never claim browser execution or invent registry paths."""

import builtins
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from collection_context import diagnostics


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "playwright"
    manifest = root / "driver" / "package" / "browsers.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "1243"},
                    {"name": "chromium-headless-shell", "revision": "1243"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        diagnostics.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(submodule_search_locations=[str(root)]),
    )
    monkeypatch.setattr(diagnostics, "_version", lambda _: "1.63.0")
    return root, manifest


def sdk(path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Browser launch is forbidden in static diagnostics")

    return SimpleNamespace(
        chromium=SimpleNamespace(
            executable_path=str(path), launch=forbidden, launch_persistent_context=forbidden
        )
    )


def executable(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original static test fixture; never executed")
    path.chmod(0o700)
    return path


def assert_unverified(report):
    assert report["available"] is False
    assert report["static_available"] is False
    assert report["runtime_verified"] is False
    assert report["headless"] == {
        "state": "not_verified",
        "static_available": False,
        "runtime_verified": False,
    }
    assert report["runtime_check"] == "static_only_no_process_started"
    assert "未功能探测" in report["next_action"]
    assert "安装" not in report["next_action"]


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_cache_directory_or_complete_files_never_prove_default_readiness(
    package, tmp_path, monkeypatch, system
):
    monkeypatch.setattr(diagnostics.sys, "platform", system)
    cache = tmp_path / "cache"
    # Regression: a non-empty revision directory formerly reported available=True.
    executable(cache, "chromium-1243/not-a-browser.txt")
    executable(cache, "chromium_headless_shell-1243/not-a-browser.txt")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    report = diagnostics._playwright_browser()
    assert_unverified(report)
    assert report["package_present"] is True
    assert report["manifest_verified"] is True
    assert report["desktop"]["state"] == "not_verified"


@pytest.mark.parametrize(
    "system,relative",
    [
        ("linux", "chromium-1243/chrome-linux/chrome"),
        ("darwin", "chromium-1243/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
        ("win32", "chromium-1243/chrome-win/chrome.exe"),
    ],
)
def test_explicit_sdk_paths_checked_without_platform_layout_guessing(
    package, tmp_path, monkeypatch, system, relative
):
    path = executable(tmp_path, relative)
    monkeypatch.setattr(diagnostics.sys, "platform", system)
    report = diagnostics._playwright_browser(sdk=sdk(path))
    assert_unverified(report)
    assert report["desktop"] == {
        "state": "static_available",
        "static_available": True,
        "runtime_verified": False,
    }
    assert str(path) not in json.dumps(report)


def test_missing_desktop_even_when_shell_file_exists(package, tmp_path):
    executable(tmp_path, "chromium_headless_shell-1243/headless_shell")
    report = diagnostics._playwright_browser(sdk=sdk(tmp_path / "chromium-1243/missing"))
    assert_unverified(report)
    assert report["desktop"]["static_available"] is False


def test_present_desktop_cannot_prove_missing_or_present_shell(package, tmp_path):
    desktop = executable(tmp_path, "chromium-1243/official-sdk-selected-file")
    missing = diagnostics._playwright_browser(sdk=sdk(desktop))
    executable(tmp_path, "chromium_headless_shell-1243/headless_shell")
    present = diagnostics._playwright_browser(sdk=sdk(desktop))
    assert missing == present
    assert_unverified(present)


def test_wrong_path_revision_is_not_verified(package, tmp_path):
    path = executable(tmp_path, "chromium-1242/chrome")
    report = diagnostics._playwright_browser(sdk=sdk(path))
    assert_unverified(report)
    assert report["desktop"]["state"] == "revision_not_verified"


def test_executable_permission_required(package, tmp_path, monkeypatch):
    path = executable(tmp_path, "chromium-1243/chrome")
    path.chmod(0o600)
    original = diagnostics.os.access
    monkeypatch.setattr(diagnostics.os, "access", lambda p, mode: False if p == path else original(p, mode))
    report = diagnostics._playwright_browser(sdk=sdk(path))
    assert_unverified(report)
    assert report["desktop"]["state"] == "not_executable"


@pytest.mark.parametrize("kind", ["file", "parent", "manifest"])
def test_symlinks_rejected(package, tmp_path, kind):
    path = executable(tmp_path, "chromium-1243/chrome")
    root, manifest = package
    if kind == "file":
        link = path.with_name("link")
        link.symlink_to(path)
        path = link
    elif kind == "parent":
        link = tmp_path / "linked-cache"
        link.symlink_to(tmp_path / "chromium-1243", target_is_directory=True)
        path = link / "chrome"
    else:
        real_manifest = root / "manifest.json"
        manifest.rename(real_manifest)
        manifest.symlink_to(real_manifest)
    report = diagnostics._playwright_browser(sdk=sdk(path))
    assert_unverified(report)
    assert report["desktop"]["static_available"] is False


@pytest.mark.parametrize(
    "contents",
    [
        b"x" * 65_537,
        b"{malformed",
        b"\xff",
        b"[]",
        b'{"browsers":{}}',
        b'{"browsers":[{}]}',
        b'{"browsers":[null]}',
        b'{"browsers":[{"name":"chromium","revision":1243}]}',
        b'{"browsers":[{"name":"chromium","revision":"1243"}]}',
        b'{"browsers":[{"name":"chromium-headless-shell","revision":"1243"}]}',
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "1243"},
                    {"name": "chromium-headless-shell", "revision": "1242"},
                ]
            }
        ).encode(),
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "1243", "revisionOverrides": {"mac14": "1242"}},
                    {"name": "chromium-headless-shell", "revision": "1243"},
                ]
            }
        ).encode(),
    ],
)
def test_bounded_manifest_and_shape_errors_downgrade(package, tmp_path, contents):
    _, manifest = package
    manifest.write_bytes(contents)
    path = executable(tmp_path, "chromium-1243/chrome")
    report = diagnostics._playwright_browser(sdk=sdk(path))
    assert_unverified(report)
    assert report["manifest_verified"] is False
    assert report["desktop"]["static_available"] is False


def test_missing_package_and_find_spec_errors_are_honest(package, monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda _: None)
    missing = diagnostics._playwright_browser()
    assert_unverified(missing)
    assert missing["package_present"] is False

    def fail(_):
        raise ValueError("private environment value")

    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", fail)
    broken = diagnostics._playwright_browser()
    assert_unverified(broken)
    assert "private" not in json.dumps(broken)


def test_sdk_and_metadata_exceptions_do_not_leak_or_imply_success(package, monkeypatch):
    class Broken:
        @property
        def chromium(self):
            raise RuntimeError("private SDK configuration")

    def fail(_):
        raise OSError("private metadata path")

    monkeypatch.setattr(diagnostics, "_version", fail)
    report = diagnostics._playwright_browser(sdk=Broken())
    assert_unverified(report)
    assert report["version"] is None
    assert "private" not in json.dumps(report)


def test_manifest_read_permission_error_downgrades_without_leaking(package, monkeypatch):
    _, manifest = package
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path == manifest:
            raise PermissionError("private manifest path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    report = diagnostics._playwright_browser()
    assert_unverified(report)
    assert report["manifest_verified"] is False
    assert "private" not in json.dumps(report)


def test_no_process_network_or_profile_writes(package, tmp_path, monkeypatch):
    path = executable(tmp_path, "chromium-1243/chrome")
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("side_effect")
        raise AssertionError("Diagnostics must not start processes/network or create profiles")

    original_import = builtins.__import__

    def import_guard(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            attempts.append("sdk_import")
            raise AssertionError("Static diagnostics must not initialize a driver")
        return original_import(name, *args, **kwargs)

    before = sorted(str(p) for p in tmp_path.rglob("*"))
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(diagnostics.socket, "socket", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(builtins, "__import__", import_guard)
    assert_unverified(diagnostics._playwright_browser())
    chromium = SimpleNamespace(
        executable_path=str(path), launch=forbidden, launch_persistent_context=forbidden
    )
    report = diagnostics._playwright_browser(sdk=SimpleNamespace(chromium=chromium))
    assert report["desktop"]["static_available"] is True
    assert_unverified(report)
    assert attempts == []
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_browser_role_does_not_block_management_report(package, monkeypatch):
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(diagnostics, "_python_dependency", lambda **kwargs: {**kwargs, "available": True})
    report = diagnostics.dependency_report()
    assert report["ready_for_management_page"] is True
    browser = next(item for item in report["dependencies"] if item["role"] == "source_browser")
    assert_unverified(browser)
    assert report["downloads_performed"] is False
