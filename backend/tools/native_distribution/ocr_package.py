"""Build a deterministic private OCR weight candidate from the fixed installed wheel.

No implicit download, installation, model requests, or public license approval.
Original upstream licenses are explicit inputs checked by fixed identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import stat
import zipfile
from importlib import metadata
from pathlib import Path

from collection_context.infrastructure.runtime_ocr import OCR_MODELS, OCR_VERSIONS

FILENAME = "ocr-ppocrv6-small-rapidocr-3.9.2-development-1.zip"
LICENSES = {
    "RapidOCR-LICENSE": (11_422, "3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333"),
    "PaddleOCR-LICENSE": (11_376, "3840c5c0c61c294264d2dd77b8777be6ddd90121ef4e0e64abcd22edea581d6e"),
}


def read_checked(path: Path, size: int, digest: str) -> bytes:
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("OCR input must be an ordinary absolute file")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
        raise ValueError("OCR input size or type differs from fixed identity")
    body = path.read_bytes()
    if path.stat() != before or len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("OCR input bytes differ from fixed identity")
    return body


def package(notices: Path, output: Path) -> dict:
    output = output.absolute()
    repository = Path(__file__).resolve().parents[3]
    if (
        output.exists()
        or output.is_symlink()
        or ".." in output.parts
        or output == Path(output.anchor)
        or output == Path.home()
        or output == repository
        or repository in output.parents
        or any(p.is_symlink() for p in output.parents)
    ):
        raise ValueError("Output must be a new library-external component directory")
    distribution = metadata.distribution("rapidocr")
    if distribution.version != OCR_VERSIONS["rapidocr"]:
        raise ValueError("OCR package version differs from fixed baseline")
    records = {str(record): record for record in distribution.files or ()}
    entries = {}
    for filename, size, digest in OCR_MODELS.values():
        relative = "rapidocr/models/" + filename
        record = records.get(relative)
        if record is None or record.size != size or record.hash is None or record.hash.mode != "sha256":
            raise ValueError("OCR weight not registered by the fixed wheel")
        encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip("=")
        if record.hash.value != encoded:
            raise ValueError("OCR weight RECORD differs from fixed baseline")
        entries["models/" + filename] = read_checked(Path(distribution.locate_file(record)), size, digest)
    for filename, (size, digest) in LICENSES.items():
        entries["licenses/" + filename] = read_checked(notices.absolute() / filename, size, digest)
    # Preserve the wheel's versioned upstream URLs and hashes, not global config.
    model_index = Path(distribution.locate_file("rapidocr/default_models.yaml"))
    entries["source/default_models.yaml"] = read_checked(
        model_index, 71_521, "db47df9d6b071721f1633667b41cfef1e40b5d7f09be030aaea8be801ca2f2f5"
    )
    entries["PROVENANCE.json"] = json.dumps(
        {
            "upstream": "RapidAI/RapidOCR via official PyPI wheel",
            "rapidocr_version": OCR_VERSIONS["rapidocr"],
            "model_identity": "wheel RECORD and packaged upstream SHA256 agree",
            "upstream_signature_verified": False,
            "weights": OCR_MODELS,
            "required_engines": OCR_VERSIONS,
            "code_license": "Apache-2.0",
            "weight_redistribution_review": "unresolved_not_public_release",
            "license_sources": [
                "https://raw.githubusercontent.com/RapidAI/RapidOCR/v3.9.2/LICENSE",
                "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/LICENSE",
            ],
            "note": "Upstream weight attribution/conversion rights remain a separate public release gate.",
        },
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    ).encode()
    output.mkdir(mode=0o700, parents=True)
    path = output / FILENAME
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, body in sorted(entries.items()):
            member = zipfile.ZipInfo(name, date_time=(2026, 10, 2, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o600) << 16
            member.compress_type = zipfile.ZIP_STORED
            archive.writestr(member, body)
    path.chmod(0o600)
    result = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "payload_bytes": sum(map(len, entries.values())),
        "members": len(entries),
        "all_members_ordinary_non_executable": True,
        "model_requests": 0,
        "public_release_authorized": False,
    }
    (output / "package-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.notices, args.output), indent=2))


if __name__ == "__main__":
    main()
