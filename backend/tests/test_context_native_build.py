"""Offline native build output guards, no actual dependency install or freeze."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def build_tool():
    source = Path(__file__).parents[1] / "tools/native_distribution/build.py"
    spec = importlib.util.spec_from_file_location("native_build_tool", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["root", "home", "repository", "repository_child"])
def test_native_output_rejects_broad_and_repository_targets(tmp_path, kind):
    repository = tmp_path / "repository"
    path = {
        "root": Path(tmp_path.anchor),
        "home": Path.home(),
        "repository": repository,
        "repository_child": repository / "build",
    }[kind]
    with pytest.raises(ValueError):
        build_tool().checked_output(path, repository)


def test_native_output_resolves_links_to_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    link = tmp_path / "outside"
    link.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ValueError):
        build_tool().checked_output(link / "build", repository)


def test_native_output_accepts_specific_external_directory_without_creating(tmp_path):
    output = tmp_path / "build-archive"
    assert build_tool().checked_output(output, tmp_path / "repository") == output
    assert not output.exists()


def test_fixed_media_identity_comes_from_product_literals():
    from collection_context.infrastructure import runtime_media_layout as layout

    assert build_tool().media_identity() == {
        "filename": layout.MEDIA_FILENAME,
        "bytes": layout.MEDIA_BYTES,
        "sha256": layout.MEDIA_SHA256,
    }


@pytest.mark.parametrize("kind", ["wrong_hash", "size", "link", "hardlink", "directory", "relative"])
def test_media_build_rejects_unfixed_or_unsafe_archive(tmp_path, monkeypatch, kind):
    import os

    tool = build_tool()
    root = tmp_path.resolve()
    path = root / "package.zip"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(
        tool,
        "media_identity",
        lambda: {"filename": "fixed.zip", "bytes": 7, "sha256": hashlib.sha256(b"fixture").hexdigest()},
    )
    if kind == "wrong_hash":
        path.write_bytes(b"changed")
    elif kind == "size":
        path.write_bytes(b"wrong")
    elif kind == "link":
        original = path
        path = root / "link"
        path.symlink_to(original)
    elif kind == "hardlink":
        os.link(path, root / "other")
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "relative":
        path = Path("package.zip")
    with pytest.raises(ValueError):
        tool.checked_media_package(path)


def test_component_hash_read_ignores_only_access_time(tmp_path, monkeypatch):
    tool = build_tool()
    path = tmp_path.resolve() / "original-package.zip"
    path.write_bytes(b"original fixed content")
    identity = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    original = Path.stat
    calls = 0

    def stat_with_access_time(self, *args, **kwargs):
        nonlocal calls
        value = original(self, *args, **kwargs)
        if self != path:
            return value
        calls += 1
        # Model the OS read effect without utime, which would also mutate ctime.
        attrs = {
            field: getattr(value, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_atime_ns",
            )
        }
        attrs["st_atime_ns"] += calls * 2_000_000_000
        return SimpleNamespace(**attrs)

    monkeypatch.setattr(Path, "stat", stat_with_access_time)
    assert tool.checked_component_package(path, identity) == identity


@pytest.mark.parametrize(
    "field",
    [
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ],
)
def test_component_hash_read_rejects_identity_or_mutation_time_change(tmp_path, monkeypatch, field):
    tool = build_tool()
    path = tmp_path.resolve() / "original-package.zip"
    path.write_bytes(b"original fixed content")
    identity = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    original = tool.hashlib.file_digest

    def digest_then_replace_version(stream, name):
        digest = original(stream, name)
        fstat = tool.os.fstat

        def changed(fd):
            info = fstat(fd)
            attrs = {
                key: getattr(info, key)
                for key in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_uid",
                    "st_gid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            }
            attrs[field] += 1
            return SimpleNamespace(**attrs)

        monkeypatch.setattr(tool.os, "fstat", changed)
        return digest

    monkeypatch.setattr(tool.hashlib, "file_digest", digest_then_replace_version)
    with pytest.raises(ValueError, match="content differs"):
        tool.checked_component_package(path, identity)


@pytest.mark.parametrize("kind", ["symlink_to_same_inode", "replacement_with_same_content"])
def test_component_rejects_path_swap_after_descriptor_hash(tmp_path, monkeypatch, kind):
    tool = build_tool()
    path = tmp_path.resolve() / "original-package.zip"
    body = b"original fixed content"
    path.write_bytes(body)
    identity = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    original = tool.hashlib.file_digest

    def digest_then_swap(stream, name):
        value = original(stream, name)
        retained = path.with_name("retained-original.zip")
        path.rename(retained)
        if kind == "symlink_to_same_inode":
            path.symlink_to(retained)
        else:
            path.write_bytes(body)
        return value

    monkeypatch.setattr(tool.hashlib, "file_digest", digest_then_swap)
    with pytest.raises(ValueError, match="content differs"):
        tool.checked_component_package(path, identity)


def test_bundled_media_resource_in_both_console_and_app_commands(tmp_path, monkeypatch):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    (tmp_path / "native_software").mkdir()
    for desktop in (False, True):
        command = tool.freeze_command(tmp_path, tmp_path / "source", tmp_path / "entry.py", desktop=desktop)
        assert str(tmp_path / "native_software") + ":native_software" in command


def test_desktop_freeze_preserves_separate_console_entry_and_mcp_metadata(tmp_path, monkeypatch):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    source = tmp_path / "src/collection_context"
    console = tool.freeze_command(tmp_path, source, tmp_path / "entry.py")
    desktop = tool.freeze_command(tmp_path, source, tmp_path / "desktop_entry.py", desktop=True)
    assert "--console" in console and "--windowed" not in console
    assert "--windowed" in desktop and "--console" not in desktop
    assert console[console.index("--name") + 1] == "CollectionContext"
    assert desktop[desktop.index("--name") + 1] == "CollectionContextDesktop"
    assert console[-1] == str(tmp_path / "entry.py")
    assert desktop[-1] == str(tmp_path / "desktop_entry.py")
    assert "--osx-bundle-identifier" in desktop
    for command in (console, desktop):
        assert "mcp.server" in command and "mcp.shared" in command and "mcp" in command
        assert "--onedir" in command and "--noupx" in command
        assert "--codesign-identity" not in command and "--argv-emulation" not in command
        assert command[command.index("--hidden-import") + 1] == "playwright.sync_api"
        assert command[command.index("--copy-metadata") + 1] == "playwright"
        assert "--collect-all" not in command and "rapidocr" not in command


@pytest.mark.parametrize("host", ["linux", "win32"])
def test_desktop_freeze_does_not_claim_other_hosts_supported(tmp_path, monkeypatch, host):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", host)
    with pytest.raises(ValueError, match="verified"):
        tool.freeze_command(tmp_path, tmp_path / "source", tmp_path / "entry.py", desktop=True)


def test_fixed_ocr_identity_comes_from_product_literals():
    from collection_context.infrastructure import runtime_ocr_layout as layout

    assert build_tool().ocr_identity() == {
        "filename": layout.OCR_FILENAME,
        "bytes": layout.OCR_BYTES,
        "sha256": layout.OCR_SHA256,
    }


def test_ocr_freeze_uses_only_metadata_not_implicit_weight_directory(tmp_path):
    command = build_tool().freeze_command(tmp_path, tmp_path / "source", tmp_path / "entry.py", ocr=True)
    assert "rapidocr" in command and "onnxruntime" in command
    assert str(tmp_path / "ocr_metadata/config.yaml") + ":rapidocr" in command
    assert str(tmp_path / "ocr_metadata/default_models.yaml") + ":rapidocr" in command
    assert "--collect-all" not in command and "--collect-data" not in command
    assert not any(".onnx" in p or "/models" in p for p in command)


def test_desktop_entry_selects_only_desktop_mode_not_implicit_data_directory(monkeypatch):
    import runpy

    from collection_context import native_bootstrap

    seen = []
    monkeypatch.setattr(native_bootstrap, "dispatch", lambda args: seen.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["CollectionContextDesktop", "--port", "18798"])
    source = Path(__file__).parents[1] / "tools/native_distribution/desktop_entry.py"
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(source), run_name="__main__")
    assert exited.value.code == 0 and seen == [["desktop", "--port", "18798"]]


@pytest.mark.parametrize("suffix", [".txt", ".env", ".pem", ".json"])
def test_native_source_rejects_unregistered_resources(tmp_path, suffix):
    source = tmp_path / "source"
    source.mkdir()
    (source / ("private" + suffix)).write_text("synthetic private fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="whitelist"):
        build_tool().validate_source(source)


def test_native_source_only_accepts_exact_resource_notice(tmp_path):
    source = tmp_path / "source"
    assets = source / "interfaces" / "assets"
    assets.mkdir(parents=True)
    (source / "main.py").write_text("# synthetic source", encoding="utf-8")
    (assets / "app.js").write_text("// synthetic asset", encoding="utf-8")
    (assets / "THIRD_PARTY_NOTICES.txt").write_text("synthetic license", encoding="utf-8")
    build_tool().validate_source(source)
    (source / "THIRD_PARTY_NOTICES.txt").write_text("not registered here", encoding="utf-8")
    with pytest.raises(ValueError, match="whitelist"):
        build_tool().validate_source(source)


@pytest.mark.parametrize("kind", ["file", "directory", "root"])
def test_native_source_refuses_links_without_copying_their_targets(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "private.py"
    marker.write_text("# synthetic outside data", encoding="utf-8")
    checked = source
    if kind == "root":
        checked = tmp_path / "linked-source"
        checked.symlink_to(outside, target_is_directory=True)
    elif kind == "directory":
        (source / "linked").symlink_to(outside, target_is_directory=True)
    else:
        (source / "linked.py").symlink_to(marker)
    with pytest.raises(ValueError, match="links|real directory"):
        build_tool().validate_source(checked)
    assert marker.read_text(encoding="utf-8") == "# synthetic outside data"


def browser_sdk_fixture(tmp_path, monkeypatch, tool, *, windows=False):
    site = tmp_path / "isolated-site"
    names = (
        "playwright/driver/" + ("node.exe" if windows else "node"),
        "playwright/driver/package/cli.js",
        "playwright/driver/package/browsers.json",
        "playwright/driver/LICENSE",
        "playwright/driver/package/LICENSE",
        "playwright/driver/package/NOTICE",
        "playwright/driver/package/ThirdPartyNotices.txt",
        "playwright/_impl/__pyinstaller/hook-playwright.sync_api.py",
        "playwright/_impl/__pyinstaller/__init__.py",
    )
    for name in names:
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original synthetic SDK fixture")
        path.chmod(0o700)
    distribution = SimpleNamespace(files=[Path(name) for name in names], locate_file=lambda p: site / p)
    monkeypatch.setattr(tool.metadata, "distribution", lambda name: distribution)
    return site, distribution


@pytest.mark.parametrize("host", ["darwin", "linux", "win32"])
def test_browser_sdk_checks_only_registered_wheel_resources(tmp_path, monkeypatch, host):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", host)
    browser_sdk_fixture(tmp_path, monkeypatch, tool, windows=host == "win32")
    report = tool.checked_browser_sdk()
    assert report == {
        "package_version": "1.63.0",
        "driver_node_input_sha256": hashlib.sha256(b"original synthetic SDK fixture").hexdigest(),
        "collection": "official_playwright_sync_api_hook",
        "browser_binaries_included": False,
        "frozen_driver_execution_verified": False,
        "license_closure": "unresolved",
    }


@pytest.mark.parametrize("kind", ["directory", "file", "broken_link"])
def test_browser_sdk_rejects_local_browser_cache_without_removing(tmp_path, monkeypatch, kind):
    tool = build_tool()
    site, _ = browser_sdk_fixture(tmp_path, monkeypatch, tool)
    cache = site / "playwright/driver/package/.local-browsers"
    if kind == "directory":
        cache.mkdir()
    elif kind == "file":
        cache.write_bytes(b"original browser marker")
    else:
        cache.symlink_to(tmp_path / "nonexistent-cache")
    with pytest.raises(ValueError, match="local-browsers"):
        tool.checked_browser_sdk()
    assert cache.exists() or cache.is_symlink()


@pytest.mark.parametrize("kind", ["link", "unregistered", "missing", "empty", "not_executable"])
def test_browser_sdk_refuses_incomplete_or_contaminated_resources(tmp_path, monkeypatch, kind):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    site, distribution = browser_sdk_fixture(tmp_path, monkeypatch, tool)
    node = site / "playwright/driver/node"
    if kind == "link":
        link = site / "playwright/driver/package/linked"
        link.symlink_to(tmp_path / "outside")
    elif kind == "unregistered":
        (site / "playwright/driver/private.json").write_bytes(b"synthetic marker")
    elif kind == "missing":
        distribution.files = [
            name for name in distribution.files if name.as_posix() != "playwright/driver/node"
        ]
    elif kind == "empty":
        node.write_bytes(b"")
    else:
        node.chmod(0o600)
    with pytest.raises(ValueError):
        tool.checked_browser_sdk()


def test_contaminated_browser_sdk_fails_before_output_or_freeze(tmp_path, monkeypatch):
    tool = build_tool()
    site, _ = browser_sdk_fixture(tmp_path, monkeypatch, tool)
    (site / "playwright/driver/package/.local-browsers").mkdir()
    monkeypatch.setattr(tool.metadata, "version", lambda package: tool.VERSIONS[package])
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **kw: pytest.fail("must not freeze"))
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="local-browsers"):
        tool.build(output)
    assert not output.exists()


def test_playwright_direct_pin_is_declared_and_ocr_not_added():
    tool = build_tool()
    requirements = Path(__file__).parents[1] / "tools/native_distribution/requirements-build.in"
    assert tool.VERSIONS["playwright"] == "1.63.0"
    assert "playwright==1.63.0" in requirements.read_text(encoding="utf-8")
    assert "rapidocr" not in tool.VERSIONS and "onnxruntime" not in tool.VERSIONS
