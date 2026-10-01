"""Original page connection lifecycle and authority; no private accounts or network."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_scheduling import env as env
from test_context_source_pages import AccountPage, account_payload

from collection_context.application.connection_runner import ConnectionRunner, separate_browser
from collection_context.application.contracts import ContextError
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.interfaces.server import main as server_main
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.workflows.connection import ConnectionCatalog, ConnectionWorkflow

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def terminal(runner):
    deadline = time.monotonic() + 3
    while runner.status()["active"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not runner.status()["active"]
    return runner.status()


def test_default_owner_page_does_not_enable_connection(managed):
    assert post(managed, "connection-run", {}).json()["data"]["enabled"] is False
    assert (
        post(managed, "connection-start", {"mode": "login", "source_confirmed": True}).json()["error"]["code"]
        == "connection_disabled"
    )


@pytest.mark.parametrize(
    "extra", [["--allow-source-connect"], ["--browser-dir", "/not-created"], ["--source-connect-headless"]]
)
def test_server_requires_explicit_complete_opt_in(env, extra, capsys):
    assert server_main(["--workspace", str(env[0].files.root), *extra]) == 1
    assert "connection_disabled" in capsys.readouterr().err


def test_profile_must_not_overlap_library_before_any_creation(env, tmp_path):
    with pytest.raises(ContextError) as caught:
        ConnectionRunner(env[0].files.root, env[0].files.root / "browser")
    assert caught.value.code == "login_directory_overlap"
    assert not (env[0].files.root / "browser").exists()
    link = tmp_path / "alias"
    link.symlink_to(env[0].files.root, target_is_directory=True)
    with pytest.raises(ContextError):
        separate_browser(env[0].files.root, link / "browser")


def test_one_actual_live_thread_no_duplicates_stale_cancel_no_model_or_jobs(env, tmp_path):
    entered = threading.Event()
    calls = []

    def operation(mode, stop):
        calls.append(mode)
        entered.set()
        assert stop.wait(3)
        ConnectionRunner._check_stop(stop)

    runner = ConnectionRunner(env[0].files.root, tmp_path / "own-browser", operation=operation)
    before = env[0].snapshot()
    try:
        result = runner.start(mode="login", source_confirmed=True)
        assert entered.wait(1) and result["active"] and runner.status()["state"] == "starting"
        with pytest.raises(ContextError) as caught:
            runner.start(mode="check", source_confirmed=True)
        assert caught.value.code == "connection_busy"
        with pytest.raises(ContextError):
            runner.cancel(run_id="c_older")
        assert runner.status()["active"]
        assert "profile" not in json.dumps(runner.status())
        runner.cancel(run_id=result["run_id"])
        final = terminal(runner)
        assert final["state"] == "cancelled" and final["model_requests"] == 0
        assert calls == ["login"] and env[0].snapshot() == before
    finally:
        runner.close()
    with pytest.raises(ContextError):
        runner.start(mode="check", source_confirmed=True)


@pytest.mark.parametrize(
    "args,code",
    [
        ({"mode": "check", "source_confirmed": False}, "source_confirmation_required"),
        ({"mode": "check", "source_confirmed": 1}, "source_confirmation_required"),
        ({"mode": "arbitrary", "source_confirmed": True}, "invalid_argument"),
        ({"mode": "login", "source_confirmed": True}, "desktop_login_required"),
    ],
)
def test_invalid_or_headless_login_does_not_launch(env, tmp_path, args, code):
    runner = ConnectionRunner(
        env[0].files.root,
        tmp_path / "browser",
        headless=True,
        operation=lambda *_: pytest.fail("must not start"),
    )
    try:
        with pytest.raises(ContextError) as caught:
            runner.start(**args)
        assert caught.value.code == code and runner.status()["state"] == "idle"
        assert not (tmp_path / "browser").exists()
    finally:
        runner.close()


def test_unexpected_failure_is_sanitized_no_implicit_retry(env, tmp_path):
    calls = []

    def operation(*_):
        calls.append(1)
        raise RuntimeError("synthetic cookie and private path must not surface")

    runner = ConnectionRunner(env[0].files.root, tmp_path / "browser", operation=operation)
    try:
        runner.start(mode="check", source_confirmed=True)
        value = terminal(runner)
        assert value["error_code"] == "connection_observation_failed" and value["state"] == "failed"
        assert "cookie" not in json.dumps(value) and calls == [1]
    finally:
        runner.close()


def test_thread_start_failure_can_close_without_joining_unstarted_thread(env, tmp_path, monkeypatch):
    runner = ConnectionRunner(env[0].files.root, tmp_path / "browser")
    monkeypatch.setattr(threading.Thread, "start", lambda *_: (_ for _ in ()).throw(RuntimeError("fixture")))
    with pytest.raises(ContextError) as caught:
        runner.start(mode="check", source_confirmed=True)
    assert caught.value.code == "connection_start_failed" and not runner.status()["active"]
    runner.close()


def test_cancel_before_browser_and_during_page_closes_resources(env):
    with pytest.raises(ContextError) as caught:
        DouyinBrowserSource(SimpleNamespace(headless=True, context=object())).account(cancelled=lambda: True)
    assert caught.value.code == "connection_cancelled"
    checks = []
    page = AccountPage([])
    browser = SimpleNamespace(headless=True, context=SimpleNamespace(new_page=lambda: page))

    def cancelled():
        checks.append(1)
        return len(checks) > 1

    with pytest.raises(ContextError) as caught:
        ConnectionWorkflow(env[0], DouyinBrowserSource(browser)).observe(cancelled=cancelled)
    assert caught.value.code == "connection_cancelled" and page.closed
    assert ConnectionCatalog(env[0]).status()["state"] == "unverified"
    assert not env[0].snapshot()["jobs"]


def test_default_runner_owns_browser_store_and_records_proof(env, tmp_path, monkeypatch):
    events = []

    class Browser:
        def __init__(self, path, *, headless):
            assert path == tmp_path / "own-browser" and not headless
            self.headless = headless
            self.context = SimpleNamespace(
                new_page=lambda: AccountPage([("/aweme/v1/web/user/profile/self/", account_payload())])
            )

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_):
            events.append("closed")

    monkeypatch.setattr("collection_context.application.connection_runner.BrowserSession", Browser)
    runner = ConnectionRunner(env[0].files.root, tmp_path / "own-browser")
    try:
        runner.start(mode="login", source_confirmed=True)
        assert terminal(runner)["state"] == "completed"
        assert runner.status()["browser_ready_at"] is not None
        assert ConnectionCatalog(env[0]).status()["state"] == "verified"
        assert events == ["enter", "closed"] and not env[0].snapshot()["jobs"]
    finally:
        runner.close()


def test_http_owner_only_csrf_no_bearer_or_paths(env, tmp_path):
    called = []
    runner = ConnectionRunner(
        env[0].files.root,
        tmp_path / "own-browser",
        headless=True,
        operation=lambda mode, _: called.append(mode),
    )
    registry = AccessRegistry(env[0])
    owner = registry.create("连接主人", ui=True, manage=True)
    viewer = registry.create("连接只读", ui=True)
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    with TestClient(
        create_app(env[0].files.root, policy, connection_runner=runner), base_url=policy.origin
    ) as client:
        login = client.post("/v1/session", json={"token": viewer["token"]}).json()["data"]
        args = {"mode": "check", "source_confirmed": True}
        assert (
            client.post(
                "/v1/management/connection-start", json=args, headers={"X-CSRF-Token": login["csrf_token"]}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/management/connection-start",
                json=args,
                headers={"Authorization": "Bearer " + owner["token"]},
            ).status_code
            == 403
        )
        login = client.post("/v1/session", json={"token": owner["token"]}).json()["data"]
        headers = {"X-CSRF-Token": login["csrf_token"]}
        assert client.post("/v1/management/connection-start", json=args).status_code == 403
        bad = client.post(
            "/v1/management/connection-start", json={**args, "profile": "/private"}, headers=headers
        )
        assert bad.json()["error"]["code"] == "invalid_argument" and not called
        good = client.post("/v1/management/connection-start", json=args, headers=headers)
        assert good.json()["ok"] and terminal(runner)["state"] == "completed"
        assert called == ["check"]
    assert not runner.status()["enabled"]
