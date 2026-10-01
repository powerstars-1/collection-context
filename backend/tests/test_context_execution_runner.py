"""Owned execution lifecycle; no real platform, model, old environment or private library."""

from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.execution_runner import ExecutionRunner
from collection_context.infrastructure.ownership import WorkerLease
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.sources.session import source_session
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.jobs import JobManager

FIELDS = {"state", "active", "closed", "allow_model_calls", "allow_source_sync", "handled", "error_code"}
PRIVATE = "never-display-private-data"


def paths(tmp_path):
    return dict(
        workspace=tmp_path / "合成 资料库",
        credential_dir=tmp_path / "独立凭据",
        browser_dir=tmp_path / "独立账号",
        runtime_dir=tmp_path / "独立运行依赖",
    )


def initialize(tmp_path):
    arguments = paths(tmp_path)
    store = LibraryStore.initialize(arguments["workspace"])
    store.close()
    secrets = FileSecrets.initialize(arguments["credential_dir"])
    secrets.close()
    return arguments


def wait_for(runner, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = runner.status()
        if predicate(current):
            return current
        time.sleep(0.001)
    pytest.fail("owned worker did not reach the expected fixed lifecycle state")


def assert_public(status):
    assert set(status) == FIELDS
    assert PRIVATE not in json.dumps(status)


def test_construction_is_readonly_no_handles_directories_secret_reads_or_requests(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("constructor opened a runtime handle")

    monkeypatch.setattr("collection_context.application.execution_runner.LibraryStore", forbidden)
    monkeypatch.setattr("collection_context.application.execution_runner.FileSecrets", forbidden)
    monkeypatch.setattr("collection_context.application.execution_runner.source_session", forbidden)
    runner = ExecutionRunner(**paths(tmp_path), _operation_factory=forbidden)
    assert_public(runner.status())
    assert runner.status()["state"] == "idle" and not runner.status()["active"]
    assert not list(tmp_path.iterdir())
    assert not runner.close()["active"] and not list(tmp_path.iterdir())


def test_constructed_directory_arguments_cannot_be_reassigned(tmp_path):
    runner = ExecutionRunner(**paths(tmp_path))
    for name in paths(tmp_path):
        with pytest.raises(AttributeError):
            setattr(runner, name, tmp_path / "replacement")


@pytest.mark.parametrize("field", ["workspace", "credential_dir", "browser_dir", "runtime_dir"])
def test_all_provided_directories_must_be_absolute(tmp_path, field):
    arguments = paths(tmp_path)
    arguments[field] = Path("relative")
    with pytest.raises(ContextError) as caught:
        ExecutionRunner(**arguments)
    assert caught.value.code == "invalid_argument" and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("workspace", "credential_dir"),
        ("workspace", "browser_dir"),
        ("workspace", "runtime_dir"),
        ("credential_dir", "browser_dir"),
        ("credential_dir", "runtime_dir"),
        ("browser_dir", "runtime_dir"),
    ],
)
@pytest.mark.parametrize("ancestor", [False, True])
def test_directories_are_pairwise_separate_including_ancestors(tmp_path, left, right, ancestor):
    arguments = paths(tmp_path)
    arguments[right] = arguments[left] / "nested" if ancestor else arguments[left]
    with pytest.raises(ContextError) as caught:
        ExecutionRunner(**arguments)
    assert caught.value.code == "execution_directory_overlap" and not list(tmp_path.iterdir())


def test_aliases_cannot_hide_directory_overlap(tmp_path):
    arguments = paths(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(arguments["workspace"], target_is_directory=True)
    arguments["browser_dir"] = alias
    with pytest.raises(ContextError) as caught:
        ExecutionRunner(**arguments)
    assert caught.value.code == "execution_directory_overlap"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"execution_confirmed": True},
        {"allow_model_calls": True},
        {"allow_source_sync": True},
        {"allow_model_calls": True, "allow_source_sync": True},
        {"allow_model_calls": 1, "execution_confirmed": True},
        {"allow_source_sync": "yes", "execution_confirmed": True},
        {"allow_model_calls": True, "execution_confirmed": 1},
    ],
)
def test_no_explicit_boolean_execution_authority_means_zero_handles_or_requests(tmp_path, kwargs):
    calls = []
    runner = ExecutionRunner(**paths(tmp_path), _operation_factory=lambda *args: calls.append(args))
    with pytest.raises(ContextError):
        runner.start(**kwargs)
    assert not calls and not list(tmp_path.iterdir()) and runner.status()["state"] == "idle"


@pytest.mark.parametrize(
    ("missing", "permission", "code"),
    [
        ("credential_dir", "allow_model_calls", "credential_setup_required"),
        ("browser_dir", "allow_source_sync", "source_setup_required"),
    ],
)
def test_missing_fixed_configuration_refuses_before_opening(tmp_path, missing, permission, code):
    arguments = paths(tmp_path)
    arguments[missing] = None
    runner = ExecutionRunner(**arguments)
    with pytest.raises(ContextError) as caught:
        runner.start(**{permission: True, "execution_confirmed": True})
    assert caught.value.code == code and not list(tmp_path.iterdir())


def test_fresh_handles_are_opened_used_closed_on_worker_thread_and_no_keys_read(tmp_path, monkeypatch):
    arguments = initialize(tmp_path)
    observed, closed = [], []
    started = threading.Event()
    actual_store, actual_secrets = LibraryStore, FileSecrets

    class TrackedStore(actual_store):
        def __init__(self, root):
            observed.append(("store-open", threading.current_thread()))
            super().__init__(root)

        def close(self):
            closed.append(("store-close", threading.current_thread()))
            super().close()

    class TrackedSecrets(actual_secrets):
        def __init__(self, root):
            observed.append(("secrets-open", threading.current_thread()))
            super().__init__(root)

        def get(self, ref):
            pytest.fail("empty synthetic operation must not read a credential")

        def close(self):
            closed.append(("secrets-close", threading.current_thread()))
            super().close()

    def factory(store, secrets, source, runtime):
        observed.append(("factory", threading.current_thread(), store, secrets, source, runtime))

        def serve(**kw):
            assert kw["poll_seconds"] == 5 and kw["max_jobs"] == 1 and kw["once"] is False
            assert kw["allow_model_calls"] is True and kw["allow_source_sync"] is False
            assert source is None
            kw["emit"]({"event": "worker_started", "payload": PRIVATE})
            kw["emit"]({"event": "job_finished", "state": "succeeded", "payload": PRIVATE})
            started.set()
            assert kw["stop"].wait(3)
            return {"handled": 1}

        return types.SimpleNamespace(serve=serve)

    monkeypatch.setattr("collection_context.application.execution_runner.LibraryStore", TrackedStore)
    monkeypatch.setattr("collection_context.application.execution_runner.FileSecrets", TrackedSecrets)
    runner = ExecutionRunner(**arguments, _operation_factory=factory)
    handles = []
    try:
        for _ in range(2):
            started.clear()
            runner.start(allow_model_calls=True, execution_confirmed=True)
            assert started.wait(3)
            assert runner._thread.daemon is False
            assert_public(runner.status())
            assert runner.status()["handled"] == 1
            runner.stop()
            wait_for(runner, lambda s: not s["active"])
            current = next(entry for entry in reversed(observed) if entry[0] == "factory")
            handles.append((current[2], current[3]))
            assert current[2].files.fd == current[3].files.fd == -1
        assert handles[0][0] is not handles[1][0] and handles[0][1] is not handles[1][1]
        assert all(entry[1] is not threading.main_thread() for entry in observed + closed)
        assert len({entry[1] for entry in observed + closed}) == 2
    finally:
        runner.close(timeout=3)


def test_source_only_never_opens_secrets_and_does_not_open_browser_until_job(tmp_path, monkeypatch):
    arguments = initialize(tmp_path)
    arguments["credential_dir"] = None

    def forbidden(*args, **kwargs):
        pytest.fail("no queued source job should open a browser or secrets")

    monkeypatch.setattr("collection_context.application.execution_runner.FileSecrets", forbidden)
    monkeypatch.setattr("collection_context.infrastructure.browser.BrowserSession.__enter__", forbidden)
    runner = ExecutionRunner(**arguments)
    try:
        runner.start(allow_source_sync=True, execution_confirmed=True)
        current = wait_for(runner, lambda s: s["state"] == "running")
        assert current["handled"] == 0 and current["allow_source_sync"] and not current["allow_model_calls"]
        assert not arguments["browser_dir"].exists() and not arguments["runtime_dir"].exists()
    finally:
        assert runner.close(timeout=3)["state"] == "stopped"


def test_stop_during_handle_opening_does_not_open_secrets_or_admit_operation(tmp_path, monkeypatch):
    arguments = initialize(tmp_path)
    entered, release = threading.Event(), threading.Event()
    handles = []

    class WaitingStore(LibraryStore):
        def __init__(self, root):
            super().__init__(root)
            handles.append(self)
            entered.set()
            assert release.wait(3)

    def forbidden(*args):
        pytest.fail("stop already requested: no next handle or operation")

    monkeypatch.setattr("collection_context.application.execution_runner.LibraryStore", WaitingStore)
    monkeypatch.setattr("collection_context.application.execution_runner.FileSecrets", forbidden)
    runner = ExecutionRunner(**arguments, _operation_factory=forbidden)
    try:
        runner.start(allow_model_calls=True, execution_confirmed=True)
        assert entered.wait(3)
        assert runner.close(timeout=0)["state"] == "stopping"
        release.set()
        assert runner.close(timeout=3)["state"] == "stopped"
        assert handles[0].files.fd == -1
    finally:
        release.set()
        runner.close(timeout=3)


def test_real_empty_library_holds_worker_lease_and_releases_only_after_stop(tmp_path):
    arguments = initialize(tmp_path)
    first, second = ExecutionRunner(**arguments), ExecutionRunner(**arguments)
    try:
        first.start(allow_model_calls=True, execution_confirmed=True)
        wait_for(first, lambda s: s["state"] == "running")
        with pytest.raises(ContextError) as caught:
            with WorkerLease(arguments["workspace"]):
                pytest.fail("second lease acquired")
        assert caught.value.code == "worker_busy"
        second.start(allow_model_calls=True, execution_confirmed=True)
        failed = wait_for(second, lambda s: not s["active"])
        assert failed["state"] == "failed" and failed["error_code"] == "worker_busy"
        assert first.close(timeout=3)["state"] == "stopped"
        with WorkerLease(arguments["workspace"]):
            pass
        store = LibraryStore(arguments["workspace"])
        try:
            assert not store.snapshot()["jobs"]
        finally:
            store.close()
    finally:
        first.close(timeout=3)
        second.close(timeout=3)


def test_stop_timeout_retains_live_inflight_state_no_second_start_or_next_job(tmp_path):
    arguments = initialize(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def factory(*args):
        def serve(**kw):
            calls.append("first-inflight")
            kw["emit"]({"event": "worker_started"})
            entered.set()
            assert release.wait(3)
            kw["emit"]({"event": "job_finished", "state": "cancelled", "payload": PRIVATE})
            if not kw["stop"].is_set():
                calls.append("next-job")
            return {"handled": 1}

        return types.SimpleNamespace(serve=serve)

    runner = ExecutionRunner(**arguments, _operation_factory=factory)
    try:
        runner.start(allow_model_calls=True, execution_confirmed=True)
        assert entered.wait(3)
        with pytest.raises(ContextError) as busy:
            runner.start(allow_source_sync=True, execution_confirmed=True)
        assert busy.value.code == "execution_busy"
        status = runner.stop()
        assert status["active"] and status["state"] == "stopping" and status["handled"] == 0
        status = runner.close(timeout=0)
        assert status["closed"] and status["active"] and status["state"] == "stopping"
        assert calls == ["first-inflight"]
        release.set()
        status = runner.close(timeout=3)
        assert status["state"] == "stopped" and not status["active"] and status["handled"] == 1
        assert calls == ["first-inflight"]
        with pytest.raises(ContextError) as closed:
            runner.start(allow_model_calls=True, execution_confirmed=True)
        assert closed.value.code == "execution_closed"
    finally:
        release.set()
        runner.close(timeout=3)


@pytest.mark.parametrize("operation", ["stop", "cancel-job"])
def test_real_original_workflow_preserves_inflight_usage_checkpoint_and_leaves_next_queued(
    tmp_path, monkeypatch, operation
):
    from test_context_model_registry import configure, prepared, requests

    arguments = initialize(tmp_path)
    store = LibraryStore(arguments["workspace"])
    secrets = FileSecrets(arguments["credential_dir"])
    try:
        key = secrets.put("synthetic-not-a-provider-key")
        configure(store, key, "vision", "fixture-vision")
        configure(store, key, "summary", "fixture-summary")
        _, identity = prepared(store)
        workflow = ExtractionWorkflow(store, secrets.get)
        first = workflow.submit(identity, idempotency_key="first", max_calls=3)
        second = workflow.submit(identity, idempotency_key="next", max_calls=3)
    finally:
        secrets.close()
        store.close()

    entered, release = threading.Event(), threading.Event()

    def inflight(sent):
        if len(sent) == 1:
            entered.set()
            assert release.wait(3)

    # Original pipeline and durable jobs, but transport is an in-memory fixture.
    sent = requests(monkeypatch, inflight)
    runner = ExecutionRunner(**arguments)
    try:
        runner.start(allow_model_calls=True, execution_confirmed=True)
        assert entered.wait(3)
        if operation == "cancel-job":
            control = LibraryStore(arguments["workspace"])
            try:
                JobManager(control).cancel(first["id"])
            finally:
                control.close()
        assert runner.close(timeout=0)["state"] == "stopping"
        assert runner.status()["active"] and runner.status()["handled"] == 0
        release.set()
        result = runner.close(timeout=3)
        assert result["state"] == "stopped" and result["handled"] == 1 and not result["active"]
        control = LibraryStore(arguments["workspace"])
        try:
            jobs = control.snapshot()["jobs"]
            current, queued = jobs[first["id"]], jobs[second["id"]]
            assert current["state"] == ("cancelled" if operation == "cancel-job" else "succeeded")
            assert len(sent) == (1 if operation == "cancel-job" else 3)
            assert all(
                call["state"] == "completed" and call["usage"] == {"total_tokens": 5}
                for call in current["calls"]
            )
            assert queued["state"] == "queued" and queued["calls"] == []
        finally:
            control.close()
    finally:
        release.set()
        runner.close(timeout=3)


@pytest.mark.parametrize(
    "failure", [RuntimeError(PRIVATE), SystemExit(PRIVATE), ContextError(PRIVATE, PRIVATE)]
)
def test_failure_does_not_cache_payload_exception_or_automatically_restart(tmp_path, failure, capsys):
    arguments = initialize(tmp_path)
    calls = []

    def factory(store, secrets, *args):
        calls.append((store, secrets))
        raise failure

    runner = ExecutionRunner(**arguments, _operation_factory=factory)
    runner.start(allow_model_calls=True, execution_confirmed=True)
    status = wait_for(runner, lambda s: not s["active"])
    assert status["state"] == "failed" and status["error_code"] == "execution_failed"
    assert_public(status)
    assert runner.status() == status and len(calls) == 1
    assert calls[0][0].files.fd == calls[0][1].files.fd == -1
    assert capsys.readouterr() == ("", "")
    runner.close(timeout=3)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, "5"])
def test_close_timeout_must_not_be_ambiguous(tmp_path, timeout):
    runner = ExecutionRunner(**paths(tmp_path))
    with pytest.raises(ContextError) as caught:
        runner.close(timeout=timeout)
    assert caught.value.code == "invalid_argument" and not runner.status()["closed"]


def test_source_session_matches_original_boundary_and_explicit_runtime(tmp_path, monkeypatch):
    arguments = initialize(tmp_path)
    calls = []

    class Browser:
        def __init__(self, profile, **kw):
            calls.append((profile, kw))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

    monkeypatch.setattr("collection_context.sources.session.BrowserSession", Browser)
    monkeypatch.setattr("collection_context.sources.session.DouyinBrowserSource", lambda browser: browser)
    store = LibraryStore(arguments["workspace"])
    try:
        with source_session(
            store, arguments["browser_dir"], headless=True, runtime_dir=arguments["runtime_dir"]
        ):
            pass
        assert calls == [
            (
                arguments["browser_dir"],
                {
                    "headless": True,
                    "runtime_dir": arguments["runtime_dir"],
                    "library_dir": arguments["workspace"],
                },
            ),
            "closed",
        ]
        with pytest.raises(ContextError) as caught:
            with source_session(store, arguments["workspace"] / "inside"):
                pytest.fail("overlap accepted")
        assert caught.value.code == "unsafe_login_profile" and len(calls) == 2
    finally:
        store.close()
