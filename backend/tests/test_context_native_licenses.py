"""Only original synthetic notices/metadata; no third-party downloads or execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def collector(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "tools/native_distribution/licenses.py"
    spec = importlib.util.spec_from_file_location("native_license_collector_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repository"
    notice = repo / "backend/src/collection_context/interfaces/assets/THIRD_PARTY_NOTICES.txt"
    notice.parent.mkdir(parents=True)
    notice.write_bytes(b"Original synthetic frontend notice for collector testing.\n")
    monkeypatch.setattr(module, "REPOSITORY", repo)
    monkeypatch.setattr(module, "FRONTEND_NOTICE", notice)
    return module


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / "isolated build environment 中文"
    root.mkdir()
    (root / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
    site = root / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    add_distribution(site, "original-component", "1.2.3")
    return root, site


def add_distribution(site, name, version, *, directory_name=None):
    path = site / (directory_name or f"{name.replace('-', '_')}-{version}.dist-info")
    path.mkdir()
    (path / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    (path / "licenses").mkdir()
    (path / "licenses/LICENSE").write_bytes(b"Original test permission text, not a real package license.\n")
    return path


def collect(collector, environment, output):
    return collector.collect_licenses(environment[0], output, collector.FRONTEND_NOTICE)


def test_exact_text_archive_hashes_and_honest_scope(collector, environment, tmp_path):
    add_distribution(environment[1], "another-component", "2.0")
    output = tmp_path / "new outside stage"
    report = collect(collector, environment, output)
    index_path = output / "license-index.json"
    index = json.loads(index_path.read_bytes())
    assert report["distribution_count"] == 2
    assert index["audit_scope"] == "build_environment_license_inventory"
    assert index["native_license_closure"] == "unresolved"
    assert index["runtime_sbom"] is False
    assert index["module_namespace_coverage"] == "not_determined"
    assert {r["component"] for r in index["unresolved"]} >= {
        "CPython",
        "Tcl/Tk",
        "OpenSSL",
        "native_embedded_dependencies",
    }
    for component in index["components"]:
        assert component["bundled_in_application"] == "not_determined"
        for license in component["licenses"]:
            data = (output / license["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == license["sha256"]
            assert len(data) == license["bytes"]
    frontend = index["frontend_notice"]
    assert (output / frontend["path"]).read_bytes() == collector.FRONTEND_NOTICE.read_bytes()
    assert report["index_sha256"] == hashlib.sha256(index_path.read_bytes()).hexdigest()
    assert str(environment[0]) not in index_path.read_text()


def test_arbitrary_frontend_notice_is_rejected(collector, environment, tmp_path):
    # Tests pass the exact injected product path; an arbitrary similarly named
    # source is rejected even when its bytes happen to be identical.
    fake = tmp_path / "THIRD_PARTY_NOTICES.txt"
    fake.write_bytes(collector.FRONTEND_NOTICE.read_bytes())
    with pytest.raises(collector.LicenseCollectionError, match="frontend_notice_not_exact"):
        collector.collect_licenses(environment[0], tmp_path / "out", fake)


def test_default_notice_uses_only_exact_product_path(collector, environment, tmp_path):
    report = collector.collect_licenses(environment[0], tmp_path / "out")
    assert report["distribution_count"] == 1


def test_uv_installation_bookkeeping_is_not_read_or_archived(collector, environment, tmp_path):
    dist = next(environment[1].glob("*.dist-info"))
    (dist / "uv_cache.json").write_bytes(b"synthetic-cache-path-not-for-license-export")
    output = tmp_path / "out"
    collect(collector, environment, output)
    assert not list(output.rglob("uv_cache.json"))
    assert all(b"synthetic-cache-path" not in p.read_bytes() for p in output.rglob("*") if p.is_file())


def test_uv_bookkeeping_link_still_rejected(collector, environment, tmp_path):
    dist = next(environment[1].glob("*.dist-info"))
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic bookkeeping")
    (dist / "uv_cache.json").symlink_to(outside)
    with pytest.raises(collector.LicenseCollectionError):
        collect(collector, environment, tmp_path / "out")


@pytest.mark.parametrize(
    "name,version,directory",
    [
        ("Original.Component", "1.2.3", "Original.Component-1.2.3.dist-info"),
        ("original_component", "2.0", "original_component-2.0.dist-info"),
    ],
)
def test_duplicate_canonical_name_rejected(collector, environment, tmp_path, name, version, directory):
    add_distribution(environment[1], name, version, directory_name=directory)
    with pytest.raises(collector.LicenseCollectionError, match="duplicate_component_name"):
        collect(collector, environment, tmp_path / "out")


@pytest.mark.parametrize(
    "kind", ["environment", "ancestor", "dist", "license_dir", "license", "frontend", "hardlink"]
)
def test_all_source_links_rejected(collector, environment, tmp_path, kind):
    root, site = environment
    dist = next(site.glob("*.dist-info"))
    selected = {
        "environment": root,
        "ancestor": root / "lib",
        "dist": dist,
        "license_dir": dist / "licenses",
        "license": dist / "licenses/LICENSE",
        "frontend": collector.FRONTEND_NOTICE,
    }.get(kind, dist / "licenses/LICENSE")
    if kind == "hardlink":
        (tmp_path / "second-link").hardlink_to(selected)
    else:
        moved = selected.with_name(selected.name + "-moved")
        selected.rename(moved)
        selected.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(collector.LicenseCollectionError):
        collect(collector, environment, tmp_path / "out")


@pytest.mark.parametrize("filename", ["private.txt", ".env", "api-key.pem", "LICENSE.secret", "token.json"])
@pytest.mark.parametrize("inside_license", [True, False])
def test_unknown_text_or_credentials_not_copied(collector, environment, tmp_path, filename, inside_license):
    dist = next(environment[1].glob("*.dist-info"))
    selected = dist / "licenses" if inside_license else dist
    (selected / filename).write_bytes(b"synthetic forbidden fixture")
    output = tmp_path / "out"
    with pytest.raises(collector.LicenseCollectionError, match="unknown_"):
        collect(collector, environment, output)
    assert not output.exists()


def test_missing_license_fails_before_output_creation(collector, environment, tmp_path):
    dist = next(environment[1].glob("*.dist-info"))
    (dist / "licenses/LICENSE").unlink()
    with pytest.raises(collector.LicenseCollectionError, match="missing_license_text"):
        collect(collector, environment, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"-----BEGIN PRIVATE KEY-----\nsynthetic, not a credential\n",
        b"api_key = synthetic-placeholder\n",
        b"sk-originalsyntheticnotarealsecret000000\n",
    ],
)
def test_obvious_credentials_disguised_as_license_are_not_archived(collector, environment, tmp_path, content):
    license_file = next(environment[1].glob("*.dist-info")) / "licenses/LICENSE"
    license_file.write_bytes(content)
    with pytest.raises(collector.LicenseCollectionError, match="credential_like_text"):
        collect(collector, environment, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "kind", ["repository", "environment", "ancestor", "home", "root", "relative", "nonempty", "linked"]
)
def test_output_scope_and_existing_content_preserved(collector, environment, tmp_path, kind):
    output = {
        "repository": collector.REPOSITORY / "out",
        "environment": environment[0] / "out",
        "ancestor": environment[0].parent,
        "home": Path.home(),
        "root": Path(tmp_path.anchor),
        "relative": Path("out"),
        "nonempty": tmp_path / "not-empty",
        "linked": tmp_path / "linked",
    }[kind]
    marker = tmp_path / "keep.txt"
    marker.write_bytes(b"must be preserved")
    if kind == "nonempty":
        output.mkdir()
        (output / "keep.txt").write_bytes(marker.read_bytes())
    elif kind == "linked":
        output.symlink_to(marker.parent, target_is_directory=True)
    with pytest.raises(collector.LicenseCollectionError):
        collect(collector, environment, output)
    assert marker.read_bytes() == b"must be preserved"
    if kind == "nonempty":
        assert (output / "keep.txt").read_bytes() == marker.read_bytes()


def test_empty_output_allowed_without_overwriting_files(collector, environment, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    report = collect(collector, environment, output)
    assert report["distribution_count"] == 1


@pytest.mark.parametrize(
    "field,value", [("Name", "../private"), ("Version", "1.0/../../private"), ("Name", "mismatched")]
)
def test_metadata_identity_cannot_escape_destination(collector, environment, tmp_path, field, value):
    metadata = next(environment[1].glob("*.dist-info")) / "METADATA"
    text = metadata.read_text()
    original = "original-component" if field == "Name" else "1.2.3"
    metadata.write_text(text.replace(f"{field}: {original}", f"{field}: {value}"))
    with pytest.raises(collector.LicenseCollectionError):
        collect(collector, environment, tmp_path / "out")


@pytest.mark.parametrize(
    "limit",
    ["MAX_TEXT_BYTES", "MAX_METADATA_BYTES", "MAX_TOTAL_BYTES", "MAX_DISTRIBUTIONS", "MAX_LICENSE_FILES"],
)
def test_all_read_and_aggregate_limits_enforced(collector, environment, tmp_path, monkeypatch, limit):
    monkeypatch.setattr(collector, limit, 1)
    if limit == "MAX_DISTRIBUTIONS":
        add_distribution(environment[1], "second", "1.0")
    elif limit == "MAX_LICENSE_FILES":
        (next(environment[1].glob("*.dist-info")) / "licenses/NOTICE").write_text(
            "Original additional notice"
        )
    with pytest.raises(collector.LicenseCollectionError):
        collect(collector, environment, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_isolation_marker_and_ambiguous_site_roots(collector, environment, tmp_path):
    root, _ = environment
    (root / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
    with pytest.raises(collector.LicenseCollectionError, match="environment_not_isolated"):
        collect(collector, environment, tmp_path / "out")
    (root / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    (root / "Lib/site-packages").mkdir(parents=True)
    with pytest.raises(collector.LicenseCollectionError, match="site_packages_not_unique"):
        collect(collector, environment, tmp_path / "out")


def test_no_dependency_import_execution_network_or_secret_reads(
    collector, environment, tmp_path, monkeypatch
):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Forbidden external behavior")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    imports = set(sys.modules)
    report = collect(collector, environment, tmp_path / "out")
    assert report["runtime_sbom"] is False
    assert attempts == []
    assert not {"original_component", "playwright", "rapidocr", "cryptography"} & (set(sys.modules) - imports)
