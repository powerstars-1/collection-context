"""Inert archives and descriptor checks; no browser or external network."""

import hashlib
import io
import json
import os
import stat
import zipfile
from types import MappingProxyType

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_browser_layout as layout
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies
from collection_context.infrastructure.runtime_installation import ArtifactPlan, RuntimeInstaller, ToolSpec


def fixture(tmp_path, monkeypatch, *, variant=None, allow=True):
    aliases = {"bundle/Resources": "Versions/Current/Resources", "bundle/Versions/Current": "1"}
    entries = [
        ("bundle/tool", b"original inert tool", stat.S_IFREG),
        ("bundle/Versions/1/Resources/data", b"original data", stat.S_IFREG),
        *((name, target.encode(), stat.S_IFLNK) for name, target in aliases.items()),
    ]
    if variant == "missing":
        entries.pop()
    elif variant == "outside":
        entries[-1] = (entries[-1][0], b"../../outside", stat.S_IFLNK)
    elif variant == "extra":
        entries.append(("bundle/unknown", b"tool", stat.S_IFLNK))
    elif variant == "ordinary":
        entries[-1] = (*entries[-1][:2], stat.S_IFREG)
    elif variant == "duplicate":
        entries.append(entries[-1])
    elif variant == "alias-child":
        entries.append(("bundle/Resources/escape", b"bad", stat.S_IFREG))
    elif variant == "absent-target":
        entries.pop(1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content, kind in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = (kind | 0o755) << 16
            archive.writestr(info, content)
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    executable_sha = hashlib.sha256(entries[0][1]).hexdigest()
    monkeypatch.setattr(layout, "HEADED_ID", "inert-browser")
    monkeypatch.setattr(layout, "HEADED_SHA256", sha)
    monkeypatch.setattr(layout, "HEADED_SOURCE", "https://example.org/inert.zip")
    monkeypatch.setattr(layout, "HEADED_EXECUTABLE", "bundle/tool")
    monkeypatch.setattr(layout, "HEADED_EXECUTABLE_SHA256", executable_sha)
    monkeypatch.setattr(layout, "ALIASES", MappingProxyType(aliases))
    monkeypatch.setattr(
        "collection_context.infrastructure.runtime_dependencies.metadata.version", lambda _: "1.63.0"
    )
    archive_path = tmp_path / "inert.zip"
    archive_path.write_bytes(data)
    plan = ArtifactPlan(
        "inert-browser" if allow else "generic-browser",
        "Darwin",
        "arm64",
        "1",
        "https://example.org/inert.zip",
        sha,
        len(data),
        "zip",
        (
            ToolSpec(
                "chromium",
                "bundle/tool",
                len(entries[0][1]),
                executable_sha,
                "1",
                "LicenseRef-Test",
                playwright_package_version="1.63.0",
                playwright_revision="1243",
            ),
        ),
    )
    runtime = tmp_path / "runtime"
    library = tmp_path / "absent-library"
    installer = RuntimeInstaller(
        runtime, library_dir=library, catalog={plan.id: plan}, _archive_source=lambda _: archive_path
    )
    return installer, plan, runtime, library


def test_fixed_literal_aliases_survive_and_registry_rechecks_them(tmp_path, monkeypatch):
    installer, plan, runtime, library = fixture(tmp_path, monkeypatch)
    result = installer.install(plan.id, installation_confirmed=True)
    assert result["static_verified"] and not result["functional_verified"]
    registry = RuntimeDependencies(runtime, library_dir=library)
    tool = registry.resolve("chromium")
    assert tool.path.is_file()
    receipt = json.loads((runtime / "runtime-dependencies.json").read_text())
    prefix = receipt["tools"]["chromium"]["relative_path"].removesuffix("bundle/tool")
    current = runtime / prefix / "bundle/Versions/Current"
    assert current.is_symlink() and os.readlink(current) == "1"
    current.unlink()
    current.symlink_to("../../outside")
    with pytest.raises(ContextError) as caught:
        registry.resolve("chromium")
    assert caught.value.code == "runtime_dependency_unsafe"
    assert not library.exists()


@pytest.mark.parametrize(
    "variant", ["missing", "outside", "extra", "ordinary", "duplicate", "alias-child", "absent-target"]
)
def test_alias_mismatch_never_publishes_receipt(tmp_path, monkeypatch, variant):
    installer, plan, runtime, _ = fixture(tmp_path, monkeypatch, variant=variant)
    with pytest.raises(ContextError):
        installer.install(plan.id, installation_confirmed=True)
    assert not (runtime / "runtime-dependencies.json").exists()


def test_generic_archive_still_rejects_same_links(tmp_path, monkeypatch):
    installer, plan, runtime, _ = fixture(tmp_path, monkeypatch, allow=False)
    with pytest.raises(ContextError) as caught:
        installer.install(plan.id, installation_confirmed=True)
    assert caught.value.code == "runtime_install_unsafe"
    assert not (runtime / "runtime-dependencies.json").exists()


def test_whole_archive_identity_controls_the_exception():
    assert layout.archive_aliases(layout.HEADED_ID, "0" * 64, layout.HEADED_SOURCE) == {}
    assert layout.archive_aliases("unknown", layout.HEADED_SHA256, layout.HEADED_SOURCE) == {}
    aliases = layout.archive_aliases(layout.HEADED_ID, layout.HEADED_SHA256, layout.HEADED_SOURCE)
    assert len(aliases) == 5 and all(not target.startswith("/") for target in aliases.values())


@pytest.mark.parametrize(
    "aliases", [{"a": "a"}, {"a": "b", "b": "a"}, {"a": "/outside"}, {"a": "../outside"}]
)
def test_canonical_graph_refuses_cycles_and_escape(aliases):
    with pytest.raises(ContextError):
        layout._canonical_target("a", aliases)
