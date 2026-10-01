"""Offline fixed-catalog desktop installation; no Tk window, network, or private config."""

from __future__ import annotations

import queue
import threading
import types
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.native_desktop import (
    DesktopController,
    DesktopDiagnostics,
    DesktopInstallation,
    DiagnosticParameters,
    _DesktopWindow,
    main,
    runtime_catalog_description,
)

SECRET = "synthetic_private_error_do_not_display"
ARTIFACT = {
    "id": "synthetic-fixed-browser",
    "name": "合成可见来源浏览器",
    "state": "available",
    "host_system": "Synthetic",
    "host_arch": "test",
    "download_bytes": 123456,
    "source_url": "https://example.invalid/fixed-catalog-package.zip",
    "license_notice": "合成许可说明，仅供无网络测试",
    "functional_verified": False,
}


def catalog(state="available"):
    return {"artifacts": [{**ARTIFACT, "state": state}], "not_available": ["ocr_weights", "Linux"]}


def events(controller):
    result = []
    while True:
        try:
            result.append(controller.events.get_nowait())
        except queue.Empty:
            return result


def installation(tmp_path, monkeypatch, installer, *, options=None, operation_lock=None):
    monkeypatch.setattr(
        "collection_context.native_desktop.default_workspace", lambda: tmp_path / "product" / "library"
    )
    return DesktopInstallation(
        installer=installer, options_checker=options or catalog, operation_lock=operation_lock
    )


@pytest.mark.parametrize("confirmation", [False, None, 1, "true"])
def test_construction_and_non_boolean_confirmation_do_not_download(tmp_path, monkeypatch, confirmation):
    controller = installation(tmp_path, monkeypatch, lambda *a, **kw: pytest.fail("unexpected install"))
    assert controller.state == "idle" and not controller.active
    with pytest.raises(ContextError) as caught:
        controller.start(
            tmp_path / "selected-library", artifact_id=ARTIFACT["id"], installation_confirmed=confirmation
        )
    assert caught.value.code == "runtime_install_confirmation"
    assert not controller.active and not list(tmp_path.iterdir())


def test_explicit_single_item_uses_fixed_runtime_and_selected_library_off_tk(tmp_path, monkeypatch):
    calls = []

    def install(runtime_dir, **kw):
        calls.append((threading.current_thread(), runtime_dir, kw))
        return {"state": "installed", "static_verified": True, "functional_verified": True}

    controller = installation(tmp_path, monkeypatch, install)
    workspace = tmp_path / "new selected library"
    controller.start(workspace, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    controller.join(2)
    assert not controller.active and controller.state == "installed"
    assert controller._worker is not None and controller._worker.daemon is False
    thread, runtime_dir, kwargs = calls[0]
    assert thread is not threading.main_thread()
    assert runtime_dir == tmp_path / "product" / "runtime"
    assert kwargs == {
        "library_dir": workspace,
        "artifact_id": ARTIFACT["id"],
        "installation_confirmed": True,
        "stop": controller.stop_event,
    }
    message = events(controller)[-1].message
    assert "静态校验" in message and "均未验证" in message
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("state", ["host_not_available", "sdk_missing", "sdk_version_mismatch"])
def test_unsupported_host_never_reaches_installer(tmp_path, monkeypatch, state):
    controller = installation(
        tmp_path,
        monkeypatch,
        lambda *a, **kw: pytest.fail("unsupported download"),
        options=lambda: catalog(state),
    )
    controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    controller.join(2)
    assert events(controller)[-1].code == "runtime_install_unavailable"
    assert not controller.active and not list(tmp_path.iterdir())


def test_arbitrary_url_cannot_select_installation(tmp_path, monkeypatch):
    controller = installation(tmp_path, monkeypatch, lambda *a, **kw: pytest.fail("custom URL download"))
    controller.start(
        tmp_path, artifact_id="https://example.invalid/arbitrary.zip", installation_confirmed=True
    )
    controller.join(2)
    assert events(controller)[-1].code == "runtime_install_catalog"
    with pytest.raises(TypeError):
        controller.start(
            tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True, runtime_dir=tmp_path
        )


@pytest.mark.parametrize(
    "failure",
    [RuntimeError(SECRET), ContextError(SECRET, SECRET), ContextError("runtime_download_failed", SECRET)],
)
def test_install_errors_are_fixed_and_do_not_leak(tmp_path, monkeypatch, failure, capsys):
    def install(*a, **kw):
        raise failure

    controller = installation(tmp_path, monkeypatch, install)
    controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    controller.join(2)
    result = events(controller)
    assert result[-1].state == "failed" and SECRET not in repr(result)
    assert capsys.readouterr() == ("", "") and not list(tmp_path.iterdir())


def test_cancellation_waits_for_real_non_daemon_thread_instead_of_claiming_download_cancelled(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    def install(*a, **kw):
        entered.set()
        assert release.wait(3)
        assert kw["stop"].is_set()
        raise ContextError("runtime_install_cancelled", SECRET)

    controller = installation(tmp_path, monkeypatch, install)
    controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    try:
        assert entered.wait(2)
        controller.request_stop()
        controller.join(0.01)
        assert controller.active and controller.state == "stopping"
        assert "底层下载可能仍在等待" in events(controller)[-1].message
        with pytest.raises(ContextError):
            controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    finally:
        release.set()
        controller.join(2)
    assert not controller.active and controller.state == "cancelled"
    assert SECRET not in repr(events(controller))


@pytest.mark.parametrize("first", ["service", "installation"])
def test_shared_lock_excludes_service_and_installation_in_both_directions(tmp_path, monkeypatch, first):
    entered, release = threading.Event(), threading.Event()
    operation_lock = threading.Lock()

    def service(*a, **kw):
        entered.set()
        assert release.wait(3)
        return 0

    def install(*a, **kw):
        entered.set()
        assert release.wait(3)
        return {"state": "installed", "static_verified": True}

    desktop = DesktopController(launch_service=service, operation_lock=operation_lock)
    runtime = installation(tmp_path, monkeypatch, install, operation_lock=operation_lock)
    try:
        if first == "service":
            desktop.start(tmp_path, port=8787, initialize_empty=False)
            assert entered.wait(2)
            with pytest.raises(ContextError) as caught:
                runtime.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
        else:
            runtime.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
            assert entered.wait(2)
            with pytest.raises(ContextError) as caught:
                desktop.start(tmp_path, port=8787, initialize_empty=False)
        assert caught.value.code == "desktop_operation_busy"
    finally:
        desktop.request_stop()
        runtime.request_stop()
        release.set()
        desktop.join(2)
        runtime.join(2)
    assert not desktop.active and not runtime.active
    assert operation_lock.acquire(blocking=False)
    operation_lock.release()


def test_catalog_lookup_stays_on_readonly_diagnostic_worker(tmp_path):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def options():
        seen.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return catalog()

    diagnostics = DesktopDiagnostics(checker=lambda *a: {"original": True}, options_checker=options)
    diagnostics.request(DiagnosticParameters(str(tmp_path), 8787, False))
    try:
        assert entered.wait(2) and diagnostics.active
        assert seen[0] is not threading.main_thread()
        diagnostics.cancel()
        assert diagnostics.active
    finally:
        release.set()
        assert diagnostics._worker is not None
        diagnostics._worker.join(2)
    assert diagnostics.events.empty() and not list(tmp_path.iterdir())


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


def ui_fixture(tmp_path):
    window = _DesktopWindow.__new__(_DesktopWindow)
    calls = []
    parameters = DiagnosticParameters(str(tmp_path / "selected-library"), 8787, False)
    window.controller = types.SimpleNamespace(active=False, events=queue.Queue(), request_stop=lambda: None)
    window.installation = types.SimpleNamespace(
        active=False,
        events=queue.Queue(),
        start=lambda *a, **kw: calls.append((a, kw)),
        request_stop=lambda: None,
    )
    window.diagnostics = types.SimpleNamespace(active=False, events=queue.Queue(), cancel=lambda: None)
    window._diagnostic_parameters = lambda: parameters
    window._diagnostic_cache = None
    window._start_intent = None
    window._install_generation = 3
    window.close_requested = False
    window.destroyed = False
    window.token_window = None
    window.install_window = None
    window.status = Variable("")
    window.permission_variables = tuple(Variable(False) for _ in range(4))
    window.messagebox = types.SimpleNamespace(askyesno=lambda *a, **kw: True)
    window.root = types.SimpleNamespace(after=lambda *a: None, destroy=lambda: calls.append("destroy"))
    for name in (
        "path_entry",
        "choose_button",
        "init_check",
        "port_entry",
        "start_button",
        "stop_button",
        "install_button",
    ):
        setattr(window, name, Widget())
    window.permission_checks = [Widget() for _ in range(4)]
    return window, calls, parameters


@pytest.mark.parametrize("action", ["workspace", "stop", "close"])
def test_nested_install_confirmation_cannot_authorize_obsolete_workspace_or_intent(tmp_path, action):
    window, calls, parameters = ui_fixture(tmp_path)

    def confirm(*a, **kw):
        if action == "workspace":
            window._diagnostic_parameters = lambda: DiagnosticParameters(str(tmp_path / "other"), 8787, False)
        else:
            getattr(window, "_" + action)()
        return True

    window.messagebox.askyesno = confirm
    window._confirm_installation(ARTIFACT, parameters, 3)
    assert not calls and not list(tmp_path.iterdir())


def test_single_confirmation_forwards_catalog_id_but_never_changes_four_permissions(tmp_path):
    window, calls, parameters = ui_fixture(tmp_path)
    window._confirm_installation(ARTIFACT, parameters, 3)
    assert calls == [
        ((Path(parameters.workspace),), {"artifact_id": ARTIFACT["id"], "installation_confirmed": True})
    ]
    assert [variable.get() for variable in window.permission_variables] == [False] * 4
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("picker_open", [False, True])
def test_cancel_confirmation_stays_with_picker_and_preserves_selection(tmp_path, picker_open):
    window, calls, parameters = ui_fixture(tmp_path)
    picker = object() if picker_open else None
    window.install_window = picker
    observed = []

    def cancel(*args, **kwargs):
        observed.append(kwargs["parent"])
        return False

    window.messagebox.askyesno = cancel
    window._confirm_installation(ARTIFACT, parameters, 3)
    assert observed == [picker if picker_open else window.root]
    assert window.install_window is picker and window._install_generation == 3
    assert not calls and not list(tmp_path.iterdir())
    assert [variable.get() for variable in window.permission_variables] == [False] * 4


def test_installing_disables_selection_and_start_and_keeps_stop_available(tmp_path):
    window, _, _ = ui_fixture(tmp_path)
    window.installation.active = True
    window._controls(False)
    assert window.path_entry.state == window.choose_button.state == window.start_button.state == "disabled"
    assert window.install_button.state == "disabled" and window.stop_button.state == "normal"
    assert all(widget.state == "disabled" for widget in window.permission_checks)


def test_close_does_not_destroy_window_until_real_install_controller_reports_inactive(tmp_path):
    window, calls, _ = ui_fixture(tmp_path)
    window.installation.active = True
    window._close()
    window._poll()
    assert not calls and not window.destroyed
    window.installation.active = False
    window._poll()
    assert calls == ["destroy"] and window.destroyed


def test_main_exit_waits_for_install_and_reports_pending_not_success(tmp_path, monkeypatch, capsys):
    calls = []
    service = types.SimpleNamespace(
        active=False,
        request_stop=lambda: calls.append("service-stop"),
        join=lambda timeout: calls.append(("service-join", timeout)),
    )
    runtime = types.SimpleNamespace(
        active=True,
        request_stop=lambda: calls.append("install-stop"),
        join=lambda timeout: calls.append(("install-join", timeout)),
    )

    def recover():
        calls.append("recover")
        raise RuntimeError(SECRET)

    window = types.SimpleNamespace(
        controller=service, installation=runtime, show=lambda: None, recover_stopping=recover
    )
    monkeypatch.setattr("collection_context.native_desktop._DesktopWindow", lambda *a: window)
    assert main(["--workspace", str(tmp_path)]) == 3
    assert calls == ["service-stop", ("service-join", 5), "install-stop", ("install-join", 5), "recover"]
    output = capsys.readouterr()
    assert "安装线程" in output.err and SECRET not in output.err


def test_dynamic_catalog_description_lists_name_state_size_source_license_and_missing_items():
    text = runtime_catalog_description(catalog())
    for value in [
        ARTIFACT["name"],
        "123,456",
        ARTIFACT["source_url"],
        ARTIFACT["license_notice"],
        "OCR 识别权重",
        "Linux 固定安装包",
        "功能未验证",
    ]:
        assert value in text
    assert "静态安装成功仍不等于登录" in text


@pytest.mark.parametrize("kind", ["service", "installation"])
@pytest.mark.parametrize("failure_point", ["construct", "start"])
def test_thread_allocation_failure_releases_shared_operation_lock(
    tmp_path, monkeypatch, kind, failure_point, capsys
):
    operation_lock = threading.Lock()

    class BrokenThread:
        def __init__(self, **kwargs):
            if failure_point == "construct":
                raise RuntimeError(SECRET)

        def start(self):
            raise RuntimeError(SECRET)

        def is_alive(self):
            return False

    monkeypatch.setattr("collection_context.native_desktop.threading.Thread", BrokenThread)
    if kind == "service":
        controller = DesktopController(operation_lock=operation_lock)
        with pytest.raises(ContextError) as caught:
            controller.start(tmp_path, port=8787, initialize_empty=False)
        assert SECRET not in str(caught.value)
        assert controller.state == "failed" and not controller.active
    else:
        controller = installation(
            tmp_path,
            monkeypatch,
            lambda *a, **kw: pytest.fail("installation started"),
            operation_lock=operation_lock,
        )
        controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
        assert controller.state == "failed" and SECRET not in repr(events(controller))
    assert operation_lock.acquire(blocking=False)
    operation_lock.release()
    assert capsys.readouterr() == ("", "")


def test_publication_outcome_unknown_does_not_claim_nothing_was_published(tmp_path, monkeypatch):
    def install(*a, **kw):
        raise ContextError("runtime_install_outcome_unknown", SECRET)

    controller = installation(tmp_path, monkeypatch, install)
    controller.start(tmp_path, artifact_id=ARTIFACT["id"], installation_confirmed=True)
    controller.join(2)
    event = events(controller)[-1]
    assert event.code == "runtime_install_outcome_unknown"
    assert "未能确认" in event.message and "未发布" not in event.message
    assert SECRET not in event.message
