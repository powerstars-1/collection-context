"""Source recipe guards only; no compilation, dependency install, or network."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


def helper():
    source = Path(__file__).parents[1] / "tools/native_distribution/ffmpeg_source_build.py"
    spec = importlib.util.spec_from_file_location("ffmpeg_source_build_test", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["root", "home", "existing", "relative", "parent"])
def test_output_requires_specific_new_external_directory(tmp_path, kind):
    value = {
        "root": Path(tmp_path.anchor),
        "home": Path.home(),
        "existing": tmp_path,
        "relative": Path("relative"),
        "parent": tmp_path / ".." / "candidate",
    }[kind]
    with pytest.raises(ValueError):
        helper().checked_output(value)


def test_output_refuses_link_parent(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target", target_is_directory=True)
    with pytest.raises(ValueError):
        helper().checked_output(tmp_path / "link" / "candidate")
    assert not (tmp_path / "target" / "candidate").exists()


@pytest.mark.parametrize("kind", ["link", "hardlink", "oversize", "empty"])
def test_signed_inputs_are_bounded_ordinary_files(tmp_path, kind):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    if kind == "link":
        target = tmp_path / "linked"
        target.symlink_to(source)
        source = target
    elif kind == "hardlink":
        import os

        os.link(source, tmp_path / "second")
    elif kind == "empty":
        source.write_bytes(b"")
    with pytest.raises(ValueError):
        helper().regular_bytes(source, 4 if kind == "oversize" else 20)


@pytest.mark.parametrize(
    "member", ["../escape", "/escape", "other/file", "ffmpeg-9.0.2/a/../b", "ffmpeg-9.0.2//b"]
)
def test_source_extraction_refuses_wrong_paths(tmp_path, member):
    archive = tmp_path / "source.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        entry = tarfile.TarInfo(member)
        entry.size = 1
        output.addfile(entry, io.BytesIO(b"x"))
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(ValueError):
        helper().extract(archive, stage)
    assert not list(stage.iterdir())


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_source_extraction_refuses_links_and_special_files(tmp_path, kind):
    archive = tmp_path / "source.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        entry = tarfile.TarInfo("ffmpeg-9.0.2/link")
        entry.type, entry.linkname = kind, "outside"
        output.addfile(entry)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(ValueError):
        helper().extract(archive, stage)


def test_source_extraction_preserves_fixed_version_and_executable(tmp_path):
    archive = tmp_path / "source.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        for name, content, mode in [("VERSION", b"9.0.2\n", 0o644), ("configure", b"# inert", 0o755)]:
            entry = tarfile.TarInfo("ffmpeg-9.0.2/" + name)
            entry.size, entry.mode = len(content), mode
            output.addfile(entry, io.BytesIO(content))
    stage = tmp_path / "stage"
    stage.mkdir()
    source = helper().extract(archive, stage)
    assert (source / "VERSION").read_bytes() == b"9.0.2\n"
    assert (source / "configure").stat().st_mode & 0o777 == 0o700


def test_unexpected_source_digest_rejected_before_signature_library_or_key_read(tmp_path):
    archive = tmp_path / "different.tar.xz"
    archive.write_bytes(b"not the fixed release")
    with pytest.raises(ValueError, match="fixed signature-verified release"):
        helper().verify(archive, tmp_path / "absent-signature", tmp_path / "absent-key")


def test_no_compilation_on_unsupported_host(tmp_path, monkeypatch):
    module = helper()
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "verify", lambda *args: pytest.fail("source verification not needed"))
    with pytest.raises(ValueError, match="scoped to Mac ARM64"):
        module.build(tmp_path / "archive", tmp_path / "signature", tmp_path / "key", tmp_path / "output")
    assert not list(tmp_path.iterdir())
