"""Local-only installation selection; no library, credential, or network reads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from collection_context.application import runtime_setup as setup
from collection_context.application.contracts import ContextError
from collection_context.cli import main


def ready(monkeypatch):
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(setup.metadata, "version", lambda name: "1.63.0")


@pytest.mark.parametrize(
    "system,arch,mac,version,state",
    [
        ("Darwin", "arm64", "14.0", "1.63.0", "available"),
        ("Darwin", "arm64", "13.0", "1.63.0", "os_version_not_available"),
        ("Darwin", "arm64", "", "1.63.0", "os_version_not_verified"),
        ("Darwin", "arm64", "14.0", "1.62.0", "sdk_version_mismatch"),
        ("Darwin", "x86_64", "14.0", "1.63.0", "host_not_available"),
        ("Windows", "arm64", "", "1.63.0", "host_not_available"),
        ("Linux", "arm64", "", "1.63.0", "host_not_available"),
    ],
)
def test_readonly_options_truthful_support(monkeypatch, system, arch, mac, version, state):
    monkeypatch.setattr(setup.platform, "system", lambda: system)
    monkeypatch.setattr(setup.platform, "machine", lambda: arch)
    monkeypatch.setattr(setup.platform, "mac_ver", lambda: (mac, (), ""))
    monkeypatch.setattr(setup.metadata, "version", lambda name: version)
    report = setup.runtime_options()
    assert report["artifacts"][0]["state"] == state
    assert report["auto_install"] is False
    assert report["grants_sync_or_model_authority"] is False
    assert report["artifacts"][0]["functional_verified"] is False
    assert report["artifacts"][0]["download_bytes"] == 98_831_293


def test_missing_sdk_not_available(monkeypatch):
    ready(monkeypatch)

    def missing(name):
        raise setup.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(setup.metadata, "version", missing)
    assert setup.runtime_options()["artifacts"][0]["state"] == "sdk_missing"


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_install_confirmation_exact_boolean_before_any_setup(tmp_path, monkeypatch, confirmed):
    ready(monkeypatch)
    monkeypatch.setattr(setup, "RuntimeDownloads", lambda *a, **k: pytest.fail("download construction"))
    with pytest.raises(ContextError) as error:
        setup.install_runtime(
            tmp_path / "runtime",
            library_dir=tmp_path / "library",
            artifact_id=setup.HEADLESS_ID,
            installation_confirmed=confirmed,
        )
    assert error.value.code == "runtime_install_confirmation"
    assert not list(tmp_path.iterdir())


def test_unknown_artifact_not_a_url_installation_route(tmp_path, monkeypatch):
    ready(monkeypatch)
    monkeypatch.setattr(setup, "RuntimeDownloads", lambda *a, **k: pytest.fail("download construction"))
    with pytest.raises(ContextError) as error:
        setup.install_runtime(
            tmp_path / "runtime",
            library_dir=tmp_path / "library",
            artifact_id="https://example.org/a",
            installation_confirmed=True,
        )
    assert error.value.code == "runtime_install_catalog"


def test_unsupported_system_fails_before_download_or_dirs(tmp_path, monkeypatch):
    ready(monkeypatch)
    monkeypatch.setattr(setup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(setup, "RuntimeDownloads", lambda *a, **k: pytest.fail("download construction"))
    with pytest.raises(ContextError) as error:
        setup.install_runtime(
            tmp_path / "runtime",
            library_dir=tmp_path / "library",
            artifact_id=setup.HEADLESS_ID,
            installation_confirmed=True,
        )
    assert error.value.code == "runtime_install_unavailable"
    assert not list(tmp_path.iterdir())


def test_shared_core_and_download_scope_exact_selection(tmp_path, monkeypatch):
    ready(monkeypatch)
    calls = []
    source = object()

    class DownloadScope:
        def __init__(self, *args, **kwargs):
            calls.append(("download_scope", args, kwargs))

        def __enter__(self):
            return source

        def __exit__(self, *args):
            calls.append(("closed",))

    class Installer:
        def __init__(self, *args, **kwargs):
            calls.append(("installer", args, kwargs))

        def install(self, *args, **kwargs):
            calls.append(("install", args, kwargs))
            return {"state": "installed", "functional_verified": False}

    monkeypatch.setattr(setup, "RuntimeDownloads", DownloadScope)
    monkeypatch.setattr(setup, "RuntimeInstaller", Installer)
    result = setup.install_runtime(
        tmp_path / "runtime",
        library_dir=tmp_path / "library",
        artifact_id=setup.HEADLESS_ID,
        installation_confirmed=True,
    )
    assert result["functional_verified"] is False
    assert calls[1][2]["_archive_source"] is source
    assert calls[2] == ("install", (setup.HEADLESS_ID,), {"installation_confirmed": True, "stop": None})
    assert calls[-1] == ("closed",)


def test_cli_options_without_library_and_no_files(tmp_path, capsys):
    assert main(["--workspace", str(tmp_path / "absent"), "runtime-options"]) == 0
    assert "download_bytes" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_cli_install_requires_explicit_directory(tmp_path, capsys):
    assert (
        main(["--workspace", str(tmp_path / "absent"), "install-runtime", "--artifact", setup.HEADLESS_ID])
        == 1
    )
    assert "runtime_install_directory_required" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_probe_refuses_existing_account_profile_before_browser(tmp_path, monkeypatch):
    profile = tmp_path / "existing"
    profile.mkdir()
    monkeypatch.setattr(setup, "BrowserSession", lambda *a, **k: pytest.fail("browser"))
    with pytest.raises(ContextError) as error:
        setup.probe_runtime(tmp_path / "runtime", library_dir=tmp_path / "library", browser_dir=profile)
    assert error.value.code == "runtime_probe_profile_exists"


def test_probe_local_fixture_and_exit_before_verified_result(tmp_path, monkeypatch):
    events = []

    class Page:
        def set_content(self, text):
            assert "original fixture" in text and "https:" not in text
            events.append("fixture")

        def locator(self, selector):
            assert selector == "#probe"
            return SimpleNamespace(text_content=lambda: "original fixture")

        def title(self):
            return "CollectionContext runtime probe"

        def close(self):
            events.append("page_closed")

    class Context:
        def route(self, pattern, callback):
            assert pattern == "**/*"
            callback(SimpleNamespace(abort=lambda: events.append("network_blocked")))

        def new_page(self):
            return Page()

    class Session:
        def __init__(self, profile, **kwargs):
            assert profile == tmp_path / "new-profile"
            assert kwargs == {
                "headless": True,
                "runtime_dir": tmp_path / "runtime",
                "library_dir": tmp_path / "library",
            }
            self.context = Context()

        def __enter__(self):
            events.append("entered")
            return self

        def __exit__(self, *args):
            events.append("closed")

    monkeypatch.setattr(setup, "BrowserSession", Session)
    result = setup.probe_runtime(
        tmp_path / "runtime", library_dir=tmp_path / "library", browser_dir=tmp_path / "new-profile"
    )
    assert events == ["entered", "network_blocked", "fixture", "page_closed", "closed"]
    assert result["functional_verified"] is True and result["model_calls"] == 0
    assert result["sync_verified"] is False and result["platform_login_verified"] is False
