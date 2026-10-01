"""Offline, bounded license inventory of one explicit isolated build environment.

This is NOT a runtime SBOM or a native/transitive license-closure assertion.
No dependency imports, interpreter execution, downloads, private library scanning,
credential access, or replacement of an existing non-empty output is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import tempfile
from email.parser import BytesParser
from pathlib import Path

REPOSITORY = Path(__file__).absolute().parents[3]
FRONTEND_NOTICE = REPOSITORY / "backend/src/collection_context/interfaces/assets/THIRD_PARTY_NOTICES.txt"
MAX_TEXT_BYTES = 1_048_576
MAX_METADATA_BYTES = 2_097_152
MAX_TOTAL_BYTES = 33_554_432
MAX_DISTRIBUTIONS = 256
MAX_LICENSE_FILES = 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9._+!-]{0,79}\Z")
_LICENSE_FILE = re.compile(
    r"(?:LICENSE|LICENCE|COPYING|NOTICE|AUTHORS|COPYRIGHT)"
    r"(?:\.(?:txt|md|rst|APACHE|BSD|MIT|GPL|LGPL|PSF|ZLIB))?\Z"
    r"|(?:LICENSE-3RD-PARTY\.txt|LICENSE_GEOS|dragon4_LICENSE\.txt|ThirdPartyNotices\.txt)\Z",
    re.IGNORECASE,
)
_METADATA_MEMBERS = frozenset(
    {
        "METADATA",
        "WHEEL",
        "RECORD",
        "INSTALLER",
        "REQUESTED",
        "entry_points.txt",
        "top_level.txt",
        "namespace_packages.txt",
        "direct_url.json",
        # uv's installation/cache bookkeeping is recognized but never read or
        # copied into the license inventory (it is not a license or SBOM).
        "uv_cache.json",
        "uv_build.json",
        "zip-safe",
    }
)
_SBOM_MEMBERS = frozenset(
    {
        "sbom.json",
        "rpds-py.cyclonedx.json",
        "pydantic-core.cyclonedx.json",
        "cryptography-rust.cyclonedx.json",
        "pillow-12.3.0.cdx.json",
    }
)
_CREDENTIAL_TEXT = re.compile(
    rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----"
    rb"|(?:sk-[A-Za-z0-9_-]{20,})"
    rb"|(?:AKIA[A-Z0-9]{16})"
    rb"|(?m:^\s*(?:api[_-]?key|authorization|password|token|secret)\s*[:=])",
    re.IGNORECASE,
)


class LicenseCollectionError(ValueError):
    def __init__(self, code: str):
        super().__init__(f"License inventory stopped: {code}; source contents are not echoed.")
        self.code = code


def _safe_path(path: Path, *, directory: bool | None = None) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise LicenseCollectionError("unsafe_path")
    if path in {Path(path.anchor), Path.home()}:
        raise LicenseCollectionError("broad_path")
    try:
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LicenseCollectionError("linked_path")
        if directory is not None:
            info = path.lstat()
            if directory != stat.S_ISDIR(info.st_mode):
                raise LicenseCollectionError("wrong_path_type")
        return path
    except OSError:
        raise LicenseCollectionError("source_unavailable") from None


def _read(path: Path, limit: int) -> bytes:
    _safe_path(path, directory=False)
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise LicenseCollectionError("safe_read_platform_unverified")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
                raise LicenseCollectionError("unsafe_or_oversized_text")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        _safe_path(path, directory=False)
        current = path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        versions = [tuple(getattr(info, field) for field in fields) for info in (before, after, current)]
        if len(data) != before.st_size or versions[0] != versions[1] or versions[0] != versions[2]:
            raise LicenseCollectionError("source_changed")
        decoded = data.decode("utf-8")
        if "\x00" in decoded:
            raise LicenseCollectionError("non_text_source")
        return data
    except (OSError, UnicodeError):
        raise LicenseCollectionError("source_unavailable_or_non_utf8") from None


def _site_packages(environment: Path) -> Path:
    _safe_path(environment, directory=True)
    config = _read(environment / "pyvenv.cfg", 16_384).decode("utf-8")
    settings = {}
    for line in config.splitlines():
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key in settings:
                raise LicenseCollectionError("ambiguous_environment_marker")
            settings[key] = value
    if settings.get("include-system-site-packages", "").lower() != "false":
        raise LicenseCollectionError("environment_not_isolated")
    candidates = []
    lib = environment / "lib"
    if lib.exists() or lib.is_symlink():
        _safe_path(lib, directory=True)
        for entry in lib.iterdir():
            if re.fullmatch(r"python3\.[0-9]{1,2}", entry.name):
                _safe_path(entry, directory=True)
                site = entry / "site-packages"
                if site.exists() or site.is_symlink():
                    candidates.append(_safe_path(site, directory=True))
    windows_site = environment / "Lib/site-packages"
    if windows_site.exists() or windows_site.is_symlink():
        candidates.append(_safe_path(windows_site, directory=True))
    if len(candidates) != 1:
        raise LicenseCollectionError("site_packages_not_unique")
    return candidates[0]


def _license_text(path: Path) -> bytes:
    data = _read(path, MAX_TEXT_BYTES)
    if _CREDENTIAL_TEXT.search(data):
        raise LicenseCollectionError("credential_like_text")
    return data


def _distributions(site: Path) -> list[Path]:
    result = []
    for count, entry in enumerate(site.iterdir(), start=1):
        if count > 4096:
            raise LicenseCollectionError("site_member_limit")
        if entry.name.endswith(".dist-info"):
            result.append(entry)
            if len(result) > MAX_DISTRIBUTIONS:
                raise LicenseCollectionError("distribution_count_limit")
    return sorted(result)


def _license_members(distribution: Path) -> list[Path]:
    files = []
    for child in distribution.iterdir():
        _safe_path(child)
        if child.name == "licenses":
            _safe_path(child, directory=True)
            pending = [child]
            visited = 0
            while pending:
                directory = pending.pop()
                for entry in directory.iterdir():
                    visited += 1
                    if visited > MAX_LICENSE_FILES:
                        raise LicenseCollectionError("license_member_limit")
                    _safe_path(entry)
                    if entry.is_dir():
                        if not _NAME.fullmatch(entry.name) and entry.name != "_core":
                            raise LicenseCollectionError("unknown_license_member")
                        pending.append(entry)
                    elif _LICENSE_FILE.fullmatch(entry.name):
                        files.append(entry)
                    else:
                        raise LicenseCollectionError("unknown_license_member")
        elif _LICENSE_FILE.fullmatch(child.name):
            files.append(child)
        elif child.name == "sboms":
            # Recognize the installed metadata directory without copying or
            # interpreting it as a verified runtime/native SBOM.
            _safe_path(child, directory=True)
            for entry in child.iterdir():
                _safe_path(entry, directory=False)
                if entry.name not in _SBOM_MEMBERS:
                    raise LicenseCollectionError("unknown_metadata_member")
        elif child.name not in _METADATA_MEMBERS:
            raise LicenseCollectionError("unknown_metadata_member")
    if not files or not any(p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING")) for p in files):
        raise LicenseCollectionError("missing_license_text")
    return sorted(files)


def _output(output: Path, environment: Path) -> Path:
    _safe_path(output)
    _safe_path(output.parent, directory=True)
    for source in (REPOSITORY, environment):
        if output == source or source in output.parents or output in source.parents:
            raise LicenseCollectionError("output_source_overlap")
    if output.exists():
        _safe_path(output, directory=True)
        if any(output.iterdir()):
            raise LicenseCollectionError("output_not_empty")
    return output


def collect_licenses(
    environment_dir: Path,
    output_dir: Path,
    frontend_notice: Path | None = None,
    *,
    extra_notices: dict[tuple[str, str], list[tuple[Path, int, str]]] | None = None,
) -> dict:
    """Copy exact approved source texts into a new/empty external output.

    Returns a report with index_path, counts and unresolved closure. The index
    does not include absolute environment paths or arbitrary METADATA bodies.
    """
    try:
        environment = _safe_path(environment_dir, directory=True)
        if environment == REPOSITORY or REPOSITORY in environment.parents:
            raise LicenseCollectionError("environment_inside_repository")
        output = _output(output_dir, environment)
        if frontend_notice is None:
            frontend_notice = FRONTEND_NOTICE
        if frontend_notice != FRONTEND_NOTICE:
            raise LicenseCollectionError("frontend_notice_not_exact")
        frontend = _license_text(_safe_path(frontend_notice))
        site = _site_packages(environment)
        directories = _distributions(site)
        if not 0 < len(directories) <= MAX_DISTRIBUTIONS:
            raise LicenseCollectionError("distribution_count_limit")
        plan: list[tuple[Path, bytes]] = []
        components = []
        names = set()
        total = len(frontend)
        source_total = total
        for directory in directories:
            _safe_path(directory, directory=True)
            metadata_bytes = _read(directory / "METADATA", MAX_METADATA_BYTES)
            source_total += len(metadata_bytes)
            if source_total > MAX_TOTAL_BYTES:
                raise LicenseCollectionError("inventory_size_limit")
            headers = BytesParser().parsebytes(metadata_bytes, headersonly=True)
            name, version = headers.get("Name"), headers.get("Version")
            if (
                len(headers.get_all("Name", [])) != 1
                or len(headers.get_all("Version", [])) != 1
                or not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or not isinstance(version, str)
                or not _VERSION.fullmatch(version)
            ):
                raise LicenseCollectionError("invalid_component_identity")
            canonical_name = re.sub(r"[-_.]+", "-", name).lower()
            directory_name, directory_version = directory.name[:-10].rsplit("-", 1)
            if (
                re.sub(r"[-_.]+", "-", directory_name).lower() != canonical_name
                or directory_version != version
            ):
                raise LicenseCollectionError("metadata_directory_mismatch")
            if canonical_name in names:
                raise LicenseCollectionError("duplicate_component_name")
            names.add(canonical_name)
            records = []
            try:
                members = _license_members(directory)
            except LicenseCollectionError as error:
                if error.code != "missing_license_text" or not (extra_notices or {}).get(
                    (canonical_name, version)
                ):
                    raise
                members = []
            approved = []
            for path, expected_size, expected_hash in (extra_notices or {}).get(
                (canonical_name, version), []
            ):
                data = _license_text(path)
                if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
                    raise LicenseCollectionError("extra_notice_identity")
                approved.append(
                    (
                        Path("upstream")
                        / ("LICENSE" if len(approved) == 0 else f"NOTICE-{len(approved)}.txt"),
                        data,
                    )
                )
            approved = [(path.relative_to(directory), _license_text(path)) for path in members] + approved
            for relative, data in approved:
                total += len(data)
                source_total += len(data)
                if source_total > MAX_TOTAL_BYTES or len(plan) >= MAX_LICENSE_FILES:
                    raise LicenseCollectionError("inventory_size_limit")
                destination = Path("components") / canonical_name / version / relative
                record = {
                    "path": destination.as_posix(),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                records.append(record)
                plan.append((destination, data))
            components.append(
                {
                    "name": name,
                    "canonical_name": canonical_name,
                    "version": version,
                    "source_distribution": directory.name,
                    "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                    "licenses": records,
                    "bundled_in_application": "not_determined",
                }
            )
        if _distributions(site) != directories:
            raise LicenseCollectionError("environment_inventory_changed")
        frontend_path = Path("frontend/THIRD_PARTY_NOTICES.txt")
        plan.append((frontend_path, frontend))
        unresolved = [
            {
                "component": "CPython",
                "reason": "Interpreter source, bundled license and standard-library notices not collected from dist-info.",
            },
            {
                "component": "Tcl/Tk",
                "reason": "Native GUI libraries and their exact build attribution are outside this environment metadata inventory.",
            },
            {
                "component": "OpenSSL",
                "reason": "Interpreter and wheel embedded crypto/native dependencies require binary-to-source matching.",
            },
            {
                "component": "native_embedded_dependencies",
                "reason": "Native extensions and PyInstaller bootloader embedded/transitive license closure not inspected.",
            },
            {
                "component": "frontend_transitive_dependencies",
                "reason": "Exact product notice preserved; JavaScript bundle-to-lockfile/license coverage not verified.",
            },
            {
                "component": "product_license",
                "reason": "An inventory is not the project's own public license decision.",
            },
        ]
        index = {
            "schema_version": 1,
            "audit_scope": "build_environment_license_inventory",
            "system": platform.system(),
            "architecture": platform.machine(),
            "distribution_count": len(components),
            "license_text_count": len(plan) - 1,
            "license_text_bytes": total,
            "source_text_bytes_read": source_total,
            "components": components,
            "frontend_notice": {
                "path": frontend_path.as_posix(),
                "bytes": len(frontend),
                "sha256": hashlib.sha256(frontend).hexdigest(),
            },
            "module_namespace_coverage": "not_determined",
            "runtime_sbom": False,
            "native_license_closure": "unresolved",
            "unresolved": unresolved,
            "public_release_authorized": False,
        }
        index_bytes = (json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if total + len(index_bytes) > MAX_TOTAL_BYTES:
            raise LicenseCollectionError("inventory_size_limit")
        # All inputs validated before any output is created; originals are verbatim.
        stage = Path(tempfile.mkdtemp(prefix=".license-inventory-", dir=output.parent))
        for relative, data in plan:
            path = stage / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o644)
        with (stage / "license-index.json").open("xb") as stream:
            stream.write(index_bytes)
        _output(output, environment)
        os.rename(stage, output)
        return {
            "audit_scope": index["audit_scope"],
            "index_path": str(output / "license-index.json"),
            "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "distribution_count": len(components),
            "license_text_count": len(plan) - 1,
            "license_text_bytes": total,
            "source_text_bytes_read": source_total,
            "native_license_closure": "unresolved",
            "unresolved": unresolved,
            "runtime_sbom": False,
            "public_release_authorized": False,
        }
    except LicenseCollectionError:
        raise
    except (OSError, ValueError, TypeError):
        raise LicenseCollectionError("inventory_failed") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(collect_licenses(args.environment, args.output), ensure_ascii=False, indent=2))
        return 0
    except LicenseCollectionError as error:
        print(json.dumps({"ok": False, "error": error.code, "native_license_closure": "unresolved"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
