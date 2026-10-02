"""Original synthetic wheel/sdist fixtures; no installs, network or user data."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import stat
import tarfile
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

VERSION = "0.2.0.dev0"
IDENTITY = "collection_context-" + VERSION
PYPROJECT = b'[project]\nname="collection-context"\nversion="0.2.0.dev0"\n'
PAYLOAD = {
    "collection_context/__init__.py": b'"""Original synthetic package."""\n',
    "collection_context/cli.py": b'def main(): return "original fixture"\n',
    "collection_context/interfaces/assets/app.js": b"/* original fixture */\n",
}
METADATA = b"Metadata-Version: 2.4\nName: collection-context\nVersion: 0.2.0.dev0\n\n"


@pytest.fixture
def tool():
    path = Path(__file__).parents[1] / "tools/build_context_package.py"
    spec = importlib.util.spec_from_file_location("package_archive_test_tool", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def members(tool, kind):
    if kind == "wheel":
        result = dict(PAYLOAD)
        result.update(
            {IDENTITY + ".dist-info/" + name: b"original build metadata\n" for name in tool.WHEEL_METADATA}
        )
        result[IDENTITY + ".dist-info/METADATA"] = METADATA
    else:
        prefix = IDENTITY + "/"
        result = {prefix + "src/" + name: data for name, data in PAYLOAD.items()}
        result.update(
            {
                prefix + "pyproject.toml": PYPROJECT,
                prefix + "setup.cfg": b"[egg_info]\n",
                prefix + "PKG-INFO": METADATA,
            }
        )
        result.update(
            {
                prefix + "src/collection_context.egg-info/" + name: b"original build metadata\n"
                for name in tool.EGG_METADATA
            }
        )
        result[prefix + "src/collection_context.egg-info/PKG-INFO"] = METADATA
    return result


def archive_path(directory, kind):
    return directory / (IDENTITY + ("-py3-none-any.whl" if kind == "wheel" else ".tar.gz"))


def write_archive(directory, kind, entries, *, special=None):
    path = archive_path(directory, kind)
    if kind == "wheel":
        with warnings.catch_warnings(), zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as out:
            warnings.simplefilter("ignore", UserWarning)
            for name, body in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                if special:
                    special(info)
                out.writestr(info, body)
    else:
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as out:
            for name, body in entries:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                if special:
                    special(info)
                out.addfile(info, io.BytesIO(body))
    return path


def validate(tool, path, kind, **kwargs):
    options = {
        "kind": kind,
        "version": VERSION,
        "payload_hashes": {name: hashlib.sha256(body).hexdigest() for name, body in PAYLOAD.items()},
        "pyproject_sha256": hashlib.sha256(PYPROJECT).hexdigest(),
    }
    options.update(kwargs)
    return tool.validate_archive(path, **options)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_original_archive_is_exact_without_execution_extraction_or_network(tool, tmp_path, monkeypatch, kind):
    expected = members(tool, kind)
    path = write_archive(tmp_path, kind, expected.items())

    def forbidden(*args, **kwargs):
        pytest.fail("Validation may not execute, extract, or use network")

    monkeypatch.setattr(tool.subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    result = validate(tool, path, kind)
    assert result["files"] == len(expected)
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["uncompressed_bytes"] == sum(map(len, expected.values()))
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "bad",
    [
        "../private",
        "/absolute/private",
        "C:/private",
        "a\\private",
        "a//private",
        "./private",
        "a/./private",
        "a/../private",
        "a/private.",
        "a/private ",
        "a/pri\nvate",
        "CON.py",
        "app/old.py",
        "collection_context/.env.local",
        "collection_context/accounts.json",
        "nested/.dist-info/private",
        "other-1.dist-info/METADATA",
        "collection_context-0.2.0.dev0/../other/private",
    ],
)
def test_member_paths_and_private_or_foreign_namespace_rejected(tool, tmp_path, kind, bad):
    entries = list(members(tool, kind).items()) + [(bad, b"ORIGINAL_PRIVATE_FIXTURE")]
    path = write_archive(tmp_path, kind, entries)
    with pytest.raises(tool.PackageValidationError) as caught:
        validate(tool, path, kind)
    assert bad not in str(caught.value)
    assert "ORIGINAL_PRIVATE_FIXTURE" not in str(caught.value)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "body", "foreign_metadata", "duplicate_name_header"]
)
def test_complete_sources_metadata_identity_and_duplicate_guards(tool, tmp_path, kind, change):
    entries = list(members(tool, kind).items())
    if change == "duplicate":
        entries.append(entries[0])
    elif change == "missing":
        entries.pop(0)
    elif change == "body":
        entries[0] = (entries[0][0], b"changed source")
    else:
        suffix = "/METADATA" if kind == "wheel" else "/PKG-INFO"
        entries = [
            (
                name,
                (
                    body.replace(b"collection-context", b"foreign-product")
                    if change == "foreign_metadata"
                    else b"Name: collection-context\n" + body
                )
                if name.endswith(suffix)
                else body,
            )
            for name, body in entries
        ]
    with pytest.raises(tool.PackageValidationError):
        validate(tool, write_archive(tmp_path, kind, entries), kind)


def test_sdist_pyproject_is_exact_staged_input(tool, tmp_path):
    entries = members(tool, "sdist")
    entries[IDENTITY + "/pyproject.toml"] += b"# unexpected change\n"
    with pytest.raises(tool.PackageValidationError, match="source_content_mismatch"):
        validate(tool, write_archive(tmp_path, "sdist", entries.items()), "sdist")


@pytest.mark.parametrize(
    "kind, mode",
    [
        ("wheel", stat.S_IFLNK),
        ("wheel", stat.S_IFIFO),
        ("wheel", stat.S_IFDIR),
        ("sdist", tarfile.SYMTYPE),
        ("sdist", tarfile.LNKTYPE),
        ("sdist", tarfile.FIFOTYPE),
        ("sdist", tarfile.CHRTYPE),
    ],
)
def test_non_regular_members_rejected(tool, tmp_path, kind, mode):
    def special(info):
        if kind == "wheel":
            info.external_attr = (mode | 0o644) << 16
        else:
            info.type = mode
            info.linkname = "../outside"

    path = write_archive(tmp_path, kind, members(tool, kind).items(), special=special)
    with pytest.raises(tool.PackageValidationError, match="unsafe_member_type"):
        validate(tool, path, kind)


def test_pax_path_override_is_not_an_escape_hatch(tool, tmp_path):
    def special(info):
        info.pax_headers = {"path": "../outside"}

    path = write_archive(tmp_path, "sdist", members(tool, "sdist").items(), special=special)
    with pytest.raises(tool.PackageValidationError, match="unsupported_tar_header"):
        validate(tool, path, "sdist")


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_only_expected_directory_entries_allowed(tool, tmp_path, kind):
    name = "collection_context/" if kind == "wheel" else IDENTITY + "/"
    entries = [(name, b""), *members(tool, kind).items()]

    def special(info):
        if info.filename.endswith("/") if kind == "wheel" else info.name.endswith("/"):
            if kind == "wheel":
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
            else:
                info.type = tarfile.DIRTYPE

    path = write_archive(tmp_path, kind, entries, special=special)
    assert validate(tool, path, kind)["members"] == len(entries)
    entries[0] = ("unrelated/", b"")
    with pytest.raises(tool.PackageValidationError, match="unexpected_directory"):
        validate(tool, write_archive(tmp_path, kind, entries, special=special), kind)


def test_zip_nul_alias_of_allowed_filename_rejected(tool, tmp_path):
    entries = list(members(tool, "wheel").items())
    original = entries[0][0]
    entries[0] = (original + "|extra", entries[0][1])
    path = write_archive(tmp_path, "wheel", entries)
    path.write_bytes(
        path.read_bytes().replace((original + "|extra").encode(), (original + "\x00extra").encode())
    )
    with pytest.raises(tool.PackageValidationError, match="unsafe_member_type"):
        validate(tool, path, "wheel")


def test_source_directory_case_alias_rejected(tool, tmp_path):
    path = archive_path(tmp_path, "wheel")
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in PAYLOAD.items()}
    manifest.update({"collection_context/Foo/x.py": "a" * 64, "collection_context/foo/y.py": "b" * 64})
    with pytest.raises(tool.PackageValidationError, match="case_ambiguous_source_manifest"):
        validate(tool, path, "wheel", payload_hashes=manifest)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("change", ["symbolic", "hard", "corrupt", "wrong_filename"])
def test_archive_itself_must_be_regular_and_exact(tool, tmp_path, kind, change):
    path = write_archive(tmp_path, kind, members(tool, kind).items())
    if change in {"symbolic", "hard"}:
        original = path.rename(tmp_path / "original")
        path.symlink_to(original) if change == "symbolic" else os.link(original, path)
    elif change == "corrupt":
        path.write_bytes(b"not an archive")
    else:
        path = path.rename(tmp_path / "another-project.whl")
    with pytest.raises(tool.PackageValidationError):
        validate(tool, path, kind)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_FILE_BYTES", "MAX_ARCHIVE_BYTES"])
def test_bounded_validation(tool, tmp_path, monkeypatch, kind, limit):
    path = write_archive(tmp_path, kind, members(tool, kind).items())
    monkeypatch.setattr(tool, limit, 1)
    with pytest.raises(tool.PackageValidationError):
        validate(tool, path, kind)


def source_fixture(tmp_path):
    root = tmp_path / "repository/backend"
    (root / "distribution").mkdir(parents=True)
    (root / "distribution/pyproject.toml").write_bytes(PYPROJECT)
    for name, body in PAYLOAD.items():
        target = root / "src" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return root


def test_stage_uses_explicit_product_files_not_legacy_or_caches(tool, tmp_path):
    root = source_fixture(tmp_path)
    (root / ".env.local").write_bytes(b"original private fixture")
    (root / "app").mkdir()
    (root / "app/legacy.py").write_bytes(b"original legacy fixture")
    cache = root / "src/collection_context/__pycache__"
    cache.mkdir()
    (cache / "cli.pyc").write_bytes(b"original cache fixture")
    stage = tmp_path / "stage"
    stage.mkdir()
    version, hashes, config_hash = tool.stage_sources(root, stage)
    assert version == VERSION
    assert hashes == {name: hashlib.sha256(data).hexdigest() for name, data in PAYLOAD.items()}
    assert config_hash == hashlib.sha256(PYPROJECT).hexdigest()
    assert {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()} == {
        "pyproject.toml",
        *("src/" + name for name in PAYLOAD),
    }


@pytest.mark.parametrize("change", ["private", "symbolic", "hard", "linked_directory"])
def test_stage_refuses_unregistered_or_linked_inputs(tool, tmp_path, change):
    root = source_fixture(tmp_path)
    source = root / "src/collection_context"
    if change == "private":
        (source / ".env.local").write_bytes(b"must not be copied")
    elif change == "symbolic":
        (source / "other.py").symlink_to(source / "cli.py")
    elif change == "hard":
        os.link(source / "cli.py", source / "other.py")
    else:
        (source / "linked").symlink_to(source / "interfaces", target_is_directory=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(tool.PackageValidationError):
        tool.stage_sources(root, stage)
    assert not (stage / "src/collection_context/.env.local").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_main_no_isolation_checks_both_and_never_echoes_build_output(
    tool, tmp_path, monkeypatch, capsys, failure
):
    root = source_fixture(tmp_path)
    monkeypatch.setattr(tool, "__file__", str(root / "tools/build_context_package.py"))
    calls = []

    def build(command, **kwargs):
        calls.append(command)
        assert "--no-isolation" in command
        if not failure:
            dist = kwargs["cwd"] / "dist"
            dist.mkdir()
            for kind in ("wheel", "sdist"):
                write_archive(dist, kind, members(tool, kind).items())
        return SimpleNamespace(
            returncode=int(failure), stdout="ORIGINAL_PRIVATE_FIXTURE", stderr="ORIGINAL_PRIVATE_FIXTURE"
        )

    monkeypatch.setattr(tool.subprocess, "run", build)
    result = tool.main(["--output", str(tmp_path / "output"), "--no-isolation"])
    captured = capsys.readouterr()
    assert len(calls) == 1
    assert "ORIGINAL_PRIVATE_FIXTURE" not in captured.out + captured.err
    if failure:
        assert result == 1
        assert json.loads(captured.err) == {"ok": False, "error": "build_failed"}
    else:
        assert result == 0
        report = json.loads(captured.out)
        assert set(report["archives"]) == {"wheel", "sdist"}
        assert report["build_isolation"] is False
        assert "not a full public source repository" in report["note"]
