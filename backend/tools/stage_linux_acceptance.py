"""Whitelist development evidence into a fresh Docker context; never copy the workspace wholesale."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

TOOLS = (
    "query_smoke.py",
    "writer_smoke.py",
    "mcp_smoke.py",
    "web_smoke.py",
    "extraction_smoke.py",
    "media_smoke.py",
)

ASSET_NOTICE = "THIRD_PARTY_NOTICES.txt"


def source_members(backend: Path) -> dict[str, Path]:
    namespace = backend / "src/collection_context"
    members = {str(file.relative_to(backend / "src")): file for file in namespace.rglob("*.py")}
    for file in (namespace / "interfaces/assets").iterdir():
        if file.suffix in {".html", ".css", ".js"} or file.name == ASSET_NOTICE:
            members[str(file.relative_to(backend / "src"))] = file
    return members


def validate_candidate(wheel: Path, backend: Path) -> dict[str, Path]:
    members = source_members(backend)
    metadata = "collection_context-0.2.0.dev0.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            name not in members and not (name.startswith(metadata) and "/" not in name[len(metadata) :])
            for name in names
        ):
            raise ValueError("Candidate contains unexpected package members")
        if {name for name in names if name.startswith("collection_context/")} != set(members):
            raise ValueError("Candidate is missing source files or assets; rebuild before staging")
        for name, source in members.items():
            if source.is_symlink() or not source.is_file() or archive.read(name) != source.read_bytes():
                raise ValueError("Candidate does not match the source under test; rebuild before staging")
    return members


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args(argv)
    wheel = args.wheel.absolute()
    backend = Path(__file__).resolve().parents[1]
    if wheel.is_symlink() or wheel.name != "collection_context-0.2.0.dev0-py3-none-any.whl":
        raise ValueError("Choose an explicit original candidate wheel, not an alias or legacy package")
    members = validate_candidate(wheel, backend)
    args.output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="linux-context-", dir=args.output.absolute()))
    shutil.copy2(wheel, stage / wheel.name)
    shutil.copy2(backend / "tools/linux_acceptance/Dockerfile", stage / "Dockerfile")
    for name, source in members.items():
        target = stage / "src" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (stage / "tests").mkdir()
    for file in sorted((backend / "tests").glob("test_context*.py")):
        if file.is_symlink():
            raise ValueError("Tests must not be aliases outside the source tree")
        shutil.copy2(file, stage / "tests" / file.name)
    (stage / "tools").mkdir()
    for name in TOOLS:
        shutil.copy2(backend / "tools" / name, stage / "tools" / name)
    shutil.copy2(backend / "tools/linux_acceptance/runner.py", stage / "tools/linux_runner.py")
    report = {
        "context": str(stage),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "test_files": sorted(file.name for file in (stage / "tests").glob("*.py")),
        "includes_legacy_or_private_runtime": False,
        "note": "Local development validation, no approval for deployment or public release",
    }
    (stage / "staging-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
