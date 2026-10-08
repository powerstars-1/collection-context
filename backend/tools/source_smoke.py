"""Small real public-link verification, not a private-account/five-source acceptance."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import collection_context
from collection_context.application.service import ContextService
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    package = Path(collection_context.__file__).absolute()
    if args.require_installed and "site-packages" not in str(package):
        raise RuntimeError("This verification must use the installed package")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="source-live-", dir=args.output))
    root.chmod(0o700)
    workspace = root / "独立公开样例库"
    LibraryStore.initialize(workspace).close()
    command = [
        sys.executable,
        "-m",
        "collection_context.cli",
        "--workspace",
        str(workspace),
        "add-link",
        "--url",
        args.url,
        "--browser-dir",
        str(root / "自己的浏览器"),
        "--download",
    ]
    env = dict(os.environ)
    if args.require_installed:
        env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    timed_out = False
    try:
        stdout, _ = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            stdout, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout, _ = process.communicate()
    try:
        response = json.loads(stdout)
    except ValueError:
        response = {"ok": False, "error": {"code": "smoke_timeout" if timed_out else "cli_not_json"}}
    report = {
        "package": str(package),
        "library": str(workspace),
        "cli_exit": process.returncode,
        "result": response,
        "private_sources_verified": False,
        "real_model_requests": 0,
        "note": "真实正常公共页面/单作品，独立浏览器；不是私人喜欢、收藏、收藏夹或云模型质量验收。",
    }
    if response.get("ok"):
        data = response["data"]
        store = LibraryStore(workspace)
        try:
            ref = data["material_ref"]
            item = store.get(ref)
            report["native_id"] = item["native_id"]
            report["title"] = item["title"]
            report["original_readable"] = bool(ContextService(store).read(ref)["text"])
            report["searchable"] = bool(ContextService(store).search(item["title"][:12])["items"])
            report["unknown_action_time_preserved"] = all(
                r["action_at"] is None for r in item["relations"].values()
            )
            if data.get("input_id"):
                prepared = PreparedInputs(store).load(data["input_id"])
                report["prepared"] = {
                    "originals": len(prepared["originals"]),
                    "audio_segments": len(prepared["audio"]),
                    "frames": len(prepared["frames"]),
                    "coverage": prepared["coverage"],
                }
            report["jobs_created"] = len(store.snapshot()["jobs"])
        finally:
            store.close()
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **report}, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") and response["data"].get("download") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
