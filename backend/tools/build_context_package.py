"""Build a development distribution; validate both archives without extracting them.

This is not a complete public source-repository exporter or a secret/license audit.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PROJECT = "collection-context"
NAMESPACE = "collection_context"
ASSETS = frozenset({"app.js", "app.css", "index.html", "THIRD_PARTY_NOTICES.txt"})
WHEEL_METADATA = frozenset({"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt"})
EGG_METADATA = frozenset(
    {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt"}
)
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 4096


class PackageValidationError(ValueError):
    """Only fixed error codes: never include archive names or file contents."""


def _fail(code: str) -> None:
    raise PackageValidationError(code)


def _path(name: str, *, directory: bool = False) -> str:
    if directory and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if (
        not name
        or len(name) > 1024
        or any(c in name for c in "\\:\x00")
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.rstrip(" .") != part for part in parts)
        or any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0]) for part in parts)
        or str(PurePosixPath(name)) != name
    ):
        _fail("noncanonical_member_path")
    return name


def _payload_path(name: str) -> bool:
    parts = name.split("/")
    if parts[0] != NAMESPACE:
        return False
    if parts[1:-1] == ["interfaces", "assets"] and parts[-1] in ASSETS:
        return True
    return (
        len(parts) >= 2
        and all(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", part) for part in parts[1:-1])
        and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*\.py", parts[-1]) is not None
    )


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular_bytes(path: Path, *, maximum: int = MAX_FILE_BYTES) -> bytes:
    # Reject the archive/source itself and linked ancestors, then compare the opened file.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        _fail("unsafe_input_file")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        _fail("unsafe_input_file")
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            _fail("input_file_changed")
        data = stream.read(maximum + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            _fail("input_file_changed")
    if len(data) != before.st_size or _identity(path.lstat()) != _identity(before):
        _fail("input_file_changed")
    return data


def _version(version: str) -> str:
    # This pure-Python project currently uses a numeric release with an optional dev suffix.
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+)?", version):
        _fail("unsupported_project_version")
    return version


def _metadata(data: bytes, version: str) -> None:
    headers = BytesParser().parsebytes(data, headersonly=True)
    if headers.get_all("Name") != [PROJECT] or headers.get_all("Version") != [version]:
        _fail("wrong_project_metadata")


def validate_archive(
    path: Path,
    *,
    kind: str,
    version: str,
    payload_hashes: Mapping[str, str],
    pyproject_sha256: str,
) -> dict[str, object]:
    """Validate an exact wheel/sdist identity, member allowlist and staged source hashes."""
    version = _version(version)
    if kind not in {"wheel", "sdist"}:
        _fail("unsupported_archive_kind")
    if not payload_hashes or f"{NAMESPACE}/__init__.py" not in payload_hashes:
        _fail("invalid_source_manifest")
    if any(
        not _payload_path(_path(name)) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for name, digest in payload_hashes.items()
    ):
        _fail("invalid_source_manifest")
    if not re.fullmatch(r"[a-f0-9]{64}", pyproject_sha256):
        _fail("invalid_source_manifest")
    identity = f"{NAMESPACE}-{version}"
    expected_filename = f"{identity}-py3-none-any.whl" if kind == "wheel" else f"{identity}.tar.gz"
    if path.name != expected_filename:
        _fail("unexpected_archive_filename")
    prefix = f"{identity}.dist-info/" if kind == "wheel" else f"{identity}/"
    if kind == "wheel":
        expected = set(payload_hashes) | {prefix + name for name in WHEEL_METADATA}
        hashes = dict(payload_hashes)
        metadata = {prefix + "METADATA"}
    else:
        expected = {prefix + "src/" + name for name in payload_hashes}
        expected |= {prefix + name for name in ("pyproject.toml", "setup.cfg", "PKG-INFO")}
        expected |= {prefix + f"src/{NAMESPACE}.egg-info/" + name for name in EGG_METADATA}
        hashes = {prefix + "src/" + name: digest for name, digest in payload_hashes.items()}
        hashes[prefix + "pyproject.toml"] = pyproject_sha256
        metadata = {prefix + "PKG-INFO", prefix + f"src/{NAMESPACE}.egg-info/PKG-INFO"}
    directories = {
        str(parent) for name in expected for parent in PurePosixPath(name).parents if str(parent) != "."
    }
    if len({name.casefold() for name in expected | directories}) != len(expected | directories):
        _fail("case_ambiguous_source_manifest")
    data = _regular_bytes(path, maximum=MAX_ARCHIVE_BYTES)
    seen: set[str] = set()
    seen_files: set[str] = set()
    total = 0

    def check(name: str, directory: bool, size: int, read) -> None:
        nonlocal total
        name = _path(name, directory=directory)
        if len(seen) >= MAX_MEMBERS:
            _fail("archive_member_limit")
        if name in seen:
            _fail("duplicate_member")
        seen.add(name)
        if directory:
            if name not in directories or size != 0:
                _fail("unexpected_directory")
            return
        if name not in expected:
            _fail("unexpected_member")
        if not 0 <= size <= MAX_FILE_BYTES or total + size > MAX_ARCHIVE_BYTES:
            _fail("archive_size_limit")
        total += size
        body = read(MAX_FILE_BYTES + 1)
        if len(body) != size:
            _fail("member_size_mismatch")
        if name in hashes and hashlib.sha256(body).hexdigest() != hashes[name]:
            _fail("source_content_mismatch")
        if name in metadata:
            _metadata(body, version)
        seen_files.add(name)

    try:
        if kind == "wheel":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    file_type = stat.S_IFMT(member.external_attr >> 16)
                    directory = member.is_dir()
                    if (
                        member.orig_filename != member.filename
                        or member.flag_bits & 1
                        or (member.external_attr >> 16) & 0o7000
                        or file_type not in {0, stat.S_IFDIR if directory else stat.S_IFREG}
                    ):
                        _fail("unsafe_member_type")
                    with archive.open(member) as stream:
                        check(member.filename, directory, member.file_size, stream.read)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tar_archive:
                for tar_member in tar_archive:
                    if (
                        not (tar_member.isfile() or tar_member.isdir())
                        or tar_member.sparse is not None
                        or tar_member.mode & 0o7000
                    ):
                        _fail("unsafe_member_type")
                    if any(key not in {"mtime", "atime", "ctime"} for key in tar_member.pax_headers):
                        _fail("unsupported_tar_header")
                    tar_stream = tar_archive.extractfile(tar_member) if tar_member.isfile() else None
                    try:
                        check(
                            tar_member.name,
                            tar_member.isdir(),
                            tar_member.size,
                            tar_stream.read if tar_stream is not None else lambda count: b"",
                        )
                    finally:
                        if tar_stream is not None:
                            tar_stream.close()
    except (
        tarfile.TarError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
        OSError,
        ValueError,
        NotImplementedError,
    ) as error:
        if isinstance(error, PackageValidationError):
            raise
        raise PackageValidationError("invalid_archive") from None
    if seen_files != expected:
        _fail("incomplete_archive")
    return {
        "filename": expected_filename,
        "sha256": hashlib.sha256(data).hexdigest(),
        "files": len(seen_files),
        "members": len(seen),
        "uncompressed_bytes": total,
    }


def stage_sources(root: Path, stage: Path) -> tuple[str, dict[str, str], str]:
    """Copy only ordinary product source/assets, never the legacy backend or private data."""
    pyproject = _regular_bytes(root / "distribution/pyproject.toml")
    project = tomllib.loads(pyproject.decode("utf-8"))["project"]
    if project["name"] != PROJECT:
        _fail("wrong_project_configuration")
    version = _version(project["version"])
    (stage / "pyproject.toml").write_bytes(pyproject)
    source = root / "src" / NAMESPACE
    if source.is_symlink() or not source.is_dir():
        _fail("unsafe_source_directory")
    manifest: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(root / "src")
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            _fail("unsafe_source_directory")
        if path.is_dir():
            continue
        name = _path(relative.as_posix())
        if not _payload_path(name):
            _fail("unexpected_source_file")
        body = _regular_bytes(path)
        target = stage / "src" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        manifest[name] = hashlib.sha256(body).hexdigest()
    if f"{NAMESPACE}/__init__.py" not in manifest:
        _fail("invalid_source_manifest")
    return version, manifest, hashlib.sha256(pyproject).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="Use existing build dependencies only; do not create/install a build environment.",
    )
    args = parser.parse_args(argv)
    try:
        output = args.output.absolute()
        output.mkdir(parents=True, exist_ok=True)
        root = Path(__file__).resolve().parents[1]
        stage = Path(tempfile.mkdtemp(prefix="context-package-", dir=output))
        version, hashes, pyproject_hash = stage_sources(root, stage)
        command = [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(stage / "dist")]
        if args.no_isolation:
            command.append("--no-isolation")
        process = subprocess.run(command, cwd=stage, check=False, capture_output=True, text=True)
        if process.returncode:
            # A backend error can contain arbitrary paths/configuration: do not echo its output.
            _fail("build_failed")
        identity = f"{NAMESPACE}-{version}"
        wheels = list((stage / "dist").glob("*.whl"))
        sdists = list((stage / "dist").glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1 or len(list((stage / "dist").iterdir())) != 2:
            _fail("unexpected_build_outputs")
        results = {
            kind: validate_archive(
                path, kind=kind, version=version, payload_hashes=hashes, pyproject_sha256=pyproject_hash
            )
            for kind, path in (("wheel", wheels[0]), ("sdist", sdists[0]))
        }
        print(
            json.dumps(
                {
                    "stage": str(stage),
                    "project": identity,
                    "archives": results,
                    "build_isolation": not args.no_isolation,
                    "note": "Development packages only; not a full public source repository, secret audit, or release approval.",
                },
                indent=2,
            )
        )
        return 0
    except (PackageValidationError, OSError, KeyError, UnicodeError, tomllib.TOMLDecodeError) as error:
        code = str(error) if isinstance(error, PackageValidationError) else "package_build_error"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
