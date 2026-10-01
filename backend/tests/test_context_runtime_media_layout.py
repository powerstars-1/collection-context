"""Fixed bundled package guards; synthetic files, no install or network."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from collection_context.application import runtime_setup as setup
from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_download as downloads
from collection_context.infrastructure import runtime_media_layout as layout


def frozen(monkeypatch, root):
    monkeypatch.setattr(layout.sys, "frozen", True, raising=False)
    monkeypatch.setattr(layout.sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(layout.sys, "executable", str(root / "CollectionContext"))
    monkeypatch.setattr(layout, "MEDIA_BYTES", 7)


def package(root):
    path = root / "native_software" / layout.MEDIA_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    return path


def test_source_mode_never_uses_neighbor_archive(tmp_path, monkeypatch):
    package(tmp_path)
    monkeypatch.delattr(layout.sys, "frozen", raising=False)
    with pytest.raises(ContextError) as caught:
        layout.bundled_media_archive()
    assert caught.value.code == "runtime_component_not_bundled"


def test_onedir_uses_fixed_resource_only(tmp_path, monkeypatch):
    path = package(tmp_path)
    frozen(monkeypatch, tmp_path)
    assert layout.bundled_media_archive() == path
    assert layout.bundled_media_state() == "available"


def test_macos_bundle_uses_real_resources_not_framework_alias(tmp_path, monkeypatch):
    contents = tmp_path / "CollectionContextDesktop.app" / "Contents"
    path = package(contents / "Resources")
    (contents / "MacOS").mkdir()
    frozen(monkeypatch, contents / "Frameworks")
    monkeypatch.setattr(layout.sys, "platform", "darwin")
    monkeypatch.setattr(layout.sys, "executable", str(contents / "MacOS" / "CollectionContextDesktop"))
    assert layout.bundled_media_archive() == path


@pytest.mark.parametrize("kind", ["missing", "size", "link", "hardlink", "directory", "parent_link"])
def test_missing_or_unsafe_package_has_no_fallback(tmp_path, monkeypatch, kind):
    root = tmp_path / "real"
    path = package(root)
    frozen(monkeypatch, root)
    if kind == "missing":
        path.unlink()
    elif kind == "size":
        path.write_bytes(b"changed size")
    elif kind == "link":
        target = tmp_path / "outside"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, tmp_path / "other")
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        monkeypatch.setattr(layout.sys, "_MEIPASS", str(alias))
    assert layout.bundled_media_state() == (
        "bundled_component_missing" if kind == "missing" else "bundled_component_invalid"
    )


def test_bundled_source_does_not_construct_public_transport(tmp_path, monkeypatch):
    selected = setup.CATALOG[layout.MEDIA_ID]
    marker = tmp_path / "fixed-package"
    monkeypatch.setattr(downloads, "bundled_media_archive", lambda: marker)
    monkeypatch.setattr(downloads, "PublicHTTP", lambda **kw: pytest.fail("network constructor"))
    with downloads.RuntimeDownloads(tmp_path, catalog=setup.CATALOG) as source:
        assert source(selected) == marker
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "field,value", [("sha256", "0" * 64), ("bytes", 7), ("source_url", "https://example.org/a")]
)
def test_media_identity_cannot_turn_into_user_download(tmp_path, monkeypatch, field, value):
    selected = replace(setup.CATALOG[layout.MEDIA_ID], **{field: value})
    monkeypatch.setattr(downloads, "PublicHTTP", lambda **kw: pytest.fail("network"))
    monkeypatch.setattr(downloads, "bundled_media_archive", lambda: pytest.fail("package lookup"))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError) as caught:
            source(selected)
    assert caught.value.code == "runtime_download_catalog"


def test_media_options_distinguish_source_url_and_network_bytes(monkeypatch):
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(setup.metadata, "version", lambda name: pytest.fail("browser SDK not needed"))
    monkeypatch.setattr(setup, "bundled_media_state", lambda: "available")
    assert setup._availability(browser=False) == "available"
    monkeypatch.setattr(setup.metadata, "version", lambda name: "1.63.0")
    options = setup.runtime_options()
    item = next(x for x in options["artifacts"] if x["id"] == layout.MEDIA_ID)
    assert item["download_bytes"] == 0 and item["archive_bytes"] == layout.MEDIA_BYTES
    assert item["delivery"] == "bundled"
    assert item["source_url_kind"] == "upstream_source_not_binary_download"
    assert item["functional_verified"] is False and "ffmpeg_pair" not in options["not_available"]
