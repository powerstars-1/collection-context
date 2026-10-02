"""Installed-package real process-death checks in new synthetic workspaces only."""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path

import collection_context
from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.ownership import WriterLease
from collection_context.library.store import LEGACY_GUARD, LibraryStore

CHILD = """
import sys
from pathlib import Path
from collection_context.library.store import LibraryStore
store = LibraryStore(Path(sys.argv[1]))
boundary = sys.argv[2]
write = store.files.write
def pause_write(relative, body, *, replace=False):
    write(relative, body, replace=replace)
    match = relative.startswith('.context/提交/c_') if boundary == 'before' else relative == '.context/提交/CURRENT.json'
    if match:
        print('owned-and-paused', flush=True)
        sys.stdin.read(1)
store.files.write = pause_write
store.upsert({'native_id':'1','title':'原创恢复样例','body':'不是真实平台内容'}, kind='saved', scope_id='s_saved')
"""

RESTART = """
import json, sys
from pathlib import Path
from collection_context.library.store import LibraryStore
store = LibraryStore(Path(sys.argv[1]))
try:
    result = store.upsert({'native_id':'1','title':'原创恢复样例','body':'不是真实平台内容'}, kind='saved', scope_id='s_saved')
    print(json.dumps({'created':result['created'], 'items':len(store.snapshot()['items'])}))
finally:
    store.close()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    installed = Path(collection_context.__file__).resolve()
    if not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Use a normally installed package, not repository imports")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="writer-smoke-", dir=args.output.absolute()))
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    observations = []
    for boundary in ("before", "after"):
        store = LibraryStore.initialize(root / f"中文 空格 {boundary}")
        process = None
        try:
            snapshot = store.snapshot()
            guard = store.files.read(LEGACY_GUARD)
            process = subprocess.Popen(
                [sys.executable, "-c", CHILD, str(store.files.root), boundary],
                cwd=root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            assert process.stdout is not None and select.select([process.stdout], [], [], 10)[0]
            assert process.stdout.readline().strip() == "owned-and-paused"
            try:
                store.transact(lambda state: None)
            except ContextError as error:
                assert error.code == "writer_busy"
            else:
                raise AssertionError("Concurrent writer entered while child remained alive")
            process.kill()  # Only this script's fresh owned child process, never a production process.
            exit_code = process.wait(timeout=5)
            assert exit_code < 0
            assert (store.files.root / WriterLease.path).is_file()
            assert store.files.read(LEGACY_GUARD) == guard
            with LibraryStore(store.files.root).files as files:
                assert files.read(LEGACY_GUARD) == guard
            reopened = LibraryStore(store.files.root)
            try:
                visible = reopened.snapshot()
                assert visible == snapshot if boundary == "before" else len(visible["items"]) == 1
                restart = subprocess.run(
                    [sys.executable, "-c", RESTART, str(store.files.root)],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                assert restart.returncode == 0
                result = json.loads(restart.stdout)
                assert result["created"] == (boundary == "before") and result["items"] == 1
                assert len(reopened.snapshot()["items"]) == 1
                observations.append(
                    {
                        "boundary": boundary,
                        "live_writer_rejected": True,
                        "owned_child_exit_code": exit_code,
                        "pre_restart_item_count": len(visible["items"]),
                        "new_process_write_succeeded": True,
                        "item_count": 1,
                        "guard_retained": True,
                    }
                )
            finally:
                reopened.close()
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stdin:
                    process.stdin.close()
            store.close()

    legacy = LibraryStore.initialize(root / "早期协议 合成库")
    try:
        original = legacy.snapshot()
        config = json.loads(legacy.files.read("context-workspace.json"))
        config.pop("writer_protocol")
        legacy.files.write("context-workspace.json", canonical_bytes(config), replace=True)
        legacy.files.unlink(LEGACY_GUARD)  # Convert only a new isolated synthetic fixture.
        upgraded = subprocess.run(
            [
                sys.executable,
                "-m",
                "collection_context.cli",
                "--workspace",
                str(legacy.files.root),
                "upgrade-writer",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert upgraded.returncode == 0 and json.loads(upgraded.stdout)["data"]["upgraded"]
        assert legacy.snapshot() == original
        legacy.transact(lambda state: None)
        explicit_upgrade = {"success": True, "committed_data_unchanged": True, "post_upgrade_write": True}
    finally:
        legacy.close()
    report = {
        "result": "passed",
        "installed_module": str(installed),
        "observations": observations,
        "explicit_legacy_upgrade": explicit_upgrade,
        "private_data_read": False,
        "platform_requests": 0,
        "model_requests": 0,
        "scope": "Current POSIX local filesystem, synthetic fixtures, not full MVP or all crash/disk failures",
    }
    target = root / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
