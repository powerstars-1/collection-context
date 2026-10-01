"""Stage a whitelist-only ORIGINAL distribution, separate from the private legacy package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    stage = Path(tempfile.mkdtemp(prefix="context-package-", dir=output))
    shutil.copy2(root / "distribution/pyproject.toml", stage / "pyproject.toml")
    shutil.copytree(
        root / "src/collection_context",
        stage / "src/collection_context",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # A fresh stage ensures setuptools cannot pick up legacy files left in old build/lib.
    process = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(stage / "dist")],
        cwd=stage,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        print(process.stdout)
        print(process.stderr, file=sys.stderr)
        return process.returncode
    wheel = next((stage / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        forbidden = [n for n in names if not (n.startswith("collection_context/") or ".dist-info/" in n)]
        if forbidden:
            raise RuntimeError("Distribution whitelist failed")
    print(
        json.dumps(
            {
                "stage": str(stage),
                "wheel": str(wheel),
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "members": len(names),
                "legacy_or_private_members": forbidden,
                "note": "Development package, not a complete MVP or public release approval.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
