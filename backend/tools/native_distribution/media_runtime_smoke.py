"""Explicit frozen offline media installation/probe in a fresh owned stage.

Only the product's fixed bundled component is installed. No existing runtime,
library, source login, environment credential, paid call or network download.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def fixture_helpers():
    source = Path(__file__).with_name("runtime_smoke.py")
    if source.is_symlink():
        raise ValueError("Expected the ordinary fixed smoke helper")
    spec = importlib.util.spec_from_file_location("fixed_native_runtime_smoke", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exercise(binary: Path, output: Path) -> dict:
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or any(p.is_symlink() for p in (binary, *binary.parents))
    ):
        raise ValueError("Expected an explicit ordinary frozen executable")
    helper = fixture_helpers()
    stage = helper.checked_stage(output)
    home = stage / "fresh-home"
    home.mkdir(mode=0o700)
    environment = {"PATH": "", "HOME": str(home), "LANG": "C", "LC_ALL": "C"}
    runtime, library = stage / "new-runtime", stage / "absent-library"
    common = ["--workspace", str(library), "--runtime-dir", str(runtime)]
    report = {
        "state": "running",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "fresh_home": True,
        "empty_path": True,
        "host_python_or_node_used_by_product": False,
        "software_download_requests": 0,
        "platform_requests": 0,
        "model_requests": 0,
        "content_quality_verified": False,
        "public_release_authorized": False,
        "verification_scope": "frozen_bundled_media_install_and_original_fixture_only",
    }
    try:
        options = helper.run(binary, [*common, "runtime-options"], stage=stage, environment=environment)
        media = [
            item
            for item in options["artifacts"]
            if item["id"].startswith("ffmpeg-") and item.get("delivery") == "bundled"
        ]
        assert len(media) == 1 and media[0]["state"] == "available"
        assert media[0]["download_bytes"] == 0
        installation = helper.run(
            binary,
            [*common, "install-runtime", "--artifact", media[0]["id"], "--confirm-install"],
            stage=stage,
            environment=environment,
        )
        receipt = runtime / "runtime-dependencies.json"
        before = hashlib.sha256(receipt.read_bytes()).hexdigest()
        probe = helper.run(
            binary,
            [*common, "probe-media-runtime", "--probe-dir", str(stage / "original-probe")],
            stage=stage,
            environment=environment,
        )
        assert probe["state"] == "verified" and probe["functional_verified"] is True
        assert probe["audio_decode_verified"] is True and probe["png_encoder_verified"] is True
        assert probe["audio_segments"] == 2 and probe["sampled_frames"] == 6 and probe["selected_frames"] >= 3
        assert probe["platform_requests"] == probe["model_requests"] == 0
        assert not library.exists() and hashlib.sha256(receipt.read_bytes()).hexdigest() == before
        report.update(
            state="passed",
            installation=installation,
            probe=probe,
            installation_receipt_unchanged_by_probe=True,
            library_created=False,
        )
    except BaseException:
        report["state"] = "failed"
        raise
    finally:
        (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.binary, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
