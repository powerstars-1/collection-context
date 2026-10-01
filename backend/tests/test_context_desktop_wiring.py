"""Offline actual HTTP lifecycle wiring; no private library/platform/cloud calls."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collection_context.application.contracts import ContextError
from collection_context.application.launcher_capabilities import LauncherCapabilities, launcher_resources
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.launcher import _attach_execution_lifecycle, _watch_stop_request, launch
from collection_context.library.store import LibraryStore
from collection_context.native_desktop import DesktopController, _DesktopWindow


def policy():
    return AccessPolicy(
        "http://127.0.0.1:8787", [Credential.from_token("synthetic", "offline-synthetic-token-" * 3)]
    )


@pytest.mark.parametrize("config,connect", [(False, False), (True, False), (False, True), (True, True)])
def test_configuration_only_http_has_no_execution_runner(tmp_path, config, connect):
    workspace = tmp_path / "原创 合成库"
    LibraryStore.initialize(workspace).close()
    capabilities = LauncherCapabilities(
        workspace,
        allow_model_config=config,
        allow_source_connect=connect,
        credential_dir=tmp_path / "credentials" if config else None,
        browser_dir=tmp_path / "browser" if connect else None,
    )
    with launcher_resources(capabilities) as resources:
        app = create_app(
            workspace,
            policy(),
            model_secrets=resources.model_secrets,
            connection_runner=resources.connection_runner,
        )
        _attach_execution_lifecycle(app, capabilities)
        assert not hasattr(app.state, "execution_runner")
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            assert client.get("/health").status_code == 200
            if resources.connection_runner:
                assert not resources.connection_runner.status()["active"]
        assert not (tmp_path / "browser").exists()
        assert not (workspace / ".context" / "worker.lock").exists()


def test_real_empty_worker_lives_only_inside_http_lifespan(tmp_path):
    workspace = tmp_path / "合成执行库"
    LibraryStore.initialize(workspace).close()
    capabilities = LauncherCapabilities(
        workspace,
        allow_model_config=True,
        allow_model_calls=True,
        credential_dir=tmp_path / "credentials",
    )
    with launcher_resources(capabilities) as resources:
        app = create_app(workspace, policy(), model_secrets=resources.model_secrets)
        _attach_execution_lifecycle(app, capabilities)
        runner = app.state.execution_runner
        assert runner.status()["state"] == "idle"
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            deadline = time.monotonic() + 3
            while runner.status()["state"] == "starting" and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert runner.status()["state"] == "running", runner.status()
            assert runner.status()["active"] is True
            assert client.get("/health").status_code == 200
            assert runner.status()["handled"] == 0
        assert runner.status()["closed"] and not runner.status()["active"]
        assert runner.status()["state"] == "stopped"
        # Empty queue never resolves a credential or creates a source profile.
        assert not list((tmp_path / "credentials").glob("*.json"))


def test_shutdown_drains_worker_before_http_resources_and_does_not_block_event_loop(tmp_path, monkeypatch):
    sequence = []
    closing = threading.Event()
    release = threading.Event()

    class Runner:
        def __init__(self, *args, **kwargs):
            sequence.append("construct")

        def start(self, **kwargs):
            assert kwargs == {
                "allow_model_calls": False,
                "allow_source_sync": True,
                "execution_confirmed": True,
            }
            sequence.append("start")

        def stop(self):
            sequence.append("stop")

        def close(self):
            closing.set()
            assert release.wait(3)
            sequence.append("closed")

    @asynccontextmanager
    async def original(app):
        sequence.append("http-open")
        try:
            yield {"marker": 1}
        finally:
            sequence.append("http-close")

    monkeypatch.setattr("collection_context.launcher.ExecutionRunner", Runner)
    capabilities = LauncherCapabilities(
        tmp_path / "library",
        allow_source_connect=True,
        allow_source_sync=True,
        browser_dir=tmp_path / "browser",
    )
    app = FastAPI(lifespan=original)
    _attach_execution_lifecycle(app, capabilities)

    async def check():
        context = app.router.lifespan_context(app)
        assert await context.__aenter__() == {"marker": 1}
        exit_task = asyncio.create_task(context.__aexit__(None, None, None))
        while not closing.is_set():
            await asyncio.sleep(0.01)
        assert "http-close" not in sequence and not exit_task.done()
        release.set()
        await exit_task

    asyncio.run(check())
    assert sequence == ["construct", "http-open", "start", "stop", "closed", "http-close"]


def test_mismatched_library_is_rejected_before_initialization(tmp_path):
    capabilities = LauncherCapabilities(tmp_path / "other")
    with pytest.raises(ContextError) as caught:
        launch(
            tmp_path / "library",
            port=8787,
            initialize_empty=True,
            no_browser=True,
            output=io.StringIO(),
            capabilities=capabilities,
        )
    assert caught.value.code == "launcher_capabilities_invalid"
    assert not list(tmp_path.iterdir())


def test_desktop_builds_fixed_capabilities_on_service_thread_only(tmp_path, monkeypatch):
    threads = []
    forwarded = []
    flags = (True, True, False, False)
    sentinel = object()

    def factory(workspace, **kwargs):
        threads.append(threading.current_thread())
        assert kwargs == dict(
            allow_model_config=True,
            allow_source_connect=True,
            allow_model_calls=False,
            allow_source_sync=False,
        )
        return sentinel

    def service(workspace, **kwargs):
        forwarded.append(kwargs["capabilities"])
        return 0

    monkeypatch.setattr(
        "collection_context.application.launcher_capabilities.default_desktop_capabilities", factory
    )
    controller = DesktopController(launch_service=service)
    controller.start(tmp_path / "not-created", port=8787, initialize_empty=False, desktop_permissions=flags)
    controller.join(3)
    assert forwarded == [sentinel]
    assert threads and threads[0] is not threading.main_thread()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags", [(1, False, False, False), [True, False, False, False], (True,)])
def test_desktop_rejects_nonliteral_permission_snapshot(tmp_path, flags):
    controller = DesktopController(launch_service=lambda *a, **kw: pytest.fail("started"))
    with pytest.raises(ContextError):
        controller.start(tmp_path, port=8787, initialize_empty=False, desktop_permissions=flags)
    assert not controller.active


def test_gui_permission_change_during_confirmation_invalidates_start(tmp_path):
    window = _DesktopWindow.__new__(_DesktopWindow)
    flags = [True, False, False, False]
    calls = []
    window.controller = SimpleNamespace(active=False, start=lambda *a, **kw: calls.append(kw))
    window.close_requested = False
    window._refresh_dependencies = lambda: {
        "capabilities": {"ready_for_management_page": True},
        "listen": {"available": True},
        "workspace": {"state": "initialized_candidate"},
    }
    window._parameters = lambda: (tmp_path, 8787)
    window._permissions = lambda: tuple(flags)
    window.initialize = SimpleNamespace(get=lambda: False)
    window.status = SimpleNamespace(set=lambda _: None)
    window.root = object()
    window._controls = lambda _: None

    def confirm(*args, **kwargs):
        flags[2] = True
        return True

    window.messagebox = SimpleNamespace(askyesno=confirm)
    window._start()
    assert not calls


def test_desktop_resources_wait_after_bounded_connection_close(tmp_path):
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    workspace = tmp_path / "library"
    LibraryStore.initialize(workspace).close()
    capabilities = LauncherCapabilities(
        workspace, allow_source_connect=True, browser_dir=tmp_path / "browser"
    )

    def operation(*args):
        started.set()
        assert release.wait(3)

    def owner():
        with launcher_resources(capabilities) as resources:
            runner = resources.connection_runner
            assert runner is not None
            runner._operation = operation
            runner.start(mode="login", source_confirmed=True)
            assert started.wait(3)
            runner.close(timeout=0)
            assert runner.status()["active"] and not runner.status()["enabled"]
        exited.set()

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert started.wait(3)
        assert not exited.wait(0.05)
    finally:
        release.set()
        thread.join(3)
    assert exited.is_set() and not thread.is_alive()


def test_stop_bridge_stops_job_admission_before_http_draining():
    server = SimpleNamespace(should_exit=False)
    requested, finished = threading.Event(), threading.Event()
    requested.set()
    observations = []
    execution = SimpleNamespace(stop=lambda: observations.append(server.should_exit))
    _watch_stop_request(server, requested, finished, execution)
    assert observations == [False] and server.should_exit and finished.is_set()
