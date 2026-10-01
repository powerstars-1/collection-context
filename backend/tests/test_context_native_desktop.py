"""Offline native lifecycle contracts; these do not prove a real GUI display."""

from __future__ import annotations

import queue
import threading
import types

import pytest

from collection_context.application.contracts import ContextError
from collection_context.native_bootstrap import _TokenWindow
from collection_context.native_desktop import (
    DesktopController,
    DesktopStatus,
    OwnerPrompt,
    _DesktopWindow,
    capability_description,
    main,
)

TOKEN = "scc_" + "synthetic_not_a_real_credential_" * 2


def next_prompt(controller):
    while True:
        event = controller.events.get(timeout=2)
        if isinstance(event, OwnerPrompt):
            return event


def status_events(controller):
    events = []
    while True:
        try:
            events.append(controller.events.get_nowait())
        except queue.Empty:
            return [event for event in events if isinstance(event, DesktopStatus)]


def test_construct_and_invalid_port_do_not_launch_or_create(tmp_path):
    calls = []
    controller = DesktopController(launch_service=lambda *a, **kw: calls.append((a, kw)) or 0)
    workspace = tmp_path / "not-created"
    assert controller.state == "idle" and not calls and not workspace.exists()
    with pytest.raises(ContextError):
        controller.start(workspace, port=0, initialize_empty=True)
    assert not controller.active and not calls and not workspace.exists()


def test_explicit_start_forwards_shared_contract_and_can_restart(tmp_path):
    calls = []
    entered = threading.Event()

    def service(workspace, **kw):
        calls.append((workspace, kw))
        entered.set()
        kw["stop_event"].wait(2)
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    try:
        assert entered.wait(2)
        assert not controller._worker.daemon
        with pytest.raises(ContextError):
            controller.start(tmp_path, port=8787, initialize_empty=True)
    finally:
        controller.request_stop()
        controller.join(2)
    assert not controller.active and controller.state == "stopped"
    first_event = calls[0][1]["stop_event"]
    assert calls[0][0] == tmp_path
    assert calls[0][1]["initialize_empty"] is False
    assert calls[0][1]["no_browser"] is False
    assert set(calls[0][1]) == {
        "port",
        "initialize_empty",
        "no_browser",
        "output",
        "owner_presenter",
        "stop_event",
    }
    entered.clear()
    controller.start(tmp_path, port=8788, initialize_empty=True)
    try:
        assert entered.wait(2)
    finally:
        controller.request_stop()
        controller.join(2)
    assert calls[1][1]["stop_event"] is not first_event
    assert calls[1][1]["initialize_empty"] is True and not controller.active


@pytest.mark.parametrize("accepted", [True, False, None, "true"])
def test_gui_queue_ack_is_main_thread_strict_bool_and_secret_cleared(tmp_path, accepted, capsys):
    results = []

    def service(workspace, **kw):
        results.append(kw["owner_presenter"](TOKEN))
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    prompt = next_prompt(controller)
    threads = []

    def present(token):
        assert token == TOKEN
        threads.append(threading.current_thread())
        return accepted

    try:
        assert TOKEN not in repr(prompt)
        controller.present_on_main_thread(prompt, present)
    finally:
        controller.request_stop() if not prompt.done.is_set() else None
        controller.join(2)
    assert threads == [threading.main_thread()]
    assert results == [accepted is True] and not controller.active
    assert prompt.token == "" and not controller._pending
    assert TOKEN not in repr(status_events(controller))
    assert capsys.readouterr() == ("", "")


def test_stop_unlocks_pending_presenter_without_gui(tmp_path):
    results = []

    def service(workspace, **kw):
        results.append(kw["owner_presenter"](TOKEN))
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    prompt = next_prompt(controller)
    controller.request_stop()
    controller.join(2)
    controller.present_on_main_thread(prompt, lambda token: pytest.fail("cancelled prompt shown"))
    assert not controller.active and results == [False]
    assert prompt.done.is_set() and not prompt.token and not controller._pending


def test_stop_during_visible_prompt_overrides_late_accept(tmp_path):
    results = []

    def service(workspace, **kw):
        results.append(kw["owner_presenter"](TOKEN))
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    prompt = next_prompt(controller)

    def present(token):
        controller.request_stop()
        return True

    controller.present_on_main_thread(prompt, present)
    controller.join(2)
    assert results == [False] and not controller.active and not prompt.token


def test_presenter_failure_is_sanitized_and_rejects(tmp_path, capsys):
    results = []

    def service(workspace, **kw):
        results.append(kw["owner_presenter"](TOKEN))
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    prompt = next_prompt(controller)

    def present(token):
        raise RuntimeError(token)

    controller.present_on_main_thread(prompt, present)
    controller.join(2)
    events = status_events(controller)
    assert not results and not controller.active
    assert events[-1].code == "owner_presentation_failed"
    assert TOKEN not in repr(events)
    assert capsys.readouterr() == ("", "")


def test_worker_cannot_execute_gui_presentation():
    controller = DesktopController()
    prompt = OwnerPrompt(TOKEN)
    errors = []

    def worker():
        try:
            controller.present_on_main_thread(prompt, lambda token: pytest.fail("GUI in worker"))
        except ContextError as error:
            errors.append(error.code)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert errors == ["desktop_display_required"]
    assert prompt.done.is_set() and not prompt.accepted and not prompt.token


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(TOKEN),
        SystemExit(TOKEN),
        ContextError(TOKEN, TOKEN),
        ContextError("owner_rollback_failed", TOKEN),
    ],
)
def test_unknown_service_failure_does_not_leak(tmp_path, failure, capsys):
    def service(workspace, **kw):
        raise failure

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    controller.join(2)
    events = status_events(controller)
    assert not controller.active and events[-1].state == "failed"
    assert TOKEN not in repr(events) and capsys.readouterr() == ("", "")
    if isinstance(failure, ContextError) and failure.code == "owner_rollback_failed":
        assert events[-1].code == "owner_rollback_failed"


def test_discard_output_only_records_ready_not_secrets_or_late_ready(tmp_path, capsys):
    states = []

    def service(workspace, **kw):
        kw["output"].write(TOKEN)
        kw["output"].write("管理页已就绪：http://127.0.0.1:8787/")
        states.extend(status_events(controller))
        controller.request_stop()
        kw["output"].write("管理页已就绪：" + TOKEN)
        return 0

    controller = DesktopController(launch_service=service)
    controller.start(tmp_path, port=8787, initialize_empty=False)
    controller.join(2)
    states.extend(status_events(controller))
    assert [event.state for event in states] == ["starting", "running", "stopping", "stopped"]
    assert TOKEN not in repr(states) and capsys.readouterr() == ("", "")


def test_window_build_failure_destroys_allocated_root(monkeypatch, capsys):
    roots = []

    class BrokenRoot:
        def __init__(self):
            self.destroyed = False
            roots.append(self)

        def title(self, text):
            raise RuntimeError(TOKEN)

        def destroy(self):
            self.destroyed = True

    monkeypatch.setitem(
        __import__("sys").modules, "tkinter", types.SimpleNamespace(Tk=BrokenRoot, ttk=object())
    )
    with pytest.raises(ContextError) as caught:
        _TokenWindow()
    assert roots[0].destroyed
    assert TOKEN not in str(caught.value) and caught.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


def test_close_requests_stop_and_clears_visible_prompt_without_destroying_early():
    window = _DesktopWindow.__new__(_DesktopWindow)
    calls = []
    window.controller = types.SimpleNamespace(active=True, request_stop=lambda: calls.append("stop"))
    window.token_window = types.SimpleNamespace(clear_and_close=lambda: calls.append("clear"))
    window.status = types.SimpleNamespace(set=lambda text: calls.append(text))
    window._controls = lambda active: calls.append(active)
    window.root = types.SimpleNamespace(destroy=lambda: pytest.fail("destroy before stop"))
    window._close()
    assert window.close_requested and calls[:2] == ["stop", "clear"]


def test_tk_callback_reporter_does_not_print_exception_widget_values(capsys):
    window = _DesktopWindow.__new__(_DesktopWindow)
    calls = []
    window.controller = types.SimpleNamespace(request_stop=lambda: calls.append("stop"))
    window.token_window = types.SimpleNamespace(clear_and_close=lambda: calls.append("clear"))
    window.status = types.SimpleNamespace(set=lambda text: calls.append(text))
    window.root = types.SimpleNamespace(after=lambda *args: calls.append("poll"))
    window._callback_failed(ValueError, ValueError(TOKEN), object())
    assert window.close_requested and calls[:2] == ["stop", "clear"]
    assert TOKEN not in repr(calls) and capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("state", "consent", "confirm", "starts"),
    [
        ("missing", False, True, False),
        ("missing", True, False, False),
        ("missing", True, True, True),
        ("empty", True, True, True),
        ("non_empty_uninitialized", True, True, False),
        ("unsafe_link", True, True, False),
        ("initialized_candidate", False, False, True),
    ],
)
def test_ui_initialization_needs_checkbox_and_dialog_not_selection(tmp_path, state, consent, confirm, starts):
    window = _DesktopWindow.__new__(_DesktopWindow)
    calls = []
    report = {
        "capabilities": {"ready_for_management_page": True},
        "listen": {"available": True},
        "workspace": {"state": state},
    }
    window.controller = types.SimpleNamespace(
        active=False, start=lambda *args, **kw: calls.append((args, kw))
    )
    window.close_requested = False
    window._refresh_dependencies = lambda: report
    window.status = types.SimpleNamespace(set=lambda text: None)
    window.initialize = types.SimpleNamespace(get=lambda: consent)
    window.messagebox = types.SimpleNamespace(askyesno=lambda *args, **kw: confirm)
    window.root = object()
    window._parameters = lambda: (tmp_path / "not-created", 8787)
    window._controls = lambda active: None
    window._start()
    assert bool(calls) is starts and not list(tmp_path.iterdir())
    if starts:
        assert calls[0][1] == {"port": 8787, "initialize_empty": consent}


def test_gui_unavailable_reports_fixed_error_without_launch(monkeypatch, capsys, tmp_path):
    def unavailable(*args):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr("collection_context.native_desktop._DesktopWindow", unavailable)
    assert main(["--workspace", str(tmp_path), "--port", "8787"]) == 2
    output = capsys.readouterr()
    assert not output.out and "desktop_display_required" in output.err and TOKEN not in output.err
    assert not list(tmp_path.iterdir())


def test_gui_exit_cannot_report_success_with_live_service(monkeypatch, capsys, tmp_path):
    calls = []
    controller = types.SimpleNamespace(
        active=True,
        request_stop=lambda: calls.append("stop"),
        join=lambda timeout: calls.append(timeout),
    )

    def recover():
        calls.append("recover")
        raise RuntimeError(TOKEN)

    window = types.SimpleNamespace(controller=controller, show=lambda: None, recover_stopping=recover)
    monkeypatch.setattr("collection_context.native_desktop._DesktopWindow", lambda *args: window)
    assert main(["--workspace", str(tmp_path)]) == 3
    output = capsys.readouterr()
    assert calls == ["stop", 5, "recover"]
    assert "desktop_stop_pending" in output.err and TOKEN not in output.err


@pytest.mark.parametrize("operation", ["stop", "cancel", "failure"])
def test_real_registry_revokes_only_new_permission_through_queue(tmp_path, operation):
    from collection_context.interfaces.access import AccessRegistry
    from collection_context.launcher import _ensure_owner_access, _prepare_workspace

    workspace = tmp_path / "合成 桌面库"
    store, _ = _prepare_workspace(workspace, initialize_empty=True)
    registry = AccessRegistry(store)
    existing = registry.create("existing-readonly")
    before = registry.records()
    store.close()

    def service(workspace, **kw):
        current, _ = _prepare_workspace(workspace, initialize_empty=False)
        try:
            _ensure_owner_access(current, kw["output"], owner_presenter=kw["owner_presenter"])
            return 0
        finally:
            current.close()

    controller = DesktopController(launch_service=service)
    controller.start(workspace, port=8787, initialize_empty=False)
    prompt = next_prompt(controller)
    shown = prompt.token
    try:
        if operation == "stop":
            controller.request_stop()
        else:

            def present(token):
                if operation == "failure":
                    raise RuntimeError(token)
                return False

            controller.present_on_main_thread(prompt, present)
    finally:
        controller.join(2)
    assert not controller.active and not prompt.token
    restored, _ = _prepare_workspace(workspace, initialize_empty=False)
    try:
        registry = AccessRegistry(restored)
        assert registry.records() == before
        assert registry.authenticate(existing["token"]).principal == existing["principal"]
        with pytest.raises(ContextError):
            registry.authenticate(shown)
    finally:
        restored.close()
    assert shown not in repr(status_events(controller))


def test_dependency_labels_show_degraded_and_do_not_render_free_text():
    report = {
        "capabilities": {
            "dependencies": [
                {
                    "role": "media_ffmpeg",
                    "available": False,
                    "required_for_start": False,
                    "next_action": TOKEN,
                },
                {"role": "management_api", "available": True, "required_for_start": True},
            ]
        }
    }
    text = capability_description(report)
    assert "可降级" in text and "启动必需" in text and TOKEN not in text


def test_optional_probe_does_not_claim_runtime_verified():
    report = {
        "capabilities": {
            "dependencies": [
                {
                    "role": "source_browser",
                    "available": False,
                    "required_for_start": False,
                    "package_available": True,
                },
                {"role": "media_ffmpeg", "available": True, "required_for_start": False},
                {"role": "local_ocr", "available": True, "required_for_start": False},
            ]
        }
    }
    text = capability_description(report)
    assert "缺失" not in text and "可用" not in text
    assert "功能未验证，已发现模块" in text and text.count("待功能验证") == 2


def test_desktop_help_does_not_construct_gui(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "collection_context.native_desktop._DesktopWindow", lambda *a: pytest.fail("help creates GUI")
    )
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0 and "不自动初始化" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
