"""Create a deterministic local media component with complete upstream source.

Only exact independently verified development binaries/source/notices are
accepted. No arbitrary report, private directory, credentials, upload or install.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import stat
import zipfile
from pathlib import Path

FILENAME = "ffmpeg-macos-arm64-9.0.2-development-1.zip"
INPUTS = {
    "artifacts/ffmpeg": (
        20_971_320,
        "fc62a6701c1645ad79a1618d872e078c680c0ad8a914ae64277d54cac0acb6eb",
        "bin/ffmpeg",
    ),
    "artifacts/ffprobe": (
        20_762_248,
        "8f7d44dec8f994142dc078e91b04240fd41eb84a179f872329c8e7c789c0cc7b",
        "bin/ffprobe",
    ),
    "upstream-source.tar.xz": (
        12_040_788,
        "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e",
        "source/ffmpeg-9.0.2.tar.xz",
    ),
    "artifacts/LICENSE.md": (
        4346,
        "2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af",
        "LICENSE.md",
    ),
    "artifacts/COPYING.LGPLv2.1": (
        26_517,
        "246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30",
        "COPYING.LGPLv2.1",
    ),
}


def source_helper():
    path = Path(__file__).with_name("ffmpeg_source_build.py")
    if path.is_symlink():
        raise ValueError("Expected an ordinary fixed source build helper")
    spec = importlib.util.spec_from_file_location("fixed_media_source_helper", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(build: Path, signature: Path, key_file: Path, output: Path) -> dict:
    helper = source_helper()
    source = build / "upstream-source.tar.xz"
    verification = helper.verify(source, signature, key_file)
    entries = {}
    for relative, (size, digest, name) in INPUTS.items():
        data = helper.regular_bytes(build / relative, size)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Media package input differs from the fixed verified candidate")
        entries[name] = data
    entries["source/ffmpeg-9.0.2.tar.xz.asc"] = helper.regular_bytes(signature, 64_000)
    entries["source/ffmpeg-devel.asc"] = helper.regular_bytes(key_file, 128_000)
    entries["source/build-recipe.py"] = helper.regular_bytes(
        Path(__file__).with_name("ffmpeg_source_build.py").absolute(), 64_000
    )
    entries["PROVENANCE.json"] = json.dumps(
        {
            "version": "9.0.2",
            "host": "Darwin",
            "architecture": "arm64",
            "minimum_target_macos": "14.0",
            "compiler": "Apple clang 21.0.0",
            "sdk": "26.5",
            "source_patches_applied": False,
            "source_verification": verification,
            "delivery": "bundled_component_not_network_download",
            "upstream_url_is_source_not_binary_download": True,
            "license_closure": "unresolved",
            "developer_signed": False,
            "notarized": False,
            "compiled_features": {
                "gpl": False,
                "nonfree": False,
                "version3": False,
                "network": False,
                "system_zlib": True,
                "png_encoder": True,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    helper.checked_output(output)
    archive = output / FILENAME
    with zipfile.ZipFile(
        archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=False
    ) as target:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if name.startswith("bin/") else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, entries[name], compresslevel=9)
    archive.chmod(0o600)
    report = {
        "archive": str(archive),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "members": len(entries),
        "payload_bytes": sum(map(len, entries.values())),
        "contains_complete_upstream_source": True,
        "product_runtime_installed": False,
        "public_release_authorized": False,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build", "signature", "key-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.build, args.signature, args.key_file, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
