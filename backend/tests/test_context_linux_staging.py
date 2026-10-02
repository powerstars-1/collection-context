"""Offline clean-tree staging evidence; no containers, installs or native Linux claims."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_linux_acceptance import candidate, fixture_backend, staging

BACKEND = Path(__file__).resolve().parents[1]


def clean_fixture(tmp_path):
    backend = fixture_backend(tmp_path)
    for name in staging.TOOLS:
        source = BACKEND / "tools" / name
        destination = backend / "tools" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (backend / "tests").mkdir()
    (backend / "tests/test_context_original.py").write_text("def test_original(): pass\n")
    for name in staging.EXTRA_TESTS:
        shutil.copy2(BACKEND / "tests" / name, backend / "tests" / name)
        for tool in staging.TEST_TOOLS.get(name, ()):
            destination = backend / "tools" / tool
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BACKEND / "tools" / tool, destination)
    return backend


def stage(monkeypatch, capsys, backend, wheel, output):
    monkeypatch.setattr(staging, "__file__", str(backend / "tools/stage_linux_acceptance.py"))
    assert staging.main(["--wheel", str(wheel), "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    return Path(report["context"]), report


def test_clean_fixture_constructs_only_required_runtime_and_matching_source(tmp_path, monkeypatch, capsys):
    backend = clean_fixture(tmp_path)
    (backend / "tools/private_debug.py").write_text("PRIVATE_MARKER_NOT_COPIED")
    (backend / "app").mkdir()
    (backend / "app/legacy.py").write_text("PRIVATE_MARKER_NOT_COPIED")
    (backend / "tests/test_legacy.py").write_text("PRIVATE_MARKER_NOT_COPIED")
    wheel = candidate(tmp_path, backend)
    context, report = stage(monkeypatch, capsys, backend, wheel, tmp_path / "outputs")
    expected = {"backend/tools/" + name for name in staging.TOOLS}
    expected.add("backend/tests/test_context_original.py")
    expected.update("backend/tests/" + name for name in staging.EXTRA_TESTS)
    expected.update(
        "backend/tools/" + name for test in staging.EXTRA_TESTS for name in staging.TEST_TOOLS.get(test, ())
    )
    assert set(report["support_files"]) == expected
    assert report["native_linux_execution_verified"] is False
    assert report["includes_legacy_or_private_runtime"] is False
    for name in expected:
        assert (context / name).read_bytes() == (backend.parent / name).read_bytes()
    assert (context / wheel.name).read_bytes() == wheel.read_bytes()
    assert (context / "Dockerfile").read_bytes() == (
        backend / "tools/linux_acceptance/Dockerfile"
    ).read_bytes()
    assert not any(b"PRIVATE_MARKER_NOT_COPIED" in p.read_bytes() for p in context.rglob("*") if p.is_file())
    assert not (context / "backend/src/collection_context/.env.local").exists()
    assert not (context / "backend/app").exists()


@pytest.mark.parametrize(
    "name",
    [
        "query_smoke.py",
        "linux_acceptance/runner.py",
        "linux_acceptance/Dockerfile",
        "windows_native_acceptance.py",
    ],
)
def test_missing_required_tool_fails_before_output(tmp_path, monkeypatch, name):
    backend = clean_fixture(tmp_path)
    (backend / "tools" / name).unlink()
    wheel = candidate(tmp_path, backend)
    monkeypatch.setattr(staging, "__file__", str(backend / "tools/stage_linux_acceptance.py"))
    output = tmp_path / "must-not-exist"
    with pytest.raises(FileNotFoundError):
        staging.main(["--wheel", str(wheel), "--output", str(output)])
    assert not output.exists()


@pytest.mark.parametrize("kind", ["tool", "tool_parent", "test", "hardlink"])
def test_unsafe_selected_inputs_fail_without_reading_or_copying(tmp_path, monkeypatch, kind):
    backend = clean_fixture(tmp_path)
    path = backend / ("tests/test_context_original.py" if kind == "test" else "tools/query_smoke.py")
    if kind == "tool_parent":
        path = backend / "tools/linux_acceptance"
    retained = tmp_path / "original-retained"
    path.rename(retained)
    if kind == "hardlink":
        os.link(retained, path)
    else:
        path.symlink_to(retained, target_is_directory=kind == "tool_parent")
    wheel = candidate(tmp_path, backend)
    output = tmp_path / "must-not-exist"
    monkeypatch.setattr(staging, "__file__", str(backend / "tools/stage_linux_acceptance.py"))
    with pytest.raises(ValueError, match="aliases|single-link"):
        staging.main(["--wheel", str(wheel), "--output", str(output)])
    assert not output.exists() and retained.exists()


def test_static_ui_and_unit_tool_dependencies_are_explicit_and_complete(tmp_path):
    backend = clean_fixture(tmp_path)
    for test_name, names in staging.TEST_TOOLS.items():
        (backend / "tests" / test_name).write_text("# original dependency marker\n")
        for name in names:
            target = backend / "tools" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BACKEND / "tools" / name, target)
    (backend / "tests/test_context_frontend.py").write_text("# original UI dependency marker\n")
    frontend = backend.parent / "frontend"
    for name in staging.FRONTEND_FILES:
        destination = frontend / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("// original authored fixture\n")
    (frontend / "src/.env.local").write_text("PRIVATE_MARKER_NOT_COPIED")
    (frontend / "src/private_debug.js").write_text("PRIVATE_MARKER_NOT_COPIED")
    members = staging.support_members(backend)
    assert "frontend/src/App.jsx" in members
    assert "frontend/src/.env.local" not in members
    assert "frontend/src/private_debug.js" not in members
    assert "backend/tools/native_distribution/media_runtime_smoke.py" in members
    assert "backend/tools/remote_https_smoke.py" in members
    assert "backend/tests/test_windows_native_acceptance_tool.py" in members
    assert "backend/tools/windows_native_acceptance.py" in members
    assert "backend/tools/cloud_model_acceptance.py" not in members
    (frontend / "src/App.jsx").unlink()
    with pytest.raises(ValueError, match="frontend"):
        staging.support_members(backend)


def test_current_clean_checkout_collects_all_context_tests_without_legacy_backend(
    tmp_path, monkeypatch, capsys
):
    """Collect all, then test synthetic dependency contracts; no native/browser probes."""
    backend = tmp_path / "clean-checkout/backend"
    backend.mkdir(parents=True)
    sources = staging.source_members(BACKEND)
    support = staging.support_members(BACKEND)
    windows_module = "test_windows_native_acceptance_tool.py"
    windows_inputs = {
        name: support[name].read_bytes()
        for name in (
            "backend/tests/" + windows_module,
            "backend/tools/windows_native_acceptance.py",
        )
    }
    for name, source in sources.items():
        target = backend / "src" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name, source in support.items():
        target = backend.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    wheel = candidate(tmp_path, backend)
    context, report = stage(monkeypatch, capsys, backend, wheel, tmp_path / "outputs")
    tests = {p.name for p in (BACKEND / "tests").glob("test_context*.py")}
    tests.update(staging.EXTRA_TESTS)
    assert tests <= set(report["test_files"])
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("RUN_LOCAL_") or name == "COLLECTION_CONTEXT_TEST_BROWSER_BINARY":
            environment.pop(name)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join((str(context / "backend/src"), str(context / "backend")))
    original_environment = dict(environment)
    original_environment["PYTHONPATH"] = os.pathsep.join((str(BACKEND / "src"), str(BACKEND)))
    original = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(BACKEND / "tests" / windows_module),
            "--collect-only",
            "-q",
            "--color=no",
            "-p",
            "no:cacheprovider",
        ],
        env=original_environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert original.returncode == 0, original.stdout[-8000:] + original.stderr[-2000:]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(context / "backend/tests"),
            "--collect-only",
            "-q",
            "--color=no",
            "-p",
            "no:cacheprovider",
        ],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-2000:]
    assert "tests collected" in result.stdout

    def module_nodeids(stdout):
        nodes = set()
        for line in stdout.splitlines():
            path, separator, test_id = line.partition("::")
            if separator and Path(path).name == windows_module:
                nodes.add(windows_module + separator + test_id)
        return nodes

    original_nodes = module_nodeids(original.stdout)
    assert original_nodes, original.stdout
    assert module_nodeids(result.stdout) == original_nodes
    for name, expected_bytes in windows_inputs.items():
        assert support[name].read_bytes() == expected_bytes, "Original changed during collection"
        assert (backend.parent / name).read_bytes() == expected_bytes
        assert (context / name).read_bytes() == expected_bytes
    assert not (context / "backend/app").exists()

    dependency_tests = sorted({*staging.TEST_TOOLS, "test_context_access.py", "test_context_frontend.py"})
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *[str(context / "backend/tests" / name) for name in dependency_tests],
        ],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout[-8000:] + checked.stderr[-2000:]
    assert "passed" in checked.stdout


def runner_module():
    path = BACKEND / "tools/linux_acceptance/runner.py"
    spec = importlib.util.spec_from_file_location("original_linux_runner", path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_runner_rejects_non_linux_before_output_or_subprocess(tmp_path, monkeypatch):
    runner = runner_module()
    monkeypatch.setattr(runner.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("No execution on host"))
    output = tmp_path / "untouched"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--output", str(output), "--models", str(tmp_path), "--expected-machine", "x86_64"],
    )
    with pytest.raises(RuntimeError, match="explicit Linux target"):
        runner.main()
    assert not output.exists()


def test_junit_counts_do_not_hide_failures_errors_or_unexpected_skips(tmp_path):
    path = tmp_path / "original-report.xml"
    path.write_text("""<testsuites><testsuite>
      <testcase classname="tests.original" name="passed"/>
      <testcase classname="tests.original" name="failure"><failure/></testcase>
      <testcase classname="tests.original" name="error"><error/></testcase>
      <testcase classname="tests.original" name="not_run"><skipped message="missing runtime"/></testcase>
      <testcase classname="tests.test_context_system_native_roundtrip"
        name="test_native_system_configuration_reopen_and_exact_cleanup">
        <skipped message="opt-in native disposable keychain roundtrip"/>
      </testcase></testsuite></testsuites>""")
    result = runner_module().junit_summary(path)
    assert {key: result[key] for key in ("total", "run", "passed", "failed", "errors", "skipped")} == {
        "total": 5,
        "run": 3,
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "skipped": 2,
    }
    assert result["unexpected_skips"] == 1
    expected = result["skipped_cases"][1]
    assert expected["expected_on_linux"] and "unverified" in expected["scope"]


def test_junit_empty_and_missing_evidence_are_not_success(tmp_path):
    runner = runner_module()
    path = tmp_path / "report.xml"
    with pytest.raises(OSError):
        runner.junit_summary(path)
    path.write_text("<testsuites/>")
    with pytest.raises(ValueError, match="no executed"):
        runner.junit_summary(path)


def test_linux_runner_enables_applicable_native_tests_without_keychain_claim():
    assert runner_module().LOCAL_FLAGS == {
        "RUN_LOCAL_MEDIA_RUNTIME": "1",
        "RUN_LOCAL_HTTPS_SMOKE": "1",
        "RUN_LOCAL_BROWSER_LEASE": "1",
        "RUN_LOCAL_SEARCH_UI": "1",
    }


def test_docker_paths_keep_repository_layout_and_explicit_arm64_scope():
    dockerfile = (BACKEND / "tools/linux_acceptance/Dockerfile").read_text()
    assert "COPY backend /acceptance/backend" in dockerfile
    assert "COPY frontend /acceptance/frontend" in dockerfile
    assert "/acceptance/backend/tools/linux_acceptance/runner.py" in dockerfile
    assert '"--expected-machine", "aarch64"' in dockerfile
    assert "COPY . " not in dockerfile
