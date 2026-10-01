"""Development package producer guards with original synthetic inputs only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def producer():
    path = Path(__file__).parents[1] / "tools/native_distribution/media_package.py"
    spec = importlib.util.spec_from_file_location("fixed_media_package_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_input_manifest_requires_pair_complete_source_and_both_notices():
    tool = producer()
    assert set(tool.INPUTS) == {
        "artifacts/ffmpeg",
        "artifacts/ffprobe",
        "upstream-source.tar.xz",
        "artifacts/LICENSE.md",
        "artifacts/COPYING.LGPLv2.1",
    }
    assert all(
        type(size) is int and size > 0 and len(digest) == 64 for size, digest, _ in tool.INPUTS.values()
    )


def test_signature_failure_before_output_or_input_packaging(tmp_path, monkeypatch):
    tool = producer()
    helper = tool.source_helper()

    def fail(*args):
        raise ValueError("synthetic signature failure")

    monkeypatch.setattr(helper, "verify", fail)
    monkeypatch.setattr(
        helper, "regular_bytes", lambda *a: pytest.fail("input reads after signature failure")
    )
    monkeypatch.setattr(tool, "source_helper", lambda: helper)
    with pytest.raises(ValueError, match="signature"):
        tool.package(tmp_path / "build", tmp_path / "signature", tmp_path / "key", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def fixture(tmp_path, monkeypatch):
    tool = producer()
    helper = tool.source_helper()
    monkeypatch.setattr(helper, "verify", lambda *args: {"verified": True, "scope": "synthetic_only"})
    monkeypatch.setattr(tool, "source_helper", lambda: helper)
    inputs = {}
    build = tmp_path.resolve() / "build"
    for relative, (_, _, member) in tool.INPUTS.items():
        data = b"original synthetic fixture: " + relative.encode()
        path = build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        inputs[relative] = (len(data), hashlib.sha256(data).hexdigest(), member)
    monkeypatch.setattr(tool, "INPUTS", inputs)
    signature, key = tmp_path.resolve() / "signature", tmp_path.resolve() / "key"
    signature.write_bytes(b"synthetic detached signature")
    key.write_bytes(b"synthetic public key")
    return tool, build, signature, key


@pytest.mark.parametrize("change", ["hash", "missing", "link", "empty"])
def test_changed_input_not_packaged_and_output_not_created(tmp_path, monkeypatch, change):
    tool, build, signature, key = fixture(tmp_path, monkeypatch)
    path = build / "artifacts/ffmpeg"
    if change == "hash":
        path.write_bytes(b"x" * path.stat().st_size)
    elif change == "empty":
        path.write_bytes(b"")
    elif change == "missing":
        path.unlink()
    else:
        original = build / "original"
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises((ValueError, FileNotFoundError)):
        tool.package(build, signature, key, tmp_path.resolve() / "output")
    assert not (tmp_path / "output").exists()


def test_reproducible_complete_package_without_private_report(tmp_path, monkeypatch):
    tool, build, signature, key = fixture(tmp_path, monkeypatch)
    (build / "report.json").write_text("synthetic private report excluded")
    first = tool.package(build, signature, key, tmp_path.resolve() / "first")
    second = tool.package(build, signature, key, tmp_path.resolve() / "second")
    assert first["archive_sha256"] == second["archive_sha256"]
    assert first["product_runtime_installed"] is False and first["public_release_authorized"] is False
    assert first["members"] == 9 and first["contains_complete_upstream_source"] is True
    with zipfile.ZipFile(first["archive"]) as archive:
        assert "source/build-recipe.py" in archive.namelist()
        assert all(not x.is_dir() and x.date_time == (1980, 1, 1, 0, 0, 0) for x in archive.infolist())
        assert "report.json" not in archive.namelist()
        provenance = json.loads(archive.read("PROVENANCE.json"))
        assert provenance["license_closure"] == "unresolved"
        assert provenance["developer_signed"] is False and provenance["notarized"] is False
