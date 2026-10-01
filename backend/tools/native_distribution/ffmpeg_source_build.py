"""Isolated development build from a signed fixed upstream FFmpeg release.

No downloads, package installation, production runtime changes or publication.
PGPy is required only in a separate developer verification environment, never
in the product. This is not a three-platform release/signing/license audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import warnings
from pathlib import Path, PurePosixPath

VERSION = "9.0.2"
FINGERPRINT = "FCF986EA15E6E293A5644F10B4322F04D67658D8"
SOURCE_URL = "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz"
SOURCE_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"


def regular_bytes(path: Path, maximum: int) -> bytes:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Expected an explicit ordinary source file")
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise ValueError("Source input is not a bounded ordinary file")
    return path.read_bytes()


def verify(archive: Path, signature: Path, key_file: Path) -> dict:
    payload = regular_bytes(archive, 64_000_000)
    if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
        raise ValueError("Source content does not match the fixed signature-verified release")
    from pgpy import PGPKey, PGPSignature

    sig = PGPSignature.from_blob(regular_bytes(signature, 64_000))
    key, _ = PGPKey.from_blob(regular_bytes(key_file, 128_000))
    if not key.is_public or str(key.fingerprint) != FINGERPRINT or sig.signer != FINGERPRINT[-16:]:
        raise ValueError("Upstream signing fingerprint does not match the fixed official release key")
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        verified = bool(key.verify(payload, sig))
    if not verified:
        raise ValueError("Detached source signature verification failed")
    return {
        "source_url": SOURCE_URL,
        "source_bytes": len(payload),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "signer_fingerprint": FINGERPRINT,
        "detached_signature_verified": True,
        "verification_scope": "cryptographic_detached_signature_with_pinned_official_primary_fingerprint",
        "verification_warning_categories": sorted({item.category.__name__ for item in observed}),
        "certificate_revocation_and_expiry_audit": False,
        "post_quantum_signatures_verified": False,
    }


def checked_output(output: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    if (
        not output.is_absolute()
        or ".." in output.parts
        or output.exists()
        or any(p.is_symlink() for p in (output, *output.parents))
        or output in {Path(output.anchor), Path.home(), repository}
        or repository in output.parents
    ):
        raise ValueError("Expected a new specific repository-external build directory")
    output.mkdir(mode=0o700)


def extract(archive: Path, output: Path) -> Path:
    prefix = "ffmpeg-" + VERSION
    count, total, seen = 0, 0, set()
    with tarfile.open(archive, "r:xz") as source:
        for member in source:
            count += 1
            parts = PurePosixPath(member.name).parts
            if (
                count > 30_000
                or not parts
                or parts[0] != prefix
                or any(part in {"/", "..", "."} for part in parts)
                or "\\" in member.name
                or str(PurePosixPath(member.name)) != member.name.rstrip("/")
                or member.name.rstrip("/") in seen
                or not (member.isfile() or member.isdir())
                or member.size < 0
                or member.size > 16_000_000
            ):
                raise ValueError("Source archive contains a disallowed member")
            seen.add(member.name.rstrip("/"))
            total += member.size
            if total > 256_000_000:
                raise ValueError("Source archive expansion limit exceeded")
            target = output.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stream = source.extractfile(member)
            assert stream is not None
            with stream, target.open("xb") as destination:
                copied = 0
                while chunk := stream.read(65_536):
                    copied += len(chunk)
                    if copied > member.size:
                        raise ValueError("Source archive size mismatch")
                    destination.write(chunk)
                if copied != member.size:
                    raise ValueError("Source archive ended before declared member size")
            target.chmod(0o700 if member.mode & 0o111 else 0o600)
    result = output / prefix
    if (result / "VERSION").read_text().strip() != VERSION:
        raise ValueError("Signed source version does not match the fixed build version")
    return result


def build(archive: Path, signature: Path, key_file: Path, output: Path) -> dict:
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise ValueError("This development build recipe is currently scoped to Mac ARM64")
    report = verify(archive, signature, key_file)
    checked_output(output)
    report.update({"state": "running", "product_installed": False, "public_release_authorized": False})
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(output / "fresh-home"),
        "TMPDIR": str(output / "tmp"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    for name in ("fresh-home", "tmp"):
        (output / name).mkdir(mode=0o700)
    sdk = subprocess.check_output(
        ["/usr/bin/xcrun", "--show-sdk-path"], env=environment, text=True, timeout=20
    ).strip()
    command = [
        "./configure",
        "--cc=/usr/bin/clang",
        "--arch=aarch64",
        "--target-os=darwin",
        "--sysroot=" + sdk,
        "--extra-cflags=-mmacosx-version-min=14.0",
        "--extra-ldflags=-mmacosx-version-min=14.0",
        "--disable-autodetect",
        "--disable-gpl",
        "--disable-nonfree",
        "--disable-version3",
        "--disable-shared",
        "--enable-static",
        "--disable-doc",
        "--disable-debug",
        "--disable-ffplay",
        "--disable-network",
        "--disable-avdevice",
        "--disable-asm",
        "--enable-zlib",
    ]
    report["configure"] = command
    try:
        # Preserve exact verified source alongside the candidate; do not build
        # from a later read of a mutable caller file without checking it again.
        payload = regular_bytes(archive, 64_000_000)
        if hashlib.sha256(payload).hexdigest() != report["source_sha256"]:
            raise ValueError("Source input changed after signature verification")
        snapshot = output / "upstream-source.tar.xz"
        with snapshot.open("xb") as destination:
            destination.write(payload)
        snapshot.chmod(0o600)
        source = extract(snapshot, output)
        with (output / "configure.log").open("xb") as log:
            subprocess.run(
                command,
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=180,
            )
        with (output / "make.log").open("xb") as log:
            subprocess.run(
                ["/usr/bin/make", "-j4", "ffmpeg", "ffprobe"],
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=1200,
            )
        artifacts = output / "artifacts"
        artifacts.mkdir(mode=0o700)
        report["tools"] = {}
        for name in ("ffmpeg", "ffprobe"):
            destination = artifacts / name
            shutil.copyfile(source / name, destination)
            destination.chmod(0o700)
            version = subprocess.check_output(
                [str(destination), "-version"], env=environment, text=True, timeout=20
            )
            if not version.startswith(name + " version " + VERSION):
                raise ValueError("Built executable version does not match")
            report["tools"][name] = {
                "bytes": destination.stat().st_size,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "version": version.splitlines()[0],
            }
        for name in ("LICENSE.md", "COPYING.LGPLv2.1"):
            shutil.copyfile(source / name, artifacts / name)
        report["state"] = "built_not_functionally_verified"
        report["source_patches_applied"] = False
        report["license_closure"] = "unresolved_until_source_build_and_binary_audit"
    except BaseException:
        report["state"] = "failed"
        raise
    finally:
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit isolated signed FFmpeg Mac ARM64 development build"
    )
    for name in ("archive", "signature", "key-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    arguments = parser.parse_args()
    print(
        json.dumps(
            build(arguments.archive, arguments.signature, arguments.key_file, arguments.output), indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
