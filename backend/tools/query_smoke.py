"""Installed-package smoke from outside the repository; original synthetic evidence only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import collection_context
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    installed = Path(collection_context.__file__).resolve()
    if not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Smoke must use a normally installed distribution, not a repository import")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="query-smoke-", dir=args.output.absolute()))
    store = LibraryStore.initialize(root / "原创合成资料库")
    try:
        item = store.upsert(
            {
                "native_id": "1",
                "title": "原创合成样例：UI 参数练习",
                "body": "此处不是已抓取的真实作品。",
                "author": "测试夹具",
            },
            kind="liked",
            scope_id="s_likes",
        )["item"]
        store.upsert(
            {"native_id": "1", "title": item["title"], "body": item["body"], "author": item["author"]},
            kind="collection",
            scope_id="s_ui",
        )
        store.save_artifact(
            item["id"],
            "screen",
            "原创合成画面文字：Tailwind，画布 1024，禁止额外文字。",
            processor_version="synthetic_fixture_v1",
            expected_content_hash=item["content_hash"],
            coverage={"accuracy": "synthetic_fixture", "not_real_extraction": True},
        )
        FileIndex(store).rebuild()
        workspace = str(store.files.root)
    finally:
        store.close()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    def run(*arguments: str) -> dict:
        process = subprocess.run(
            [sys.executable, "-m", "collection_context.cli", "--workspace", workspace, *arguments],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if process.returncode:
            raise RuntimeError("Installed query command failed")
        output = json.loads(process.stdout)
        if not output["ok"]:
            raise RuntimeError("Installed query returned a business error")
        return output["data"]

    search = run("search", "--query", "UI 1024")
    ref = search["items"][0]["material_ref"]
    first = run("read", "--ref", ref, "--artifact", "screen", "--max-chars", "12")
    rest = run(
        "read",
        "--ref",
        ref,
        "--artifact",
        "screen",
        "--offset",
        str(first["next_offset"]),
        "--version",
        first["version"],
    )
    status = run("status", "--ref", ref)
    assert len(search["items"]) == 1
    assert len(search["items"][0]["relations"]) == 2
    assert "Tailwind" in first["text"] + rest["text"]
    assert status["artifacts"]["audio"]["state"] == "missing"
    assert search["scope_coverage"]["state"] == "unknown"
    print(
        json.dumps(
            {
                "fixture_workspace": workspace,
                "installed_module": str(installed),
                "search": search,
                "screen_text": first["text"] + rest["text"],
                "audio_state": status["artifacts"]["audio"]["state"],
                "result": "passed",
                "real_sync_or_extraction": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
