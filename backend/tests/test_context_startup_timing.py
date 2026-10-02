"""Developer diagnostic reports retain measurements, never arguments or exception bodies."""

from __future__ import annotations

import io
import json
import subprocess
import threading

import pytest

from tools.startup_timing_smoke import (
    CHILD_TIMING,
    MAX_DIAGNOSTIC_BYTES,
    DiagnosticCapture,
    OperationTimings,
    https_probe,
    import_timings,
    phase_timings,
    unfinished_phase,
)


def test_import_rows_ignore_unstructured_private_text_and_sort_actual_self_cost():
    body = (
        "private-secret-fixture\n"
        "import time: self [us] | cumulative | imported package\n"
        "import time: 5 | 30 | module.child\n"
        "import time: 15 | 20 | parent\n"
        "import time: 900 | 950 | exception includes private-secret-fixture\n"
    )
    result = import_timings(body)
    assert result == [
        {"module": "parent", "self_us": 15, "cumulative_us": 20},
        {"module": "module.child", "self_us": 5, "cumulative_us": 30},
    ]
    assert "private-secret" not in json.dumps(result)


def test_import_report_is_bounded():
    result = import_timings("\n".join(f"import time: {i} | {i} | original_{i}" for i in range(100)))
    assert len(result) == 30
    assert result[0]["self_us"] == 99


def test_startup_phases_accept_only_static_labels_and_nonsecret_numeric_fields():
    valid = {"phase": "application_create", "seconds": 1.2, "failed": False}
    invalid = [
        {**valid, "phase": "private-secret"},
        {**valid, "secret": "private-secret"},
        {**valid, "seconds": "private-secret"},
        {**valid, "seconds": -1},
        {**valid, "seconds": float("inf")},
        {**valid, "seconds": float("nan")},
        {**valid, "seconds": True},
        {**valid, "failed": "private-secret"},
        None,
        [],
    ]
    body = "\n".join("context-startup:" + json.dumps(value) for value in [valid, *invalid])
    result = phase_timings(body + "\ncontext-startup:invalid-json")
    assert result == [valid]
    assert "private-secret" not in json.dumps(result)


def test_call_timings_preserve_result_and_exception_without_recording_inputs():
    timer = OperationTimings()
    secret = "original-secret-only-in-call"

    def call(value, *, fails=False):
        if fails:
            raise ValueError(value)
        return value

    wrapper = timer.wrapper("controlled_call", call)
    assert wrapper(secret) == secret
    with pytest.raises(ValueError, match=secret):
        wrapper(secret, fails=True)
    result = timer.report()
    counts = result["operations"]["controlled_call.foreground"]
    assert counts["calls"] == 2 and counts["failed"] == 1
    assert counts["seconds"] >= counts["max"] >= 0
    assert result["inclusive_durations_overlap"]
    assert secret not in json.dumps(result)


def test_background_metrics_are_separate_and_thread_name_is_not_logged():
    timer = OperationTimings()
    call = timer.wrapper("controlled_call", lambda: None)
    call()
    thread = threading.Thread(target=call, name="collection-context-owned-execution")
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    other = threading.Thread(target=call, name="private-name-never-log")
    other.start()
    other.join(timeout=2)
    assert not other.is_alive()
    result = timer.report()
    assert result["operations"]["controlled_call.worker"]["calls"] == 1
    assert result["operations"]["controlled_call.foreground"]["calls"] == 2
    assert "private-name" not in json.dumps(result)


def test_instrumentation_restores_product_methods_after_failure():
    from collection_context.infrastructure.files import SafeFiles

    original = SafeFiles.read
    with pytest.raises(RuntimeError):
        with OperationTimings().measure():
            assert SafeFiles.read is not original
            raise RuntimeError("controlled fixture failure")
    assert SafeFiles.read is original


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("private-secret", "not_observed"),
        ("context-startup-begin:server_module_import", "server_module_import"),
        ("context-startup-begin:application_create\ncontext-startup-begin:private-secret", "application_create"),
        (
            'context-startup-begin:workspace_open\ncontext-startup:{"phase":"workspace_open","seconds":1,"failed":false}',
            "no_active_instrumented_phase",
        ),
        (
            'context-startup-begin:workspace_open\ncontext-startup:{"phase":"application_create","seconds":1,"failed":false}',
            "workspace_open",
        ),
        (
            'context-startup-begin:workspace_open\ncontext-startup:{"phase":"workspace_open","seconds":"private-secret","failed":false}',
            "workspace_open",
        ),
    ],
)
def test_unfinished_phase_uses_only_valid_matching_static_markers(body, expected):
    assert unfinished_phase(body) == expected


@pytest.mark.parametrize("extra", [0, 1, 2048])
def test_capture_keeps_bounded_memory_and_original_owner_drains_closes(extra):
    from tools.remote_https_smoke import StartupLogs

    body = b"x" * (MAX_DIAGNOSTIC_BYTES + extra)
    stream = io.BufferedReader(io.BytesIO(body))
    capture = DiagnosticCapture()
    logs = StartupLogs(capture.observe(stream))
    logs.finish()
    assert stream.closed
    assert bytes(capture.body) == body[:MAX_DIAGNOSTIC_BYTES]
    assert capture.clipped is bool(extra)
    assert logs.snapshot()["stderr_over_limit"] is bool(extra)
    assert logs.snapshot()["stderr_inspected_bytes"] == min(len(body), MAX_DIAGNOSTIC_BYTES)


def test_observer_returns_overflow_to_the_original_pipe_consumer():
    capture = DiagnosticCapture()
    stream = io.BufferedReader(io.BytesIO(b"x" * MAX_DIAGNOSTIC_BYTES + b"overflow"))
    observed = capture.observe(stream)
    result = bytearray()
    while chunk := observed.read1(1024):
        result.extend(chunk)
    assert result.endswith(b"overflow") and len(result) == MAX_DIAGNOSTIC_BYTES + 8
    assert len(capture.body) == MAX_DIAGNOSTIC_BYTES and capture.clipped
    assert not stream.closed  # Only the drain owner closes; observation does not.
    observed.close()
    assert stream.closed


@pytest.mark.parametrize("failure", [None, "startup", "generic", "interrupt"])
def test_probe_preserves_pipe_and_startup_evidence_restores_all_patches(tmp_path, monkeypatch, failure):
    from tools import remote_https_smoke

    processes = []

    def process(args, *extra, **kwargs):
        processes.append((args, extra, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", process)
    original_logs = remote_https_smoke.StartupLogs
    observation = {"state": "deadline", "budget_ms": 8000, "attempts": 1}
    marker = b"context-startup-begin:application_create\nprivate-secret-fixture\n"

    def smoke():
        # The certificate and child process must still share the original factory.
        subprocess.Popen(["openssl", "req"], stderr=subprocess.DEVNULL)
        subprocess.Popen(
            ["python", "-m", "collection_context.interfaces.server", "--remote"],
            env={"PATH": "controlled"}, stderr=subprocess.PIPE,
        )
        logs = remote_https_smoke.StartupLogs(io.BufferedReader(io.BytesIO(marker)))
        logs.finish()
        if failure == "startup":
            raise remote_https_smoke.StartupFailure("product_https_server_not_ready", observation)
        if failure == "generic":
            raise ValueError("private-secret-fixture")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        return {"result": "passed", "startup": observation}

    monkeypatch.setattr(remote_https_smoke, "run_smoke", smoke)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            https_probe(tmp_path)
    else:
        result = https_probe(tmp_path)
        assert result["state"] == ("passed" if failure is None else "failed")
        assert result["startup_observation"] == (None if failure == "generic" else observation)
        assert result["unfinished_instrumented_phase"] == "application_create"
        assert result["original_ready_limit_seconds"] == 8
        assert result["import_profiling_can_change_stderr_classification"] is True
        assert "private-secret" not in json.dumps(result)
    assert subprocess.Popen is process
    assert remote_https_smoke.StartupLogs is original_logs
    assert processes[0][0] == ["openssl", "req"]
    args, _, kwargs = processes[1]
    assert args == ["python", "-c", CHILD_TIMING, "--remote"]
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs["env"] == {"PATH": "controlled", "PYTHONPROFILEIMPORTTIME": "1"}
    assert not list(tmp_path.iterdir())  # No raw stderr or credential file.
