"""Read-only diagnostics lifecycle; fake GUI callbacks, not a visual Tk验收."""

from __future__ import annotations

import queue
import threading
import time
import types
from pathlib import Path

import pytest

from collection_context.native_desktop import (
    DesktopDiagnostics,
    DiagnosticParameters,
    DiagnosticResult,
    _DesktopWindow,
    main,
)


def report(state="initialized_candidate"):
    return {
        "capabilities": {"ready_for_management_page": True, "dependencies": []},
        "listen": {"available": True},
        "workspace": {"state": state},
    }


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def configure(self, **kw):
        self.state = kw["state"]


def window_fixture(tmp_path, checker):
    window = _DesktopWindow.__new__(_DesktopWindow)
    starts = []
    window.controller = types.SimpleNamespace(
        active=False,
        events=queue.Queue(),
        start=lambda *args, **kw: starts.append((args, kw)),
        request_stop=lambda: None,
    )
    window.diagnostics = DesktopDiagnostics(checker=checker)
    window._diagnostic_cache = None
    window._start_intent = None
    window.close_requested = False
    window.destroyed = False
    window.token_window = None
    window.workspace = Variable(str(tmp_path / "未初始化 资料库"))
    window.port = Variable("8787")
    window.initialize = Variable(False)
    window.dependencies = Variable("")
    window.status = Variable("")
    for name in ("path_entry", "choose_button", "init_check", "port_entry", "start_button", "stop_button"):
        setattr(window, name, Widget())
    window.root = types.SimpleNamespace(after=lambda *args: None, destroy=lambda: None)
    window.messagebox = types.SimpleNamespace(askyesno=lambda *args, **kw: True)
    return window, starts


def finished(diagnostics):
    deadline = time.monotonic() + 2
    while diagnostics.active and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not diagnostics.active


def test_all_diagnostic_io_runs_off_main_thread_and_selection_never_starts(tmp_path):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def checker(workspace, port):
        seen.append((threading.current_thread(), workspace, port))
        entered.set()
        assert release.wait(2)
        return report()

    window, starts = window_fixture(tmp_path, checker)
    try:
        assert window._refresh_dependencies() is None
        assert entered.wait(2)
        assert seen[0][0] is not threading.main_thread()
        assert window.diagnostics._worker.daemon
        assert not starts and not list(tmp_path.iterdir())
        release.set()
        finished(window.diagnostics)
        window._poll_diagnostics()
        assert window._diagnostic_cache is not None and not starts
    finally:
        release.set()
        finished(window.diagnostics)


@pytest.mark.parametrize("action", ["stop", "close"])
def test_pending_check_keeps_stop_close_responsive_and_cannot_start_after_cancel(tmp_path, action):
    entered, release = threading.Event(), threading.Event()

    def checker(*args):
        entered.set()
        assert release.wait(2)
        return report("missing")

    window, starts = window_fixture(tmp_path, checker)
    window.initialize.set(True)
    try:
        window._start()
        assert entered.wait(2)
        assert not starts and window.start_button.state == "disabled"
        assert window.stop_button.state == "normal"
        getattr(window, "_" + action)()
        # The call returned while the OS-like check remains blocked. It did not
        # cancel the syscall; only permission to use its eventual result ended.
        assert window.diagnostics.active and window._start_intent is None
        if action == "close":
            window._poll()
            assert window.destroyed
        else:
            assert "仍在等待返回" in window.status.get()
        release.set()
        finished(window.diagnostics)
        window._poll_diagnostics()
        assert not starts and not list(tmp_path.iterdir())
    finally:
        release.set()
        finished(window.diagnostics)


def test_single_worker_replaces_pending_requests_and_rejects_old_results(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def checker(workspace, port):
        calls.append((workspace, port))
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return report()

    diagnostics = DesktopDiagnostics(checker=checker)
    first = DiagnosticParameters(str(tmp_path / "first"), 8787, False)
    latest = DiagnosticParameters(str(tmp_path / "latest"), 8790, True)
    try:
        initial = diagnostics.request(first)
        assert entered.wait(2)
        worker = diagnostics._worker
        assert diagnostics.request(first) == initial
        diagnostics.request(DiagnosticParameters(str(tmp_path / "discarded"), 8788, False))
        generation = diagnostics.request(latest)
        assert diagnostics._worker is worker and len(calls) == 1
        release.set()
        finished(diagnostics)
        result = diagnostics.events.get_nowait()
        assert result.generation == generation and result.parameters == latest
        assert diagnostics.events.empty()
        assert calls == [(Path(first.workspace), first.port), (Path(latest.workspace), latest.port)]
        assert not list(tmp_path.iterdir())
    finally:
        release.set()
        finished(diagnostics)


@pytest.mark.parametrize("changed", ["workspace", "port", "initialize"])
def test_parameter_changes_invalidate_start_intent_and_queued_results(tmp_path, changed):
    window, starts = window_fixture(tmp_path, lambda *args: report())
    window._start()
    finished(window.diagnostics)
    getattr(window, changed).set(
        {"workspace": str(tmp_path / "changed"), "port": "8790", "initialize": True}[changed]
    )
    window._parameters_changed()
    window._poll_diagnostics()
    assert not starts and window._diagnostic_cache is None and window._start_intent is None
    assert window.start_button.state == "normal"


def test_current_completed_check_starts_only_after_explicit_action(tmp_path):
    window, starts = window_fixture(tmp_path, lambda *args: report())
    window._refresh_dependencies()
    finished(window.diagnostics)
    window._poll_diagnostics()
    assert not starts
    window._start()
    assert len(starts) == 1 and starts[0][1] == {"port": 8787, "initialize_empty": False}
    assert not list(tmp_path.iterdir())


def test_directory_dialog_invalidates_old_start_before_nested_events(tmp_path):
    window, starts = window_fixture(tmp_path, lambda *args: report())
    window._start()
    finished(window.diagnostics)

    def choose(**kw):
        window._poll_diagnostics()
        assert not starts and window._start_intent is None
        return ""

    window.filedialog = types.SimpleNamespace(askdirectory=choose)
    window._choose()
    assert not starts and window._diagnostic_cache is None


def test_closed_window_does_not_queue_new_diagnostic(tmp_path):
    window, starts = window_fixture(tmp_path, lambda *args: pytest.fail("check after close"))
    window._close()
    assert window._refresh_dependencies() is None
    assert not window.diagnostics.active and not starts


@pytest.mark.parametrize("action", ["stop", "close", "change"])
def test_nested_initialization_confirmation_cannot_launch_cancelled_or_stale_check(tmp_path, action):
    window, starts = window_fixture(tmp_path, lambda *args: report("missing"))
    window.initialize.set(True)
    confirms = []

    def confirm(*args, **kw):
        confirms.append(True)
        if action == "change":
            window.port.set("8791")
            window._parameters_changed()
        else:
            getattr(window, "_" + action)()
        return True

    window.messagebox.askyesno = confirm
    window._start()
    assert not confirms and not starts
    finished(window.diagnostics)
    window._poll_diagnostics()
    assert confirms == [True] and not starts and not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [RuntimeError("private exception"), SystemExit("private exception")])
def test_diagnostic_failure_is_fixed_sanitized_and_does_not_launch(tmp_path, capsys, failure):
    def checker(*args):
        raise failure

    window, starts = window_fixture(tmp_path, checker)
    window._start()
    finished(window.diagnostics)
    window._poll_diagnostics()
    assert not starts and "检测失败" in window.status.get()
    assert "private exception" not in window.status.get() + window.dependencies.get()
    assert capsys.readouterr() == ("", "")


def test_mismatched_result_never_enters_gui_cache_or_starts(tmp_path):
    window, starts = window_fixture(tmp_path, lambda *args: report())
    parameters = DiagnosticParameters(str(tmp_path / "other"), 8787, False)
    window.diagnostics.events.put(DiagnosticResult(0, parameters, Path(parameters.workspace), report()))
    window._poll_diagnostics()
    assert not starts and window._diagnostic_cache is None


def test_main_abandons_readonly_check_without_claiming_syscall_cancelled(monkeypatch, tmp_path, capsys):
    entered, release = threading.Event(), threading.Event()

    def checker(*args):
        entered.set()
        assert release.wait(2)
        return report()

    window, starts = window_fixture(tmp_path, checker)
    joins = []
    window.controller.join = lambda timeout: joins.append(timeout)
    window.show = lambda: None
    window.diagnostics.request(window._diagnostic_parameters())
    assert entered.wait(2)
    monkeypatch.setattr("collection_context.native_desktop._DesktopWindow", lambda *args: window)
    try:
        assert main(["--workspace", str(tmp_path)]) == 0
        assert window.diagnostics.active and not starts and joins == [5]
        assert "系统调用尚未确认返回" in capsys.readouterr().err
    finally:
        release.set()
        finished(window.diagnostics)
