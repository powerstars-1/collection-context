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
