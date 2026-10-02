"""Actual Tk widgets and restored selection, not human/frozen-app acceptance.

Uses only an isolated product-preference directory and nonexistent synthetic
library locations. Does not Start, initialize a library, log in or call models.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from collection_context import native_desktop
from collection_context.application.desktop_preferences import DesktopSelection


def run(output: Path) -> Path:
    stage = Path(tempfile.mkdtemp(prefix="desktop-selection-", dir=output))
    source = stage / "中文 空格" / "离线旧库"
    product = stage / "product"
    native_desktop.default_workspace = lambda: product / "workspace"
    window = native_desktop._DesktopWindow(product / "workspace", 8791)
    window.root.update()
    assert window.root.winfo_ismapped()
    window.legacy_check.invoke()  # Actual Tk class binding + registered callback.
    window.root.update()
    assert window.legacy_readonly.get() is True
    assert str(window.init_check.cget("state")) == "disabled"
    assert all(str(widget.cget("state")) == "disabled" for widget in window.permission_checks)
    assert str(window.install_button.cget("state")) == "disabled"
    window.legacy_check.invoke()
    window.root.update()
    assert window.legacy_readonly.get() is False
    assert str(window.init_check.cget("state")) == "normal"
    assert not window.controller.active and not product.exists() and not source.exists()
    selection = DesktopSelection(source, 8792, True)
    failures = []

    def save():
        try:
            window.preferences.save(selection)
        except BaseException:
            failures.append("save_failed")

    saver = threading.Thread(target=save)
    saver.start()
    saver.join(5)
    assert not saver.is_alive() and not failures
    window._close()
    window.root.update()
    assert not window.controller.active and not window._installation_active()
    if not window.destroyed:
        window.root.destroy()
        window.destroyed = True

    reopened = native_desktop._DesktopWindow(product / "workspace", 8787)
    try:
        reopened.restore_selection(workspace=True, port=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reopened.root.update()
            if reopened.workspace.get() == str(source):
                break
            time.sleep(0.01)
        assert reopened.workspace.get() == str(source)
        assert reopened.port.get() == "8792" and reopened.legacy_readonly.get() is True
        assert reopened.initialize.get() is False
        assert not any(variable.get() for variable in reopened.permission_variables)
        assert str(reopened.init_check.cget("state")) == "disabled"
        assert str(reopened.install_button.cget("state")) == "disabled"
        assert not reopened.controller.active and not source.exists()
        result = {
            "state": "passed",
            "actual_tk_window_mapped": True,
            "actual_checkbox_callback_and_disabled_controls": True,
            "restored_location_port_readonly": True,
            "permissions_restored": False,
            "initialized_source": False,
            "started_service": False,
            "model_requests": 0,
            "platform_requests": 0,
            "verification_scope": "source_Tk_callback_not_physical_click_or_frozen_distribution",
        }
    finally:
        reopened._close()
        reopened.root.update()
        if not reopened.destroyed:
            reopened.root.destroy()
            reopened.destroyed = True
    report = stage / "report.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离的真实Tk选库记忆验证；不启动后台或读取私人库。")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_dir():
        parser.error("output must be an existing evidence directory")
    print(run(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
