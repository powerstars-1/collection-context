"""Default tests are offline; explicit local TLS opt-in never accesses a cloud provider."""

from __future__ import annotations

import os
import ssl
import subprocess

import pytest

from tools import remote_https_smoke as smoke


def test_shutdown_is_bounded_and_escalates_only_own_process():
    events = []

    class OwnProcess:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append(("wait", timeout))
            if events.count(("wait", timeout)) == 1:
                raise subprocess.TimeoutExpired("synthetic", timeout)

        def kill(self):
            events.append("kill")

    smoke._stop(OwnProcess())
    assert events == ["terminate", ("wait", 3), "kill", ("wait", 3)]


def test_failure_output_contains_no_exception_or_credentials(monkeypatch, capsys):
    def fail():
        raise RuntimeError("secret-fixture must never reach output")

    monkeypatch.setattr(smoke, "run_smoke", fail)
    assert smoke.main() == 1
    output = capsys.readouterr().out
    assert "secret-fixture" not in output and "controlled_https_smoke_failed" in output


def test_https_client_binds_verified_context_and_bounded_read(monkeypatch):
    events = []
    context = ssl.create_default_context()

    class Response:
        status = 200

        def read(self, maximum):
            events.append(("read", maximum))
            return b'{"ok":true}'

        def getheaders(self):
            return [("cache-control", "no-store")]

    class Client:
        sock = None

        def __init__(self, host, port, *, context, timeout):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            events.append(("connection", host, port, timeout))

        def request(self, method, path, *, body, headers):
            events.append((method, path))

        def getresponse(self):
            return Response()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(smoke.http.client, "HTTPSConnection", Client)
    code, data, headers = smoke._request(1, context, "/health")
    assert code == 200 and data["ok"] and headers["cache-control"] == "no-store"
    assert events == [
        ("connection", "localhost", 1, 10.0),
        ("GET", "/health"),
        ("read", smoke.MAX_RESPONSE_BYTES + 1),
        "closed",
    ]


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_HTTPS_SMOKE") != "1", reason="Explicit isolated local TLS test only"
)
def test_real_product_remote_https_transport():
    result = smoke.run_smoke()
    assert result["result"] == "passed" and result["server_stopped"]
    assert result["model_requests"] == result["platform_requests"] == 0
    assert result["client"]["certificate_verification"] == "CERT_REQUIRED_and_hostname"
    assert "live_reader_revocation_enforced" in result["checks"]
    assert "live_owner_cookie_revocation_enforced" in result["checks"]
    assert "external_host_AI_client" in result["unverified"]
