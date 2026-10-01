"""Owned runtime wiring only; fake SDK never visits a platform or executes a browser."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_context_addition import addition as addition
from test_context_connection_runner import env as env
from test_context_connection_runner import terminal
from test_context_source_pages import AccountPage, account_payload
from test_context_synchronization import queued
from test_context_synchronization import syncenv as syncenv

from collection_context.application.connection_runner import ConnectionRunner
from collection_context.application.contracts import ContextError
from collection_context.cli import main, source_session
from collection_context.infrastructure import runtime_dependencies as registry
from collection_context.infrastructure.browser import BrowserSession
from collection_context.library.store import LibraryStore
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "原创浏览器运行目录"
    root.mkdir(mode=0o700)
    tools = {}
    for role in ("chromium", "chromium_headless_shell"):
        data = f"original nonexecutable fixture {role}".encode()
        path = root / role
        path.write_bytes(data)
        path.chmod(0o700)
        tools[role] = {
            "relative_path": role,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "version": "original-1",
            "source_url": "https://example.org/original-fixture",
            "license_id": "BSD-3-Clause",
            "playwright": {"package_version": "original-1", "revision": "1"},
        }
    receipt = root / registry.RECEIPT_NAME
    receipt.write_text(json.dumps({"schema_version": 1, "host": registry._host(), "tools": tools}))
    receipt.chmod(0o600)
    monkeypatch.setattr(registry.metadata, "version", lambda _: "original-1")
    return root


@pytest.fixture
def sdk(monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    events = []

    def launched(profile, **options):
        events.append(("launch", profile, options))
        return SimpleNamespace(close=lambda: events.append("context_closed"))

    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launched),
        stop=lambda: events.append("runtime_stopped"),
    )

    def started():
        events.append("sdk_started")
        return runtime

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: SimpleNamespace(start=started))
    return events


@pytest.mark.parametrize("headless", [False, True])
def test_explicit_browser_role_and_absolute_executable(runtime, sdk, tmp_path, headless):
    library = tmp_path.resolve() / "separate-library"
    profile = tmp_path.resolve() / "own-profile"
    with BrowserSession(profile, headless=headless, runtime_dir=runtime, library_dir=library):
        pass
    role = "chromium_headless_shell" if headless else "chromium"
    launch = sdk[1]
    assert launch[0:2] == ("launch", str(profile))
    assert launch[2]["executable_path"] == str(runtime / role)
    assert launch[2]["headless"] is headless and launch[2]["accept_downloads"] is False
    assert sdk[-2:] == ["context_closed", "runtime_stopped"]


@pytest.mark.parametrize("corruption", ["missing", "hash", "binding"])
def test_bad_receipt_starts_no_sdk_and_creates_no_profile(runtime, sdk, tmp_path, monkeypatch, corruption):
    profile = tmp_path.resolve() / "untouched-profile"
    if corruption == "missing":
        (runtime / registry.RECEIPT_NAME).unlink()
    elif corruption == "hash":
        (runtime / "chromium_headless_shell").write_bytes(b"changed")
    else:
        monkeypatch.setattr(registry.metadata, "version", lambda _: "different-package")
    with pytest.raises(ContextError):
        with BrowserSession(profile, runtime_dir=runtime, library_dir=tmp_path.resolve() / "library"):
            pass
    assert sdk == [] and not profile.exists()


def test_runtime_requires_library_binding_and_disjoint_profile(runtime, sdk, tmp_path):
    with pytest.raises(ContextError) as caught:
        with BrowserSession(tmp_path / "profile", runtime_dir=runtime):
            pass
    assert caught.value.code == "invalid_argument"
    with pytest.raises(ContextError) as caught:
        with BrowserSession(runtime / "profile", runtime_dir=runtime, library_dir=tmp_path / "library"):
            pass
    assert caught.value.code == "runtime_directory_overlap" and not sdk
    assert not (runtime / "profile").exists()


@pytest.mark.parametrize("changed_at", [2, 3])
def test_valid_receipt_replacement_does_not_switch_browser(runtime, sdk, tmp_path, monkeypatch, changed_at):
    resolve = registry.RuntimeDependencies.resolve
    calls = []

    def changing(self, role):
        value = resolve(self, role)
        calls.append(role)
        return replace(value, version="replacement") if len(calls) >= changed_at else value

    monkeypatch.setattr(registry.RuntimeDependencies, "resolve", changing)
    with pytest.raises(ContextError) as caught:
        with BrowserSession(tmp_path / "profile", runtime_dir=runtime, library_dir=tmp_path / "library"):
            pass
    assert caught.value.code == "runtime_dependency_integrity"
    assert not any(isinstance(event, tuple) and event[0] == "launch" for event in sdk)
    assert sdk == ([] if changed_at == 2 else ["sdk_started", "runtime_stopped"])


def test_development_browser_keeps_sdk_default_when_no_runtime(sdk, tmp_path):
    with BrowserSession(tmp_path / "profile"):
        pass
    assert "executable_path" not in sdk[1][2]


def test_source_session_passes_owner_fixed_runtime_without_persisting_it(tmp_path, monkeypatch):
    store = LibraryStore.initialize(tmp_path / "library")
    seen = []

    class Browser:
        def __init__(self, profile, **options):
            seen.append((profile, options))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("collection_context.cli.BrowserSession", Browser)
    try:
        before = store.snapshot()
        with source_session(store, tmp_path / "profile", runtime_dir=tmp_path / "runtime"):
            pass
        assert seen[0][1] == {
            "headless": True,
            "runtime_dir": tmp_path / "runtime",
            "library_dir": store.files.root,
        }
        assert store.snapshot() == before
    finally:
        store.close()


def test_read_only_cli_does_not_resolve_runtime_or_create_it(tmp_path, monkeypatch, capsys):
    store = LibraryStore.initialize(tmp_path / "library")
    store.close()
    runtime = tmp_path / "never-created-runtime"
    monkeypatch.setattr(
        registry.RuntimeDependencies, "__init__", lambda *a, **k: pytest.fail("unexpected runtime read")
    )
    assert (
        main(
            [
                "--workspace",
                str(tmp_path / "library"),
                "--runtime-dir",
                str(runtime),
                "search",
                "--query",
                "original",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["error"] is None
    assert not runtime.exists()


def test_worker_addition_inherits_startup_runtime_not_job_payload(tmp_path):
    store = LibraryStore.initialize(tmp_path / "library")
    try:
        runtime = tmp_path / "fixed-runtime"
        sync = SynchronizationWorkflow(store, runtime_dir=runtime)
        worker = BackgroundWorker(ExtractionWorkflow(store, lambda _: pytest.fail("no model")), sync)
        assert worker.addition.runtime_dir == runtime
        admission = AdditionWorkflow(store, runtime_dir=runtime).submit(
            url="https://www.douyin.com/video/81",
            download=False,
            idempotency_key="original-wiring",
            source_confirmed=True,
        )
        assert "runtime" not in json.dumps(admission)
    finally:
        store.close()


def test_addition_and_sync_forward_runtime_to_owned_ingestion(addition, syncenv, tmp_path, monkeypatch):
    runtime = tmp_path / "owner-only-runtime"
    seen = []

    def ingestion(*args, **kwargs):
        seen.append(kwargs["runtime_dir"])
        return IngestionWorkflow(*args, **kwargs)

    monkeypatch.setattr("collection_context.workflows.addition.IngestionWorkflow", ingestion)
    monkeypatch.setattr("collection_context.workflows.synchronization.IngestionWorkflow", ingestion)
    addition[1].runtime_dir = runtime
    job = addition[1].submit(
        url="https://www.douyin.com/video/81",
        download=False,
        idempotency_key="forwarded",
        source_confirmed=True,
    )
    assert addition[1].run(job["id"])["state"] == "succeeded"
    syncenv[1].runtime_dir = runtime
    job, _ = queued(syncenv)
    assert syncenv[1].run(job["id"])["state"] == "succeeded"
    assert seen == [runtime, runtime]
    assert str(runtime) not in json.dumps(addition[0].snapshot())
    assert str(runtime) not in json.dumps(syncenv[0].snapshot())


def test_connection_thread_receives_server_fixed_runtime(env, tmp_path, monkeypatch):
    seen = []
    runtime = tmp_path / "owner-only-runtime"

    class Browser:
        def __init__(self, profile, **options):
            seen.append(options)
            self.headless = options["headless"]
            self.context = SimpleNamespace(
                new_page=lambda: AccountPage([("/aweme/v1/web/user/profile/self/", account_payload())])
            )

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("collection_context.application.connection_runner.BrowserSession", Browser)
    runner = ConnectionRunner(env[0].files.root, tmp_path / "profile", headless=True, runtime_dir=runtime)
    try:
        runner.start(mode="check", source_confirmed=True)
        assert terminal(runner)["state"] == "completed"
        assert seen == [{"headless": True, "runtime_dir": runtime, "library_dir": env[0].files.root}]
        assert "runtime_dir" not in runner.status()
    finally:
        runner.close()
