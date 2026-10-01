"""Picker layout/focus contracts; fake widgets do not prove native GUI interaction."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from collection_context.native_desktop import _DesktopWindow, runtime_catalog_description


def options():
    return {
        "artifacts": [
            {
                "id": "fixed-media",
                "name": "原创媒体组件",
                "state": "available",
                "host_system": "Darwin",
                "host_arch": "arm64",
                "download_bytes": 0,
                "archive_bytes": 32_134_093,
                "payload_bytes": 53_818_344,
                "delivery": "bundled",
                "source_url": "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
                "license_notice": "原创许可提示，发行审查未完成",
            },
            {"id": "unavailable", "name": "不可安装项", "state": "host_not_available"},
        ],
        "not_available": [],
    }


def test_bundled_details_show_both_storage_sizes_and_no_binary_download_url():
    description = runtime_catalog_description({"artifacts": options()["artifacts"][:1]})
    assert "下载体积：0 字节" in description
    assert "随包归档：32,134,093 字节" in description
    assert "展开体积：53,818,344 字节" in description
    assert "随包携带，无网络下载；来源地址是上游源码" in description
    assert "功能未验证" in description


def picker(monkeypatch):
    widgets = []
    focus = []

    class Widget:
        def __init__(self, kind, *args, **kwargs):
            self.kind, self.options = kind, kwargs
            self.bindings = {}
            self.index = -1
            widgets.append(self)

        def pack(self, **kwargs):
            self.packing = kwargs

        def configure(self, **kwargs):
            self.options.update(kwargs)

        def insert(self, *args):
            self.content = args[-1]

        def title(self, value):
            pass

        def geometry(self, value):
            pass

        def protocol(self, *args):
            pass

        def bind(self, name, callback):
            self.bindings[name] = callback

        def focus_set(self):
            focus.append(self)

        def current(self):
            return self.index

        def yview(self, *args):
            pass

        def set(self, *args):
            pass

    def factory(kind):
        return lambda *args, **kwargs: Widget(kind, *args, **kwargs)

    class Variable:
        def __init__(self, *, master, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    ttk = SimpleNamespace(
        **{name: factory(name) for name in ("Frame", "Label", "Radiobutton", "Button", "Scrollbar")}
    )
    tk = SimpleNamespace(Toplevel=factory("Toplevel"), Text=factory("Text"), StringVar=Variable, ttk=ttk)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    window = object.__new__(_DesktopWindow)
    window.root = object()
    window.close_requested = False
    window.controller = SimpleNamespace(active=False)
    window.installation = SimpleNamespace(active=False)
    window._install_generation = 17
    window.install_window = None
    parameters = object()
    window._diagnostic_cache = SimpleNamespace(runtime_options=options(), parameters=parameters)
    calls = []
    window._refresh_dependencies = lambda: None
    window._controls = lambda active: calls.append(("controls", active))
    window._confirm_installation = lambda *args: calls.append(("confirmation", *args))
    window._open_installation()
    return window, widgets, focus, calls


def test_scrollable_picker_focus_does_not_select_or_authorize(monkeypatch):
    window, widgets, focus, calls = picker(monkeypatch)
    radio = next(w for w in widgets if w.kind == "Radiobutton")
    text = next(w for w in widgets if w.kind == "Text")
    scrollbar = next(w for w in widgets if w.kind == "Scrollbar")
    button = next(w for w in widgets if w.options.get("text") == "确认所选单项安装")
    assert focus == [radio] and radio.options["variable"].get() == ""
    assert len([w for w in widgets if w.kind == "Radiobutton"]) == 2
    assert radio.options["state"] == "normal"
    assert button.options["state"] == "disabled"
    assert scrollbar.options["orient"] == "vertical"
    assert scrollbar.options["command"] == text.yview
    assert text.options["yscrollcommand"] == scrollbar.set and text.options["state"] == "disabled"
    assert calls == [("controls", False)] and window._start_intent is None


def test_only_explicit_available_selection_enables_confirmation(monkeypatch):
    window, widgets, focus, calls = picker(monkeypatch)
    radios = [w for w in widgets if w.kind == "Radiobutton"]
    variable = radios[0].options["variable"]
    button = next(w for w in widgets if w.options.get("text") == "确认所选单项安装")
    button.options["command"]()
    assert calls == [("controls", False)]
    assert radios[1].options["state"] == "disabled"
    variable.set("unavailable")
    radios[1].options["command"]()
    assert button.options["state"] == "disabled"
    button.options["command"]()
    assert calls == [("controls", False)]
    variable.set("fixed-media")
    radios[0].options["command"]()
    assert button.options["state"] == "normal"
    button.options["command"]()
    assert calls[-1][0] == "confirmation" and calls[-1][1]["id"] == "fixed-media"
    assert calls[-1][3] == 17
