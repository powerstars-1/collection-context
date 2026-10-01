from __future__ import annotations

import io
import json
import re
import socket
import threading
import urllib.request

import pytest

from collection_context.application.contracts import ContextError
from collection_context.diagnostics import port_report, startup_report, workspace_report
from collection_context.interfaces.access import AccessRegistry
from collection_context.launcher import _ensure_owner_access, _prepare_workspace, launch, main


def error(code, operation):
    with pytest.raises(ContextError) as caught:
        operation()
    assert caught.value.code == code


def test_workspace_report_and_initialization_gate_preserve_nonempty_directories(tmp_path):
    missing = tmp_path / "新资料库"
    assert workspace_report(missing)["state"] == "missing"
    error(
        "workspace_initialization_required",
        lambda: _prepare_workspace(missing, initialize_empty=False),
    )
    assert not missing.exists()

    unrelated = tmp_path / "已有目录"
    unrelated.mkdir()
    marker = unrelated / "保留.txt"
    marker.write_text("不得覆盖", encoding="utf-8")
    error("workspace_not_usable", lambda: _prepare_workspace(unrelated, initialize_empty=True))
    assert marker.read_text(encoding="utf-8") == "不得覆盖"


def test_explicit_empty_initialization_builds_index_and_reopens(tmp_path):
    path = tmp_path / "新资料 空格"
    path.mkdir()
    store, created = _prepare_workspace(path, initialize_empty=True)
    assert created and store.snapshot()["items"] == {}
    assert (path / ".context" / "索引" / "CURRENT.json").is_file()
    store.close()

    reopened, created = _prepare_workspace(path, initialize_empty=False)
    assert not created and reopened.snapshot()["generation"] == 0
    reopened.close()


def test_first_launch_access_token_is_printed_once_and_only_hash_is_stored(tmp_path):
    store, _ = _prepare_workspace(tmp_path / "库", initialize_empty=True)
    output = io.StringIO()
    _ensure_owner_access(store, output)
    text = output.getvalue()
    token = re.search(r"scc_[A-Za-z0-9_-]+", text)
    assert token is not None
    persisted = (tmp_path / "库" / ".context" / "访问规则.json").read_text(encoding="utf-8")
    assert token.group(0) not in persisted
    assert AccessRegistry(store).credentials()[0].permissions == frozenset(
        {"collections:read", "ui:view", "ui:manage"}
    )

    again = io.StringIO()
    _ensure_owner_access(store, again)
    assert again.getvalue() == ""
    store.close()


def test_existing_read_only_access_does_not_block_creation_of_local_owner_access(tmp_path):
    store, _ = _prepare_workspace(tmp_path / "只读库", initialize_empty=True)
    AccessRegistry(store).create("AI只读")
    output = io.StringIO()
    _ensure_owner_access(store, output)
    permissions = [credential.permissions for credential in AccessRegistry(store).credentials()]
    assert frozenset({"collections:read"}) in permissions
    assert frozenset({"collections:read", "ui:view", "ui:manage"}) in permissions
    assert re.search(r"scc_[A-Za-z0-9_-]+", output.getvalue())
    store.close()


def test_port_check_reports_conflict_without_stopping_other_process():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        assert port_report(port)["code"] == "port_in_use"
        occupied.listen()
        assert occupied.getsockname()[1] == port


def test_diagnostic_report_is_secret_free_and_does_not_initialize(tmp_path, capsys):
    workspace = tmp_path / "诊断库"
    report = startup_report(workspace, 8787)
    assert report["workspace"]["state"] == "missing"
    assert report["authorizations"] == {
        "source_sync": False,
        "model_calls": False,
        "automatic_downloads": False,
    }
    assert not workspace.exists()

    result = main(["--workspace", str(workspace), "--diagnose"])
    output = json.loads(capsys.readouterr().out)
    assert result in {0, 1}
    assert output["workspace"]["state"] == "missing"
    assert "token" not in json.dumps(output).lower()
    assert not workspace.exists()


def test_invalid_or_occupied_port_is_rejected_before_workspace_creation(tmp_path):
    workspace = tmp_path / "不会创建"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        assert (
            main(
                [
                    "--workspace",
                    str(workspace),
                    "--port",
                    str(port),
                    "--initialize-empty",
                    "--no-browser",
                ]
            )
            == 1
        )
    assert not workspace.exists()


def test_desktop_owner_confirmation_never_writes_token_to_output(tmp_path):
    store, _ = _prepare_workspace(tmp_path / "窗口口令库", initialize_empty=True)
    output = io.StringIO()
    presented = []

    def accept(token):
        presented.append(token)
        return True

    try:
        _ensure_owner_access(store, output, owner_presenter=accept)
        assert len(presented) == 1
        assert presented[0] not in output.getvalue()
        assert "scc_" not in output.getvalue()
        assert "ui:manage" in AccessRegistry(store).authenticate(presented[0]).permissions
        persisted = store.files.read(".context/访问规则.json")
        assert presented[0].encode() not in persisted
        _ensure_owner_access(
            store, output, owner_presenter=lambda _: pytest.fail("existing owner must not be re-presented")
        )
    finally:
        store.close()


@pytest.mark.parametrize("reply", [False, None, "true", 1])
def test_desktop_cancellation_only_revokes_its_new_credential(tmp_path, reply):
    store, _ = _prepare_workspace(tmp_path / "撤销库", initialize_empty=True)
    registry = AccessRegistry(store)
    existing = registry.create("已有AI只读")
    before = registry.records()
    output = io.StringIO()
    presented = []

    def cancel(token):
        presented.append(token)
        return reply

    try:
        error(
            "owner_confirmation_cancelled",
            lambda: _ensure_owner_access(store, output, owner_presenter=cancel),
        )
        assert registry.records() == before
        assert registry.authenticate(existing["token"]).principal == existing["principal"]
        error("authentication_required", lambda: registry.authenticate(presented[0]))
        assert "scc_" not in output.getvalue()
    finally:
        store.close()


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_desktop_exception_is_sanitized_and_new_access_rolled_back(tmp_path, failure):
    store, _ = _prepare_workspace(tmp_path / "故障库", initialize_empty=True)
    output = io.StringIO()
    presented = []

    def fail(token):
        presented.append(token)
        raise failure("private token: " + token)

    try:
        with pytest.raises(ContextError) as caught:
            _ensure_owner_access(store, output, owner_presenter=fail)
        assert caught.value.code == "owner_presentation_failed"
        assert caught.value.__suppress_context__
        assert presented[0] not in str(caught.value)
        assert AccessRegistry(store).records() == []
        assert "scc_" not in output.getvalue()
    finally:
        store.close()


def test_desktop_stopped_before_launch_does_not_create_any_files(tmp_path):
    stopped = threading.Event()
    stopped.set()
    workspace = tmp_path / "不会创建"
    assert (
        launch(
            workspace,
            port=8787,
            initialize_empty=True,
            no_browser=True,
            output=io.StringIO(),
            stop_event=stopped,
            owner_presenter=lambda _: pytest.fail("must not create credential"),
        )
        == 0
    )
    assert not workspace.exists()


def test_desktop_failed_rollback_stops_with_secret_free_actionable_error(tmp_path, monkeypatch):
    store, _ = _prepare_workspace(tmp_path / "撤销故障库", initialize_empty=True)
    output = io.StringIO()
    token_seen = []

    def cancel(token):
        token_seen.append(token)
        return False

    def fail_revoke(*_):
        raise OSError("private exception: " + token_seen[0])

    monkeypatch.setattr(AccessRegistry, "revoke", fail_revoke)
    try:
        with pytest.raises(ContextError) as caught:
            _ensure_owner_access(store, output, owner_presenter=cancel)
        assert caught.value.code == "owner_rollback_failed"
        assert caught.value.__suppress_context__
        assert token_seen[0] not in str(caught.value)
        assert "scc_" not in output.getvalue()
        assert len(AccessRegistry(store).records()) == 1
    finally:
        store.close()


def test_desktop_stop_event_drains_real_loopback_server(tmp_path, monkeypatch):
    monkeypatch.setattr("collection_context.launcher._desktop_available", lambda: True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    stopped = threading.Event()
    ready = threading.Event()
    outcomes = []
    output = io.StringIO()
    workspace = tmp_path / "桌面服务库"

    def run():
        try:
            outcomes.append(
                launch(
                    workspace,
                    port=port,
                    initialize_empty=True,
                    no_browser=False,
                    output=output,
                    stop_event=stopped,
                    owner_presenter=lambda _: True,
                    open_page=lambda _: ready.set(),
                )
            )
        except BaseException as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=run, name="desktop-launch-test")
    worker.start()
    try:
        assert ready.wait(timeout=10), output.getvalue()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            assert response.status == 200
    finally:
        stopped.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcomes == [0]
    assert "scc_" not in output.getvalue()
    assert workspace_report(workspace)["state"] == "initialized_candidate"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
