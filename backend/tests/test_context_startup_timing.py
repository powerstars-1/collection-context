"""Developer diagnostic reports retain measurements, never arguments or exception bodies."""

from __future__ import annotations

import json
import threading

import pytest

from tools.startup_timing_smoke import OperationTimings, import_timings, phase_timings


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
