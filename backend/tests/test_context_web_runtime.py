"""Both entrypoints must use the same explicit single-process HTTP stack."""

from __future__ import annotations

import asyncio
import importlib.abc
import io
import os
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from collection_context.infrastructure.web_runtime import web_runtime_options
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore


def test_fixed_configuration_is_fresh_and_does_not_follow_environment(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setenv("UVICORN_LOOP", "uvloop")
    monkeypatch.setenv("UVICORN_HTTP", "httptools")
    monkeypatch.setenv("UVICORN_RELOAD", "true")
    first = web_runtime_options()
    assert first == {
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
        "interface": "asgi3",
        "lifespan": "on",
        "workers": 1,
        "reload": False,
        "proxy_headers": False,
        "access_log": False,
        "log_level": "warning",
        "limit_concurrency": 20,
        "timeout_keep_alive": 5,
    }
    first["workers"] = 8
    assert web_runtime_options()["workers"] == 1


def test_actual_uvicorn_config_load_uses_http_without_optional_protocol_plugins(monkeypatch):
    import uvicorn

    monkeypatch.setenv("WEB_CONCURRENCY", "8")

    class NoOptionalProtocols(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in {"uvloop", "httptools", "websockets", "wsproto"}:
                pytest.fail("Explicit HTTP configuration imported an optional protocol")

    finder = NoOptionalProtocols()
    sys.meta_path.insert(0, finder)
    try:

        async def application(scope, receive, send):
            pass

        config = uvicorn.Config(application, **web_runtime_options())
        config.load()
        assert config.workers == 1 and not config.reload and not config.proxy_headers
        assert config.http_protocol_class.__module__ == "uvicorn.protocols.http.h11_impl"
        assert config.ws_protocol_class is None
        assert config.lifespan_class.__module__ == "uvicorn.lifespan.on"
        assert config.interface == "asgi3"
        assert config.get_loop_factory().__module__ == asyncio.SelectorEventLoop.__module__
    finally:
        sys.meta_path.remove(finder)


def test_fresh_interpreter_rejects_optional_protocol_imports_and_ignores_worker_environment(tmp_path):
    script = """
import asyncio, importlib.abc, sys
class NoProtocolPlugins(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'uvloop', 'httptools', 'websockets', 'wsproto'}:
            raise RuntimeError('optional protocol was imported')
sys.meta_path.insert(0, NoProtocolPlugins())
from collection_context.infrastructure.web_runtime import web_runtime_options
assert 'uvicorn' not in sys.modules and 'fastapi' not in sys.modules
import uvicorn
async def app(scope, receive, send):
    pass
config = uvicorn.Config(app, **web_runtime_options())
config.load()
assert config.workers == 1 and config.reload is False
assert config.ws_protocol_class is None
assert config.http_protocol_class.__module__ == 'uvicorn.protocols.http.h11_impl'
assert config.get_loop_factory().__module__ == asyncio.SelectorEventLoop.__module__
assert config.lifespan == 'on' and config.interface == 'asgi3'
assert config.proxy_headers is False and config.access_log is False
print('explicit_http_runtime_verified')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "WEB_CONCURRENCY": "8",
            "UVICORN_LOOP": "uvloop",
            "UVICORN_HTTP": "httptools",
            "UVICORN_RELOAD": "true",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "explicit_http_runtime_verified"
    assert result.stderr == ""


@pytest.mark.parametrize("remote", [False, True])
def test_standalone_server_passes_fixed_options_without_changing_bind_or_tls(tmp_path, monkeypatch, remote):
    import uvicorn

    from collection_context.interfaces.server import main

    root = tmp_path / "original-library"
    with closing(LibraryStore.initialize(root)) as store:
        AccessRegistry(store).create("Original reader")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    args = ["--workspace", str(root), "--origin", "http://127.0.0.1:12345"]
    if remote:
        args = [
            "--workspace",
            str(root),
            "--origin",
            "https://localhost:12345",
            "--remote",
            "--bind",
            "127.0.0.1",
            "--tls-cert",
            str(tmp_path / "fixture.pem"),
            "--tls-key",
            str(tmp_path / "fixture-key.pem"),
        ]
    assert main(args) == 0
    assert len(calls) == 1
    options = calls[0]
    assert all(options[key] == value for key, value in web_runtime_options().items())
    assert options["host"] == "127.0.0.1" and options["port"] == 12345
    assert options["ssl_certfile"] == (str(tmp_path / "fixture.pem") if remote else None)
    assert options["ssl_keyfile"] == (str(tmp_path / "fixture-key.pem") if remote else None)


def test_launcher_passes_the_same_options_and_does_not_spawn_workers(tmp_path, monkeypatch):
    import uvicorn

    from collection_context import launcher

    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setattr(launcher, "_wait_for_ready", lambda *args, **kwargs: None)
    configurations = []

    class OneServer:
        started = True
        should_exit = False

        def __init__(self, config):
            configurations.append(config)

        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", OneServer)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert (
        launcher.launch(
            tmp_path / "original-desktop-library",
            port=port,
            initialize_empty=True,
            no_browser=True,
            output=io.StringIO(),
            owner_presenter=lambda _: True,
        )
        == 0
    )
    assert len(configurations) == 1
    config = configurations[0]
    assert all(getattr(config, key) == value for key, value in web_runtime_options().items())
    assert config.host == "127.0.0.1" and config.port == port
    assert config.ssl_certfile is None and config.ssl_keyfile is None
