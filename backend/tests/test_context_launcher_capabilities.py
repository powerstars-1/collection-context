"""Offline fixed startup grants and handle ownership; no account or model access."""

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from collection_context.application import launcher_capabilities as module
from collection_context.application.contracts import ContextError
from collection_context.application.launcher_capabilities import (
    LauncherCapabilities,
    default_desktop_capabilities,
    launcher_resources,
)
from collection_context.infrastructure.secrets import FileSecrets


@pytest.fixture
def paths(tmp_path):
    return {
        "workspace": tmp_path / "library",
        "credential_dir": tmp_path / "credentials",
        "browser_dir": tmp_path / "browser",
        "runtime_dir": tmp_path / "runtime",
    }


def reject(call, code=None):
    with pytest.raises(ContextError) as caught:
        call()
    if code:
        assert caught.value.code == code
    assert "synthetic-secret" not in caught.value.message


def test_default_and_constructor_are_frozen_side_effect_free(paths):
    config = LauncherCapabilities(paths["workspace"])
    assert not any(
        (
            config.allow_model_config,
            config.allow_model_calls,
            config.allow_source_connect,
            config.allow_source_sync,
        )
    )
    assert config.credential_dir is config.browser_dir is config.runtime_dir is None
    assert not any(path.exists() for path in paths.values())
    with pytest.raises(FrozenInstanceError):
        config.allow_model_calls = True


@pytest.mark.parametrize(
    "name", ["allow_model_config", "allow_source_connect", "allow_model_calls", "allow_source_sync"]
)
@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_flags_are_not_truthy_coerced(paths, name, value):
    reject(lambda: LauncherCapabilities(**paths, **{name: value}), "launcher_capabilities_invalid")


@pytest.mark.parametrize(
    "options",
    [
        {"allow_model_config": True},
        {"allow_source_connect": True},
        {"allow_model_calls": True},
        {"allow_source_sync": True},
        {"allow_model_calls": True, "credential_dir": "given"},
        {"allow_source_sync": True, "browser_dir": "given"},
    ],
)
def test_prerequisites_do_not_implicitly_grant(paths, options):
    options = {key: paths[key] if value == "given" else value for key, value in options.items()}
    reject(lambda: LauncherCapabilities(paths["workspace"], **options), "launcher_capabilities_invalid")


@pytest.mark.parametrize("role", ["workspace", "credential_dir", "browser_dir", "runtime_dir"])
@pytest.mark.parametrize("bad", ["relative", "root", "home", "parent", "not_path"])
def test_paths_are_absolute_specific_without_parent_hops(paths, role, bad):
    values = {
        "relative": Path("relative"),
        "root": Path("/"),
        "home": Path.home(),
        "parent": paths[role] / ".." / "other",
        "not_path": "/synthetic-secret",
    }
    paths[role] = values[bad]
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")


@pytest.mark.parametrize(
    "left,right",
    [
        ("workspace", "credential_dir"),
        ("workspace", "browser_dir"),
        ("workspace", "runtime_dir"),
        ("credential_dir", "browser_dir"),
        ("credential_dir", "runtime_dir"),
        ("browser_dir", "runtime_dir"),
    ],
)
@pytest.mark.parametrize("direction", ["same", "child", "parent"])
def test_all_directory_overlap_rejected(paths, left, right, direction):
    paths[right] = paths[left] if direction == "same" else paths[left] / "nested"
    if direction == "parent":
        paths[left], paths[right] = paths[right], paths[left]
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")


@pytest.mark.parametrize("role", ["workspace", "credential_dir", "browser_dir", "runtime_dir"])
@pytest.mark.parametrize("parent_link", [False, True])
def test_leaf_and_ancestor_links_rejected(paths, tmp_path, role, parent_link):
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    paths[role] = link / "child" if parent_link else link
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")


@pytest.mark.parametrize("role", ["credential_dir", "browser_dir", "runtime_dir"])
@pytest.mark.parametrize("mode", [0o755, 0o770, 0o600])
def test_private_existing_directories_are_not_repaired(paths, role, mode):
    paths[role].mkdir(mode=mode)
    paths[role].chmod(mode)
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")
    assert paths[role].stat().st_mode & 0o777 == mode


def test_permission_failure_and_unsupported_os_fail_closed(paths, monkeypatch):
    original = Path.lstat

    def denied(path):
        if path == paths["credential_dir"]:
            raise PermissionError("synthetic-secret")
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied)
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")

    def unsupported():
        raise ContextError("unsupported_platform", "not verified")

    monkeypatch.setattr(module, "require_safe_files_runtime", unsupported)
    reject(lambda: LauncherCapabilities(paths["workspace"]), "unsupported_platform")


def test_foreign_owner_not_accepted(paths, monkeypatch):
    paths["credential_dir"].mkdir(mode=0o700)
    monkeypatch.setattr(module.os, "getuid", lambda: os.stat(paths["credential_dir"]).st_uid + 1)
    reject(lambda: LauncherCapabilities(**paths), "launcher_directory_unsafe")


def test_defaults_no_cwd_env_or_old_configuration(tmp_path, monkeypatch):
    root = tmp_path / "product"
    canonical = root / "workspace"
    monkeypatch.setattr(module.diagnostics, "default_workspace", lambda: canonical)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-secret")
    config = default_desktop_capabilities(
        tmp_path / "external", allow_model_config=True, allow_source_connect=True
    )
    assert config.credential_dir == root / "credentials"
    assert config.browser_dir == root / "browser"
    assert config.runtime_dir is None
    assert not root.exists()
    assert not config.allow_model_calls and not config.allow_source_sync
    assert default_desktop_capabilities(canonical).workspace == canonical
    reject(lambda: default_desktop_capabilities(root), "launcher_directory_unsafe")
    reject(lambda: default_desktop_capabilities(root / "browser"), "launcher_directory_unsafe")


def test_default_runtime_requires_receipt_but_never_reads_it(tmp_path, monkeypatch):
    root = tmp_path / "product"
    runtime = root / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(module.diagnostics, "default_workspace", lambda: root / "workspace")
    assert default_desktop_capabilities(tmp_path / "library").runtime_dir is None
    receipt = runtime / module.RECEIPT_NAME
    receipt.write_text("not a validated receipt, never read by startup")
    receipt.chmod(0o600)
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: pytest.fail("must not read receipt"))
    config = default_desktop_capabilities(tmp_path / "library")
    assert config.runtime_dir == runtime
    assert not config.allow_model_calls and not config.allow_source_sync


@pytest.mark.parametrize("kind", ["link", "hardlink", "mode", "directory"])
def test_default_bad_receipt_not_selected(tmp_path, monkeypatch, kind):
    root = tmp_path / "product"
    runtime = root / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(module.diagnostics, "default_workspace", lambda: root / "workspace")
    receipt = runtime / module.RECEIPT_NAME
    if kind == "link":
        receipt.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        receipt.mkdir()
    else:
        receipt.write_text("original fixture")
        receipt.chmod(0o600 if kind == "hardlink" else 0o644)
        if kind == "hardlink":
            os.link(receipt, tmp_path / "copy")
    reject(lambda: default_desktop_capabilities(tmp_path / "library"), "launcher_directory_unsafe")


def test_factory_default_has_no_handles_or_writes(paths, monkeypatch):
    monkeypatch.setattr(module, "FileSecrets", lambda *_: pytest.fail("no secrets"))
    monkeypatch.setattr(module, "ConnectionRunner", lambda *_a, **_k: pytest.fail("no runner"))
    with launcher_resources(LauncherCapabilities(paths["workspace"])) as resources:
        assert resources.model_secrets is resources.connection_runner is None
        assert resources.credential_storage == "not_enabled" and resources.worker_started is False
    assert not any(path.exists() for path in paths.values())


def test_real_secret_handle_opens_without_get_and_closes_on_error(paths, monkeypatch):
    backend = FileSecrets.initialize(paths["credential_dir"])
    ref = backend.put("original synthetic fixture not a real key")
    backend.close()
    monkeypatch.setattr(FileSecrets, "get", lambda *_: pytest.fail("startup must not read keys"))
    config = LauncherCapabilities(**paths, allow_model_config=True)
    opened = None
    with pytest.raises(RuntimeError):
        with launcher_resources(config) as resources:
            opened = resources.model_secrets
            assert opened.files.fd >= 0
            assert resources.credential_storage == "private_service_files_not_encrypted"
            assert not resources.worker_started
            raise RuntimeError("original body failure")
    assert opened.files.fd == -1
    assert (paths["credential_dir"] / ref).exists()
    assert not paths["browser_dir"].exists() and not paths["runtime_dir"].exists()


def test_factory_opens_model_and_connection_without_starting_browser(paths, monkeypatch):
    monkeypatch.setattr(
        module.ConnectionRunner, "start", lambda *_a, **_k: pytest.fail("no connection start")
    )
    config = LauncherCapabilities(
        **paths,
        allow_model_config=True,
        allow_source_connect=True,
        allow_model_calls=True,
        allow_source_sync=True,
    )
    with launcher_resources(config) as resources:
        secrets, runner = resources.model_secrets, resources.connection_runner
        assert runner.runtime_dir == paths["runtime_dir"] and runner.headless is False
        assert runner.status()["state"] == "idle" and not runner.status()["active"]
        assert not resources.worker_started
    assert secrets.files.fd == -1 and not runner.status()["enabled"]
    assert not paths["browser_dir"].exists() and not paths["runtime_dir"].exists()


def test_factory_partial_failure_closes_secret_handle(paths, monkeypatch):
    closed = []
    original_close = FileSecrets.close

    def close(value):
        closed.append(value)
        original_close(value)

    monkeypatch.setattr(FileSecrets, "close", close)

    def fail(*_a, **_k):
        raise ContextError("fixture_failure", "original runner constructor failure")

    monkeypatch.setattr(module, "ConnectionRunner", fail)
    config = LauncherCapabilities(**paths, allow_model_config=True, allow_source_connect=True)
    reject(lambda: _open(config), "fixture_failure")
    assert len(closed) == 1 and closed[0].files.fd == -1


def _open(config):
    with launcher_resources(config):
        pass


def test_factory_rechecks_changed_path_before_side_effects(paths, tmp_path):
    config = LauncherCapabilities(**paths, allow_model_config=True)
    target = tmp_path / "foreign"
    target.mkdir()
    paths["credential_dir"].symlink_to(target, target_is_directory=True)
    reject(lambda: _open(config), "launcher_directory_unsafe")
    assert not list(target.iterdir())


@pytest.mark.parametrize("headless", [1, None, "false"])
def test_factory_headless_not_coerced(paths, headless):
    with pytest.raises(ContextError) as caught:
        with launcher_resources(LauncherCapabilities(paths["workspace"]), headless=headless):
            pass
    assert caught.value.code == "launcher_capabilities_invalid"
