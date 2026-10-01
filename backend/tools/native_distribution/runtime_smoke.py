"""Frozen browser/driver smoke with empty PATH, fresh HOME, no source login.

The explicit runtime must already have a verified installation receipt. This
tool does not install, change that receipt, download software, read credentials,
or contact Douyin/models. All product work happens in the selected frozen child.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def checked_stage(output: Path) -> Path:
    repository = Path(__file__).resolve().parents[3]
    if (
        not output.is_absolute()
        or ".." in output.parts
        or output.exists()
        or any(p.is_symlink() for p in (output, *output.parents))
        or output == Path(output.anchor)
        or output == Path.home()
        or output == repository
        or repository in output.parents
    ):
        raise ValueError("Expected a new specific library-external output directory")
    output.mkdir(mode=0o700)
    return output


def run(binary: Path, arguments: list[str], *, stage: Path, environment: dict[str, str]) -> dict:
    child = subprocess.run(
        [str(binary), "cli", *arguments],
        cwd=stage,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if child.returncode != 0:
        raise RuntimeError("Frozen runtime command failed; internal child output not echoed")
    payload = json.loads(child.stdout)
    if payload.get("ok") is not True:
        raise RuntimeError("Frozen runtime command did not report successful completion")
    return payload["data"]


def exercise(binary: Path, runtime: Path, output: Path) -> dict:
    if not binary.is_absolute() or not binary.is_file() or binary.is_symlink():
        raise ValueError("Expected an explicit frozen executable")
    if not runtime.is_absolute() or not runtime.is_dir() or runtime.is_symlink():
        raise ValueError("Expected an explicit installed runtime")
    stage = checked_stage(output)
    home = stage / "fresh-home"
    home.mkdir(mode=0o700)
    environment = {"PATH": "", "HOME": str(home), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    receipt = runtime / "runtime-dependencies.json"
    before = hashlib.sha256(receipt.read_bytes()).hexdigest()
    report = {
        "state": "running",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "fresh_home": True,
        "empty_path": True,
        "host_python_or_node_used_by_product": False,
        "platform_requests": 0,
        "model_requests": 0,
        "browser_probe": "not_completed",
        "installation_receipt_unchanged": False,
        "verification_scope": "frozen_headless_local_fixture_only",
    }
    try:
        common = ["--workspace", str(stage / "absent-library"), "--runtime-dir", str(runtime)]
        options = run(binary, [*common, "runtime-options"], stage=stage, environment=environment)
        assert options["artifacts"][0]["state"] == "available"
        probe = run(
            binary,
            [*common, "probe-runtime", "--browser-dir", str(stage / "new-probe-profile")],
            stage=stage,
            environment=environment,
        )
        assert probe["functional_verified"] is True and probe["browser_closed"] is True
        assert probe["verification_scope"] == "owned_headless_local_fixture_only"
        assert probe["sync_verified"] is False and probe["platform_login_verified"] is False
        assert not (stage / "absent-library").exists()
        report["browser_probe"] = "passed"
        report["installation_receipt_unchanged"] = hashlib.sha256(receipt.read_bytes()).hexdigest() == before
        assert report["installation_receipt_unchanged"]
        report["state"] = "passed"
    except BaseException:
        report["state"] = "failed"
        raise
    finally:
        (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit frozen local browser runtime smoke")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = exercise(arguments.binary, arguments.runtime, arguments.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
