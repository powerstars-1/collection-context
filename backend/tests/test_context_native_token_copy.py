"""Token-copy UI contracts with a fake local SDK, no real system clipboard."""

from __future__ import annotations

import sys
import threading
import types

import pytest

from collection_context.application.contracts import ContextError
from collection_context.interfaces.access import AccessRegistry
from collection_context.launcher import _ensure_owner_access, _prepare_workspace
from collection_context.native_bootstrap import _TokenWindow, present_owner_token

TOKEN = "scc_" + "synthetic-test-not-a-real-key" * 2


class FakeVariable:
    def __init__(self, master=None, value=""):
        self.value = value
        self.fail_write = False

    def set(self, value):
        if self.fail_write:
            raise RuntimeError(TOKEN)
        self.value = value

    def get(self):
        return self.value


class FakeWidget:
    def __init__(self, master=None, **kwargs):
        self.options = kwargs

    def pack(self, **kwargs):
        pass

    def focus_set(self):
        pass


class FakeRoot:
    def __init__(self):
        self.clipboard_calls = []
        self.clipboard = "other clipboard content"
        self.protocols = {}
        self.destroyed = False
        self.on_loop = lambda: None
        self.fail_clipboard_at = None

    def title(self, text):
        pass

    def geometry(self, size):
        pass

    def resizable(self, *args):
        pass

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def mainloop(self):
        self.on_loop()

    def transient(self, parent):
        pass

    def wait_window(self, child):
        child.on_loop()

    def destroy(self):
        self.destroyed = True

    def clipboard_clear(self):
        self.clipboard_calls.append("clear")
        if self.fail_clipboard_at == "clear":
            raise RuntimeError(TOKEN)
        self.clipboard = ""

    def clipboard_append(self, text):
        self.clipboard_calls.append("append")
        if self.fail_clipboard_at == "append":
            raise RuntimeError(TOKEN)
        self.clipboard += text


@pytest.fixture
def toolkit(monkeypatch):
    roots = []
    widgets = []

    def root_factory(*args):
        root = FakeRoot()
        roots.append(root)
        return root

    def widget_factory(kind):
        def factory(*args, **kwargs):
            widget = FakeWidget(*args, **kwargs)
            widget.kind = kind
            widgets.append(widget)
            return widget

        return factory

    ttk = types.SimpleNamespace(
        **{name: widget_factory(name) for name in ["Frame", "Label", "Entry", "Button", "Checkbutton"]}
    )
    tk = types.SimpleNamespace(
        Tk=root_factory, Toplevel=root_factory, StringVar=FakeVariable, BooleanVar=FakeVariable, ttk=ttk
    )
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    return types.SimpleNamespace(roots=roots, widgets=widgets)


def button(toolkit, text):
    return next(
        widget.options["command"]
        for widget in toolkit.widgets
        if widget.kind == "Button" and widget.options["text"] == text
    )


def saved_variable(toolkit):
    return next(widget.options["variable"] for widget in toolkit.widgets if widget.kind == "Checkbutton")


def test_readonly_selection_and_risk_notice_do_not_export(toolkit, capsys):
    window = _TokenWindow()
    assert window.entry.options["state"] == "readonly"
    assert window.entry.options["exportselection"] is False
    notices = [widget.options.get("text", "") for widget in toolkit.widgets if widget.kind == "Label"]
    assert any("剪贴板历史" in text and "跨设备同步" in text for text in notices)
    assert not toolkit.roots[0].clipboard_calls
    window.root.on_loop = window.clear_and_close
    assert window.show(TOKEN) is False
    assert window.secret.get() == "" and window.root.destroyed
    assert not window.root.clipboard_calls and capsys.readouterr() == ("", "")


def test_copy_button_requires_click_and_does_not_check_saved(toolkit, capsys):
    window = _TokenWindow()
    root = window.root

    def interaction():
        assert root.clipboard_calls == []
        assert button(toolkit, "复制口令到本机剪贴板")() is True
        assert root.clipboard == TOKEN and root.clipboard_calls == ["clear", "append"]
        assert saved_variable(toolkit).get() is False
        assert TOKEN not in window.copy_status.get()
        button(toolkit, "已保存，继续")()
        assert not root.destroyed and not window.result
        button(toolkit, "取消（撤销本次新权限）")()

    root.on_loop = interaction
    assert window.show(TOKEN) is False
    assert root.clipboard_calls == ["clear", "append"]
    assert root.clipboard == TOKEN  # Closing neither auto-clears nor claims history cleanup.
    assert capsys.readouterr() == ("", "")


def test_confirm_saved_without_click_never_copies(toolkit):
    window = _TokenWindow()

    def interaction():
        saved_variable(toolkit).set(True)
        button(toolkit, "已保存，继续")()

    window.root.on_loop = interaction
    assert present_owner_token(TOKEN, window_factory=lambda: window) is True
    assert window.root.clipboard_calls == []
    assert window.secret.get() == "" and window.root.destroyed


@pytest.mark.parametrize("failure_at", ["clear", "append"])
def test_copy_failure_feedback_is_fixed_sanitized_and_not_saved(toolkit, failure_at, capsys):
    window = _TokenWindow()
    window.secret.set(TOKEN)
    window.root.fail_clipboard_at = failure_at
    assert button(toolkit, "复制口令到本机剪贴板")() is False
    assert window.copy_status.get() == "复制未能确认完成，请自行选择口令并保存；未显示内部异常。"
    assert not saved_variable(toolkit).get() and not window.result
    assert capsys.readouterr() == ("", "")
    window.clear_and_close()


def test_copy_feedback_failure_does_not_log_secret(toolkit, capsys):
    window = _TokenWindow()
    window.secret.set(TOKEN)
    window.copy_status.fail_write = True
    window.root.fail_clipboard_at = "append"
    assert button(toolkit, "复制口令到本机剪贴板")() is False
    assert capsys.readouterr() == ("", "")
    window.clear_and_close()


def test_copy_cannot_run_off_main_thread_or_after_close(toolkit):
    window = _TokenWindow()
    window.secret.set(TOKEN)
    results = []
    worker = threading.Thread(target=lambda: results.append(button(toolkit, "复制口令到本机剪贴板")()))
    worker.start()
    worker.join(2)
    assert results == [False] and not window.root.clipboard_calls
    window.clear_and_close()
    assert button(toolkit, "复制口令到本机剪贴板")() is False
    assert not window.root.clipboard_calls


def test_parent_window_uses_same_click_boundary(toolkit):
    parent = FakeRoot()
    window = _TokenWindow(parent=parent)

    def interaction():
        assert not window.root.clipboard_calls
        button(toolkit, "复制口令到本机剪贴板")()
        window.root.protocols["WM_DELETE_WINDOW"]()

    window.root.on_loop = interaction
    assert present_owner_token(TOKEN, window_factory=lambda: window) is False
    assert window.root.clipboard_calls == ["clear", "append"]
    assert window.root.destroyed and not parent.clipboard_calls


def test_cancelling_after_user_copy_revokes_only_fresh_permission(toolkit, tmp_path, capsys):
    import io

    store, _ = _prepare_workspace(tmp_path / "synthetic-copy-workspace", initialize_empty=True)
    registry = AccessRegistry(store)
    existing = registry.create("existing-readonly")
    before = registry.records()
    shown = []
    output = io.StringIO()

    def present(token):
        shown.append(token)
        window = _TokenWindow()

        def interaction():
            assert button(toolkit, "复制口令到本机剪贴板")() is True
            button(toolkit, "取消（撤销本次新权限）")()

        window.root.on_loop = interaction
        return present_owner_token(token, window_factory=lambda: window)

    try:
        with pytest.raises(ContextError) as caught:
            _ensure_owner_access(store, output, owner_presenter=present)
        assert caught.value.code == "owner_confirmation_cancelled"
        assert registry.records() == before
        assert registry.authenticate(existing["token"]).principal == existing["principal"]
        with pytest.raises(ContextError):
            registry.authenticate(shown[0])
        assert toolkit.roots[-1].clipboard == shown[0]  # Revoked, not silently exported/removed again.
        assert shown[0] not in output.getvalue()
        assert capsys.readouterr() == ("", "")
    finally:
        store.close()
