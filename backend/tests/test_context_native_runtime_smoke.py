"""Verification reports must distinguish visible rendering from platform login."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def helper():
    path = Path(__file__).resolve().parents[1] / "tools/native_distribution/runtime_smoke.py"
    spec = importlib.util.spec_from_file_location("runtime_smoke_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("headed", [False, True])
def test_explicit_mode_selects_catalog_and_matching_scope(tmp_path, monkeypatch, headed):
    module = helper()
    binary = tmp_path / "binary"
    binary.write_bytes(b"inert non-executable fixture")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runtime-dependencies.json").write_text("{}")
    calls = []

    def run(binary, arguments, *, stage, environment):
        assert environment["PATH"] == "" and Path(environment["HOME"]).parent == stage
        calls.append(arguments)
        if "runtime-options" in arguments:
            return {
                "artifacts": [
                    {"id": "chromium-macos-arm64-1243", "state": "available"},
                    {"id": "chromium-headless-macos-arm64-1243", "state": "available"},
                ]
            }
        assert ("--headed" in arguments) == headed
        return {
            "functional_verified": True,
            "browser_closed": True,
            "verification_scope": "owned_headed_local_fixture_only"
            if headed
            else "owned_headless_local_fixture_only",
            "sync_verified": False,
            "platform_login_verified": False,
        }

    monkeypatch.setattr(module, "run", run)
    report = module.exercise(binary, runtime, tmp_path / "new-output", headed=headed)
    assert report["state"] == "passed" and len(calls) == 2
    assert report["verification_scope"] == (
        "frozen_headed_local_fixture_only" if headed else "frozen_headless_local_fixture_only"
    )
    assert report["installation_receipt_unchanged"] and report["model_requests"] == 0


def test_headless_result_cannot_pass_visible_probe(tmp_path, monkeypatch):
    module = helper()
    binary = tmp_path / "binary"
    binary.write_bytes(b"inert")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runtime-dependencies.json").write_text("{}")

    def run(binary, arguments, **kwargs):
        if "runtime-options" in arguments:
            return {"artifacts": [{"id": "chromium-macos-arm64-1243", "state": "available"}]}
        return {
            "functional_verified": True,
            "browser_closed": True,
            "verification_scope": "owned_headless_local_fixture_only",
        }

    monkeypatch.setattr(module, "run", run)
    output = tmp_path / "new-output"
    with pytest.raises(AssertionError):
        module.exercise(binary, runtime, output, headed=True)
    assert json.loads((output / "report.json").read_text())["state"] == "failed"
