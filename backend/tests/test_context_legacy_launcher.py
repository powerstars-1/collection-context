"""Explicit legacy desktop selection, source preservation and real local shutdown."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import time
import types
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from collection_context.application import legacy_desktop
from collection_context.application.contracts import ContextError
from collection_context.application.launcher_capabilities import LauncherCapabilities
from collection_context.launcher import launch, main
from collection_context.native_desktop import (
    DesktopController,
    DesktopDiagnostics,
    DiagnosticParameters,
    _DesktopWindow,
)


def old_library(root):
    inbox = root / "00_素材收件箱/抖音"
    inbox.mkdir(parents=True)
    (inbox / "原创教程.md").write_text(
        "# 原创纸飞机\n\n## 基本信息\n- 平台: 抖音\n"
        "- 链接: https://www.douyin.com/video/123\n- 来源: 收藏\n\n"
        "## 原始材料\n纸飞机按虚线折叠，翼尖留白24px。\n",
        encoding="utf-8",
    )
    return root


def tree(root):
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_legacy_diagnose_invalid_source_is_failure_without_creating_anything(tmp_path, capsys):
    source = tmp_path / "未创建"
    assert main(["--workspace", str(source), "--legacy-vault", "--diagnose", "--port", str(free_port())]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["workspace"]["state"] == "legacy_unavailable"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["initialize", "capabilities", "non_bool"])
def test_legacy_launch_rejects_conflicting_permissions_before_any_write(tmp_path, kind):
    source = old_library(tmp_path / "旧库")
    before = tree(source)
    options = {
        "port": free_port(),
        "initialize_empty": kind == "initialize",
        "no_browser": True,
        "output": io.StringIO(),
        "legacy_vault": "true" if kind == "non_bool" else True,
    }
    if kind == "capabilities":
        options["capabilities"] = LauncherCapabilities(source)
    with pytest.raises(ContextError) as caught:
        launch(source, **options)
    assert caught.value.code == "launcher_capabilities_invalid"
    assert tree(source) == before


def test_real_legacy_launch_login_read_setup_and_ordered_stop_preserve_source(tmp_path, monkeypatch):
    source = old_library(tmp_path / "旧资料 中文空格")
    before = tree(source)
    monkeypatch.setattr(legacy_desktop, "default_workspace", lambda: tmp_path / "产品/workspace")
    for name in ("launcher_resources", "_attach_execution_lifecycle"):
        monkeypatch.setattr(
            "collection_context.launcher." + name, lambda *a, **k: pytest.fail("No processing")
        )
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    stop = threading.Event()
    output = io.StringIO()
    tokens, outcomes = [], []

    def accept(token):
        tokens.append(token)
        return True

    def run():
        try:
            outcomes.append(
                launch(
                    source,
                    port=port,
                    initialize_empty=False,
                    no_browser=True,
                    output=output,
                    owner_presenter=accept,
                    stop_event=stop,
                    legacy_vault=True,
                )
            )
        except BaseException as error:
            outcomes.append(error)

    worker = threading.Thread(target=run, name="legacy-launch-offline-test")
    worker.start()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(path, data=None, **headers):
        payload = json.dumps(data).encode() if data is not None else None
        with client.open(
            urllib.request.Request(
                origin + path,
                data=payload,
                headers={"Content-Type": "application/json", "Origin": origin, **headers},
            ),
            timeout=3,
        ) as response:
            return json.load(response)

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker.is_alive():
            try:
                if request("/health")["ok"]:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.025)
        else:
            pytest.fail("Legacy server was not ready; no sensitive output retained")
        session = request("/v1/session", {"token": tokens[0]})["data"]
        assert session["library_mode"] == "legacy_readonly"
        assert set(session["permissions"]) == {"collections:read", "ui:view"}
        setup = request("/v1/agent-setup")["data"]
        assert setup["library_mode"] == "legacy_readonly"
        args = setup["mcp"]["configuration"]["mcpServers"]["collection-context"]["args"]
        assert args[-3:] == ["--workspace", str(source), "--legacy-vault"]
        assert setup["http"]["base_url"] == origin
        csrf = {"X-CSRF-Token": session["csrf_token"]}
        result = request("/v1/collections/search", {"query": "24px"}, **csrf)["data"]
        ref = result["items"][0]["material_ref"]
        assert (
            "24px"
            in request("/v1/collections/read", {"material_ref": ref, "artifact": "original"}, **csrf)["data"][
                "text"
            ]
        )
        assert request(f"/v1/collections/{ref}/status")["ok"]
    finally:
        stop.set()
        worker.join(10)
    assert not worker.is_alive() and outcomes == [0]
    assert tree(source) == before
    assert "scc_" not in output.getvalue()
    assert not (source / ".context").exists()


def test_first_owner_cancel_after_source_moves_revokes_only_unconfirmed_access(tmp_path, monkeypatch):
    source = old_library(tmp_path / "旧资料")
    before = tree(source)
    moved = tmp_path / "暂离的旧资料"
    monkeypatch.setattr(legacy_desktop, "default_workspace", lambda: tmp_path / "产品/workspace")
    output = io.StringIO()

    def cancel(token):
        source.rename(moved)
        return False

    try:
        with pytest.raises(ContextError) as caught:
            launch(
                source,
                port=free_port(),
                initialize_empty=False,
                no_browser=True,
                output=output,
                owner_presenter=cancel,
                legacy_vault=True,
            )
        assert caught.value.code == "owner_confirmation_cancelled"
        assert "scc_" not in output.getvalue()
    finally:
        moved.rename(source)
    store, created = legacy_desktop.prepare_legacy_access(source)
    try:
        from collection_context.interfaces.access import AccessRegistry

        assert not created and AccessRegistry(store).records() == []
    finally:
        store.close()
    assert tree(source) == before


def test_desktop_legacy_diagnostics_do_not_inspect_processing_components(tmp_path):
    diagnostics = DesktopDiagnostics(
        checker=lambda *a: pytest.fail("Managed diagnostic must not run"),
        legacy_checker=lambda *a: {"workspace": {"state": "legacy_readonly"}},
        options_checker=lambda: pytest.fail("Legacy does not inspect install catalog"),
    )
    diagnostics.request(DiagnosticParameters(str(tmp_path), 8787, False, legacy_readonly=True))
    event = diagnostics.events.get(timeout=2)
    assert event.report == {"workspace": {"state": "legacy_readonly"}}
    assert event.runtime_options is None
    assert not list(tmp_path.iterdir())


def test_controller_forwards_legacy_only_without_building_capabilities(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "collection_context.application.launcher_capabilities.default_desktop_capabilities",
        lambda *a, **k: pytest.fail("No processing authority"),
    )
    controller = DesktopController(launch_service=lambda *a, **k: calls.append((a, k)) or 0)
    controller.start(tmp_path, port=8787, initialize_empty=False, legacy_readonly=True)
    controller.join(2)
    assert not controller.active and calls[0][1]["legacy_vault"] is True
    assert "capabilities" not in calls[0][1]
    with pytest.raises(ContextError):
        controller.start(tmp_path, port=8787, initialize_empty=True, legacy_readonly=True)


@pytest.mark.parametrize("action", ["accept", "cancel", "change_mode", "initialize"])
def test_legacy_ui_requires_current_explicit_readonly_confirmation(tmp_path, action):
    window = _DesktopWindow.__new__(_DesktopWindow)
    calls = []
    value = {"legacy": True}
    window.legacy_readonly = types.SimpleNamespace(get=lambda: value["legacy"])
    window.controller = types.SimpleNamespace(active=False, start=lambda *a, **k: calls.append((a, k)))
    window.close_requested = False
    window._refresh_dependencies = lambda: {
        "capabilities": {"ready_for_management_page": True},
        "listen": {"available": True},
        "workspace": {"state": "legacy_readonly"},
    }
    window.status = types.SimpleNamespace(set=lambda text: None)
    window.initialize = types.SimpleNamespace(get=lambda: action == "initialize")
    window._parameters = lambda: (tmp_path, 8787)
    window._controls = lambda active: None
    window.root = object()

    def confirm(*a, **kw):
        if action == "change_mode":
            value["legacy"] = False
        return action != "cancel"

    window.messagebox = types.SimpleNamespace(askyesno=confirm)
    window._start()
    assert bool(calls) is (action == "accept")
    if calls:
        assert calls[0][1] == {"port": 8787, "initialize_empty": False, "legacy_readonly": True}
    assert not list(tmp_path.iterdir())
