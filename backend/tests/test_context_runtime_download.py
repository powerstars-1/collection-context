"""Offline fixed-catalog downloads; no socket, model, or browser requests."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_download as downloads
from collection_context.infrastructure.runtime_installation import ArtifactPlan, ToolSpec


def plan(data=b"synthetic archive"):
    return ArtifactPlan(
        id="headless-fixture",
        host_system="Darwin",
        host_arch="arm64",
        version="1.0",
        source_url="https://cdn.playwright.dev/fixture.zip",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        archive_type="zip",
        tools=(
            ToolSpec(
                role="chromium_headless_shell",
                relative_path="browser/tool",
                bytes=1,
                sha256="a" * 64,
                version="1.0",
                license_id="LicenseRef-Test",
                playwright_package_version="1.63.0",
                playwright_revision="1243",
            ),
        ),
    )


def test_catalog_identity_required_before_network_or_files(tmp_path, monkeypatch):
    original = plan()
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: pytest.fail("network"))
    with downloads.RuntimeDownloads(tmp_path, catalog={original.id: original}) as source:
        with pytest.raises(ContextError, match="固定产品"):
            source(replace(original, source_url="https://cdn.playwright.dev/different.zip"))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("url", ["http://cdn.playwright.dev/a", "https://example.org/a"])
def test_only_allowlisted_tls_hosts_before_network(tmp_path, monkeypatch, url):
    selected = replace(plan(), source_url=url)
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: pytest.fail("network"))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError):
            source(selected)
    assert not list(tmp_path.iterdir())


def test_private_download_exact_bytes_hash_and_scope_cleanup(tmp_path, monkeypatch):
    data = b"synthetic archive"
    selected = plan(data)
    calls = []

    def get(self, url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(data=data)

    monkeypatch.setattr(downloads.PublicHTTP, "get", get)
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        archive = source(selected)
        assert archive.read_bytes() == data
        assert archive.stat().st_mode & 0o777 == 0o600
        assert archive.parent.stat().st_mode & 0o777 == 0o700
        assert len(calls) == 1
        assert calls[0][1] == {"max_bytes": len(data), "timeout": 120}
    assert not archive.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize("body", [b"changed data", b"synthetic archivf"])
def test_hash_or_length_failure_installs_nothing(tmp_path, monkeypatch, body):
    selected = plan()
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: SimpleNamespace(data=body))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError) as error:
            source(selected)
        assert error.value.code == "runtime_download_integrity"
    assert not list(tmp_path.iterdir())


def test_cancel_before_network_and_after_bounded_download(tmp_path, monkeypatch):
    selected = plan()
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: pytest.fail("network"))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}, stop=stop) as source:
        with pytest.raises(ContextError) as error:
            source(selected)
        assert error.value.code == "runtime_install_cancelled"
    stop.clear()

    def get(*a, **k):
        stop.set()
        return SimpleNamespace(data=b"synthetic archive")

    monkeypatch.setattr(downloads.PublicHTTP, "get", get)
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}, stop=stop) as source:
        with pytest.raises(ContextError) as error:
            source(selected)
        assert error.value.code == "runtime_install_cancelled"
    assert not list(tmp_path.iterdir())


def test_download_failure_fixed_error_no_retry(tmp_path, monkeypatch):
    selected = plan()
    calls = []

    def get(*a, **k):
        calls.append(True)
        raise ContextError("download_failed", "synthetic internal detail")

    monkeypatch.setattr(downloads.PublicHTTP, "get", get)
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError) as error:
            source(selected)
        assert error.value.code == "runtime_download_failed"
        assert "internal" not in str(error.value)
    assert calls == [True] and not list(tmp_path.iterdir())


def test_archive_limit_before_network(tmp_path, monkeypatch):
    selected = replace(plan(), bytes=downloads.MAX_DOWNLOAD_BYTES + 1)
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: pytest.fail("network"))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError) as error:
            source(selected)
        assert error.value.code == "runtime_download_limit"
    assert not list(tmp_path.iterdir())
