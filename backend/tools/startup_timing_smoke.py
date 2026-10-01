"""Owned offline startup/worker diagnostics; instrumentation is not a performance signoff.

Runs the unchanged HTTPS smoke and unchanged memory-vault integration assertion.
Only static operation labels, times, counts and module import names are recorded.
No private libraries, native Keychain values, model calls or source connections.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from collection_context.infrastructure.files import SafeFiles
from collection_context.library.store import LibraryStore

CHILD_TIMING = """
import functools, json, sys, time
started = time.perf_counter()
import collection_context.interfaces.server as server
print('context-startup:' + json.dumps({'phase': 'server_module_import',
    'seconds': time.perf_counter() - started, 'failed': False}), file=sys.stderr, flush=True)
def measured(label, function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        begin = time.perf_counter()
        failed = False
        try:
            return function(*args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            print('context-startup:' + json.dumps({'phase': label,
                'seconds': time.perf_counter() - begin, 'failed': failed}),
                file=sys.stderr, flush=True)
    return call
server.LibraryStore = measured('workspace_open', server.LibraryStore)
server.create_app = measured('application_create', server.create_app)
sys.exit(server.main(sys.argv[1:]))
"""


def phase_timings(body: str) -> list[dict]:
    rows = []
    for line in body.splitlines():
        if not line.startswith("context-startup:"):
            continue
        try:
            value = json.loads(line.removeprefix("context-startup:"))
            if (
                isinstance(value, dict)
                and set(value) == {"phase", "seconds", "failed"}
                and value["phase"] in {"server_module_import", "workspace_open", "application_create"}
                and type(value["seconds"]) in {int, float}
                and 0 <= value["seconds"] <= 600
                and type(value["failed"]) is bool
            ):
                rows.append(value)
        except (ValueError, TypeError):
            continue
    return rows[:3]


class OperationTimings:
    def __init__(self):
        self.values: dict[str, dict] = {}
        self.lock = threading.Lock()

    def wrapper(self, label, function):
        @functools.wraps(function)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            failed = False
            try:
                return function(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                elapsed = time.perf_counter() - started
                worker = threading.current_thread().name == "collection-context-owned-execution"
                key = label + (".worker" if worker else ".foreground")
                with self.lock:
                    value = self.values.setdefault(key, {"calls": 0, "failed": 0, "seconds": 0, "max": 0})
                    value["calls"] += 1
                    value["failed"] += int(failed)
                    value["seconds"] += elapsed
                    value["max"] = max(value["max"], elapsed)

        return measured

    @contextmanager
    def measure(self):
        targets = (
            (SafeFiles, "read", "file_read"),
            (SafeFiles, "write", "file_write"),
            (SafeFiles, "_parent", "parent_validation"),
            (LibraryStore, "snapshot", "library_snapshot"),
            (os, "fsync", "fsync"),
        )
        originals = [(owner, name, getattr(owner, name)) for owner, name, _ in targets]
        try:
            for owner, name, label in targets:
                setattr(owner, name, self.wrapper(label, getattr(owner, name)))
            yield
        finally:
            for owner, name, original in originals:
                setattr(owner, name, original)

    def report(self):
        return {
            "inclusive_durations_overlap": True,
            "operations": {
                key: {
                    name: round(value, 6) if isinstance(value, float) else value
                    for name, value in values.items()
                }
                for key, values in sorted(self.values.items())
            },
        }


def import_timings(body: str) -> list[dict]:
    rows = []
    for line in body.splitlines():
        match = re.fullmatch(r"import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([A-Za-z_][\w.]*)", line)
        if match:
            rows.append({"module": match[3], "self_us": int(match[1]), "cumulative_us": int(match[2])})
    return sorted(rows, key=lambda value: value["self_us"], reverse=True)[:30]


def https_probe(stage: Path) -> dict:
    from tools import remote_https_smoke

    original = subprocess.Popen
    timings = OperationTimings()
    with tempfile.TemporaryFile(dir=stage) as stderr:

        def traced_process(args, *extra, **kwargs):
            if "collection_context.interfaces.server" in args:
                args = [args[0], "-c", CHILD_TIMING, *args[3:]]
                kwargs["env"] = {**kwargs["env"], "PYTHONPROFILEIMPORTTIME": "1"}
                kwargs["stderr"] = stderr
            return original(args, *extra, **kwargs)

        started = time.perf_counter()
        subprocess.Popen = traced_process
        try:
            with timings.measure():
                result = remote_https_smoke.run_smoke()
            state = result["result"]
        except Exception:
            state = "failed"
        finally:
            subprocess.Popen = original
        elapsed = time.perf_counter() - started
        stderr.seek(0)
        body = stderr.read(2_000_001)
    return {
        "state": state,
        "elapsed_seconds": round(elapsed, 6),
        "original_ready_limit_seconds": 8,
        "import_diagnostics_truncated": len(body) > 2_000_000,
        "child_imports": import_timings(body[:2_000_000].decode("utf-8", errors="replace")),
        "child_startup_phases": phase_timings(body[:2_000_000].decode("utf-8", errors="replace")),
        **timings.report(),
    }


def worker_probe(stage: Path) -> dict:
    import pytest

    tests = Path(__file__).resolve().parents[1] / "tests"
    sys.path.insert(0, str(tests))
    fixture_module = importlib.import_module("test_context_system_credential_integration")
    root = stage / "original-worker"
    root.mkdir(mode=0o700)
    patches = pytest.MonkeyPatch()
    timings = OperationTimings()
    started = time.perf_counter()
    fixture = fixture_module.system_env.__wrapped__(root, patches)
    try:
        with timings.measure():
            environment = next(fixture)
            fixture_module.test_authorized_execution_reopens_same_system_backend_and_pinned_refs(
                environment, patches
            )
        state = "passed"
    except (Exception, pytest.fail.Exception):
        state = "failed"
    finally:
        fixture.close()
        patches.undo()
    return {
        "state": state,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "original_worker_limit_seconds": 3,
        "credential_backend": "process_memory_fixture_not_native_Keychain",
        **timings.report(),
    }


def main() -> int:
    # Direct script invocation must locate only these repository-owned test helpers.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage = args.output
    if (
        not stage.is_absolute()
        or stage.exists()
        or any(parent.is_symlink() for parent in stage.parents)
        or not stage.parent.is_dir()
    ):
        raise ValueError("Expected a new explicit ordinary output directory")
    stage.mkdir(mode=0o700)
    report = {
        "scope": "original_offline_startup_and_worker_only",
        "instrumentation_changes_timing": True,
        "root_cause_proven": False,
        "platform_requests": 0,
        "model_requests": 0,
        "native_credential_access": False,
        "https": https_probe(stage),
        "worker": worker_probe(stage),
    }
    (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(stage / "report.json"),
                "https": report["https"]["state"],
                "worker": report["worker"]["state"],
            }
        )
    )
    return 0 if report["https"]["state"] == report["worker"]["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
