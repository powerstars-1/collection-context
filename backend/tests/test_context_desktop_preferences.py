"""Offline remembered selection; not native GUI or Windows acceptance."""

from __future__ import annotations

import json
import queue
import threading
import types
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.desktop_preferences import DesktopPreferences, DesktopSelection
from collection_context.native_desktop import DesktopController, _DesktopWindow, main


def test_missing_preference_does_not_create_anything(tmp_path):
    preferences = DesktopPreferences(tmp_path / "product" / "desktop")
    assert preferences.load() is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("legacy", [False, True])
def test_roundtrip_only_location_port_mode_not_permissions(tmp_path, legacy):
    source = tmp_path / "外置盘 中文" / "旧资料库"
    preferences = DesktopPreferences(tmp_path / "product" / "desktop")
    selection = DesktopSelection(source, 8791, legacy)
    preferences.save(selection)
    assert preferences.load() == selection
    path = preferences.directory / "last-selection.json"
    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "workspace": str(source),
        "port": 8791,
        "legacy_readonly": legacy,
    }
    assert path.stat().st_mode & 0o077 == 0
    assert not source.exists()
    assert list(preferences.directory.iterdir()) == [path]
    replacement = DesktopSelection(tmp_path / "other", 8800)
    preferences.save(replacement)
    assert preferences.load() == replacement
    assert list(preferences.directory.iterdir()) == [path]


@pytest.mark.parametrize(
    "change",
    [
        {"permissions": [True] * 4},
        {"initialize_empty": True},
        {"token": "synthetic-not-a-real-key"},
        {"schema_version": True},
        {"schema_version": 2},
        {"port": True},
        {"port": "8787"},
        {"port": 0},
        {"port": 65_536},
        {"legacy_readonly": "true"},
        {"workspace": "relative"},
        {"workspace": "/tmp/../other"},
        {"workspace": "/tmp/path\ncommand"},
        {"workspace": "/" + "x" * 4096},
        {"workspace": None},
    ],
)
def test_untrusted_fields_are_rejected_not_repaired(tmp_path, change):
    preferences = DesktopPreferences(tmp_path / "desktop")
    preferences.directory.mkdir()
    payload = DesktopSelection(tmp_path / "source", 8787).payload()
    payload.update(change)
    data = json.dumps(payload).encode()
    path = preferences.directory / "last-selection.json"
    path.write_bytes(data)
    with pytest.raises(ContextError) as caught:
        preferences.load()
    assert caught.value.code == "desktop_preference_unavailable"
    assert path.read_bytes() == data
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("data", [b"{", b"[]", b"x" * 20_001])
def test_bounded_corrupt_preference_is_not_overwritten(tmp_path, data):
    preferences = DesktopPreferences(tmp_path / "desktop")
    preferences.directory.mkdir()
    path = preferences.directory / "last-selection.json"
    path.write_bytes(data)
    with pytest.raises(ContextError):
        preferences.load()
    assert path.read_bytes() == data


@pytest.mark.parametrize("linked_parent", [False, True])
def test_preference_links_not_followed_for_read_or_save(tmp_path, linked_parent):
    destination = tmp_path / "other"
    destination.mkdir()
    path = destination / "last-selection.json"
    before = json.dumps(DesktopSelection(tmp_path / "source", 8787).payload()).encode()
    path.write_bytes(before)
    local = tmp_path / "desktop"
    if linked_parent:
        local.symlink_to(destination, target_is_directory=True)
    else:
        local.mkdir()
        (local / "last-selection.json").symlink_to(path)
    preferences = DesktopPreferences(local)
    with pytest.raises(ContextError):
        preferences.load()
    with pytest.raises(ContextError):
        preferences.save(DesktopSelection(tmp_path / "new-source", 8791))
    assert path.read_bytes() == before
    assert list(destination.iterdir()) == [path]


def test_selected_source_cannot_contain_the_preference_directory(tmp_path):
    preferences = DesktopPreferences(tmp_path / "source" / "desktop")
    with pytest.raises(ContextError):
        preferences.save(DesktopSelection(tmp_path / "source", 8787, True))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("ready", [False, True])
def test_only_verified_ready_records_selection_on_worker(tmp_path, ready, capsys):
    recorded = []

    def service(workspace, **options):
        if ready:
            options["output"].write("管理页已就绪：http://127.0.0.1:8791\n")
            options["output"].write("管理页已就绪：http://127.0.0.1:8791\n")
        return 0 if ready else 2

    def remember(selection):
        recorded.append((selection, threading.current_thread()))

    controller = DesktopController(launch_service=service, remember_selection=remember)
    controller.start(tmp_path, port=8791, initialize_empty=False, legacy_readonly=True)
    controller.join(2)
    assert not controller.active
    assert len(recorded) == int(ready)
    if ready:
        assert recorded[0][0] == DesktopSelection(tmp_path, 8791, True)
        assert recorded[0][1] is not threading.main_thread()
    assert not list(tmp_path.iterdir())
    assert capsys.readouterr() == ("", "")


def test_record_failure_does_not_turn_running_service_into_failure(tmp_path, capsys):
    seen = []

    def service(workspace, **options):
        options["output"].write("管理页已就绪：http://127.0.0.1:8787\n")
        seen.append(controller.state)
        return 0

    def remember(selection):
        raise RuntimeError("synthetic-sensitive-error")

    controller = DesktopController(launch_service=service, remember_selection=remember)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    controller.join(2)
    assert seen == ["running"]
    messages = []
    while not controller.events.empty():
        messages.append(controller.events.get_nowait())
    assert any("未能保存" in message.message for message in messages)
    assert "synthetic-sensitive-error" not in repr(messages)
    assert capsys.readouterr() == ("", "")


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def window_fixture():
    window = _DesktopWindow.__new__(_DesktopWindow)
    window._preference_events = queue.Queue()
    window._restore_fields = (True, True)
    window.close_requested = False
    window.controller = types.SimpleNamespace(active=False)
    window.diagnostics = types.SimpleNamespace(generation=3)
    window._start_intent = None
    window.workspace = Variable("/default")
    window.port = Variable("8787")
    window.legacy_readonly = Variable(False)
    window.initialize = Variable(False)
    window.permission_variables = tuple(Variable(False) for _ in range(4))
    window.status = Variable("")
    window._legacy_mode_changed = lambda: None
    window._refresh_dependencies = lambda: None
    return window


def test_restore_only_fills_visible_controls_never_permissions_or_start(tmp_path):
    window = window_fixture()
    window._preference_events.put((3, DesktopSelection(tmp_path / "离线旧库", 8791, True), False))
    window._poll_preferences()
    assert window.workspace.get() == str(tmp_path / "离线旧库")
    assert window.port.get() == "8791" and window.legacy_readonly.get() is True
    assert window.initialize.get() is False
    assert not any(variable.get() for variable in window.permission_variables)
    assert "尚未启动" in window.status.get()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("reason", ["edit", "start", "close", "active", "installation"])
def test_late_preference_result_cannot_override_new_intent(tmp_path, reason):
    window = window_fixture()
    if reason == "edit":
        window.diagnostics.generation = 4
    elif reason == "start":
        window._start_intent = 3
    elif reason == "close":
        window.close_requested = True
    elif reason == "active":
        window.controller.active = True
    else:
        window.installation = types.SimpleNamespace(active=True)
    window._preference_events.put((3, DesktopSelection(tmp_path, 8791, True), False))
    window._poll_preferences()
    assert window.workspace.get() == "/default" and window.port.get() == "8787"
    assert window.legacy_readonly.get() is False


def test_read_worker_never_blocks_gui_or_changes_controls(tmp_path):
    window = window_fixture()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    threads = []

    def load():
        threads.append(threading.current_thread())
        entered.set()
        release.wait(2)
        finished.set()
        return DesktopSelection(tmp_path, 8791, True)

    window.preferences = types.SimpleNamespace(load=load)
    window.restore_selection(workspace=True, port=True)
    try:
        assert entered.wait(1)
        assert window.workspace.get() == "/default" and not finished.is_set()
        assert threads[0] is not threading.main_thread() and threads[0].daemon
        window.close_requested = True
    finally:
        release.set()
        assert finished.wait(2)
        window._preference_worker.join(2)
        assert not window._preference_worker.is_alive()
    window._poll_preferences()
    assert window.workspace.get() == "/default"


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], (True, True)),
        (["--workspace", "/explicit"], (False, True)),
        (["--port", "8791"], (True, False)),
        (["--workspace", "/explicit", "--port", "8791"], (False, False)),
    ],
)
def test_explicit_launch_parameters_override_remembered_fields(monkeypatch, args, expected):
    restored = []
    constructed = []
    window = types.SimpleNamespace(
        controller=types.SimpleNamespace(active=False, request_stop=lambda: None, join=lambda timeout: None),
        show=lambda: None,
        restore_selection=lambda **fields: restored.append(fields),
    )

    def factory(workspace, port):
        constructed.append((workspace, port))
        return window

    monkeypatch.setattr("collection_context.native_desktop._DesktopWindow", factory)
    assert main(args) == 0
    assert restored == [{"workspace": expected[0], "port": expected[1]}]
    if "--workspace" in args:
        assert constructed[0][0] == Path("/explicit")
    if "--port" in args:
        assert constructed[0][1] == 8791
