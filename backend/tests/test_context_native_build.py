"""Offline native build output guards, no actual dependency install or freeze."""

from __future__ import annotations

import importlib.util
from pathlib import Path

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


@pytest.mark.parametrize("host", ["linux", "win32"])
def test_desktop_freeze_does_not_claim_other_hosts_supported(tmp_path, monkeypatch, host):
    tool = build_tool()
    monkeypatch.setattr(tool.sys, "platform", host)
    with pytest.raises(ValueError, match="verified"):
        tool.freeze_command(tmp_path, tmp_path / "source", tmp_path / "entry.py", desktop=True)


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
