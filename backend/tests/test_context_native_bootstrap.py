"""Native bootstrap boundaries, no real display, secrets, or external process."""

from __future__ import annotations

import threading

import pytest

from collection_context.application.contracts import ContextError
from collection_context.native_bootstrap import dispatch, present_owner_token


class FakeWindow:
    def __init__(self, result=True, failure=False):
        self.result = result
        self.failure = failure
        self.shown = None
        self.cleared = False

    def show(self, token):
        self.shown = token
        if self.failure:
            raise ValueError("secret exception: " + token)
        return self.result

    def clear_and_close(self):
        self.shown = None
        self.cleared = True


@pytest.mark.parametrize("accepted", [True, False, None, "true"])
def test_token_acknowledgement_is_explicit_and_cleared(accepted, capsys):
    window = FakeWindow(accepted)
    assert present_owner_token("scc_" + "x" * 40, window_factory=lambda: window) is (accepted is True)
    assert window.cleared and window.shown is None
    assert capsys.readouterr() == ("", "")


def test_token_failure_has_no_secret_exception_chain(capsys):
    token = "scc_" + "x" * 40
    window = FakeWindow(failure=True)
    with pytest.raises(ContextError) as caught:
        present_owner_token(token, window_factory=lambda: window)
    assert caught.value.code == "desktop_display_required"
    assert token not in str(caught.value) and caught.value.__suppress_context__
    assert window.cleared and capsys.readouterr() == ("", "")


def test_token_presentation_refuses_worker_thread():
    errors = []

    def worker():
        try:
            present_owner_token("scc_" + "x" * 40, window_factory=lambda: pytest.fail("not GUI thread"))
        except ContextError as error:
            errors.append(error.code)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and errors == ["desktop_display_required"]


@pytest.mark.parametrize("token", [None, "model-key", "scc_short", 123])
def test_token_shape_rejects_non_product_secret(token):
    with pytest.raises(ContextError):
        present_owner_token(token, window_factory=lambda: pytest.fail("invalid token"))


def test_no_argument_and_unknown_mode_do_not_start_or_create(capsys):
    assert dispatch([]) == 0
    assert "浏览器、FFmpeg、OCR" in capsys.readouterr().out
    assert dispatch(["shell", "anything"]) == 2
    assert "invalid_mode" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mode", "module"),
    [
        ("cli", "collection_context.cli"),
        ("mcp", "collection_context.interfaces.mcp"),
        ("web", "collection_context.interfaces.server"),
        ("launch", "collection_context.launcher"),
        ("desktop", "collection_context.native_desktop"),
    ],
)
def test_dispatch_preserves_shared_entry_arguments(mode, module, monkeypatch):
    import importlib

    observed = []
    monkeypatch.setattr(importlib.import_module(module), "main", lambda argv: observed.append(argv) or 17)
    assert dispatch([mode, "--workspace", "中文 路径", "--help"]) == 17
    assert observed == [["--workspace", "中文 路径", "--help"]]
