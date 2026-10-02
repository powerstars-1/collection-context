"""Default tests are offline; explicit local TLS opt-in never accesses a cloud provider."""

from __future__ import annotations

import io
import os
import ssl
import subprocess

import pytest

from tools import remote_https_smoke as smoke


def test_startup_logs_keep_only_fixed_categories_and_close_owned_stream():
    stream = io.BytesIO(b"private-fixture-token\nERROR: address already in use /private/path")
    logs = smoke.StartupLogs(stream)
    logs.finish()
    state = logs.snapshot()
    assert stream.closed and state["stderr_class"] == "bind_failed"
    assert "private" not in str(state) and "token" not in str(state)


def test_startup_logs_bound_inspection_but_drain_overflow_and_split_patterns():
    body = b"x" * 1020 + b"ModuleNotFoundError: secret-fixture" + b"y" * smoke.MAX_STARTUP_LOG_BYTES
    stream = io.BytesIO(body)
    logs = smoke.StartupLogs(stream)
    logs.finish()
    state = logs.snapshot()
    assert state["stderr_class"] == "dependency_missing"
    assert state["stderr_inspected_bytes"] == smoke.MAX_STARTUP_LOG_BYTES
    assert state["stderr_over_limit"] is True and "secret" not in str(state)
    assert stream.closed


def test_pipe_read_and_close_failure_never_echoes_source_exception():
    class FailedPipe(io.BytesIO):
        def read1(self, size):
            raise OSError("private diagnostic detail")

        def close(self):
            super().close()
            raise OSError("private close detail")

    logs = smoke.StartupLogs(FailedPipe())
    logs.finish()
    assert logs.snapshot()["stderr_class"] == "capture_unavailable"
    assert "private" not in str(logs.snapshot())


def readiness_fixture(monkeypatch, request, *, exited=False):
    now, timeouts = [0.0], []
    monkeypatch.setattr(smoke.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def invoke(port, context, path, *, timeout):
        assert path == "/health" and port == 1
        timeouts.append(timeout)
        return request(now, timeout)

    monkeypatch.setattr(smoke, "_request", invoke)

    class Process:
        def poll(self):
            return 1 if exited else None

    class Logs:
        def snapshot(self):
            return {"stderr_class": "none", "stderr_inspected_bytes": 0, "stderr_over_limit": False}

    return Process(), Logs(), timeouts, now


def test_ready_requires_verified_product_identity_and_keeps_eight_second_budget(monkeypatch):
    process, logs, timeouts, _ = readiness_fixture(
        monkeypatch,
        lambda now, timeout: (
            200,
            {"ok": True, "data": {"service": "collection-context", "development_candidate": True}},
            {},
        ),
    )
    result = smoke._wait_ready(process, 1, ssl.create_default_context(), logs)
    assert result["state"] == "ready" and result["last_probe"] == "verified_product_health"
    assert result["budget_ms"] == 8000 and result["attempts"] == 1 and timeouts == [0.5]


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": 1, "data": {"service": "collection-context", "development_candidate": True}},
        {"ok": True, "data": {"service": "another-server", "development_candidate": True}},
        {"ok": True, "data": {"service": "collection-context", "development_candidate": 1}},
        [],
        None,
    ],
)
def test_other_health_or_malformed_envelope_cannot_fake_product_readiness(monkeypatch, response):
    process, logs, timeouts, _ = readiness_fixture(monkeypatch, lambda now, timeout: (200, response, {}))
    with pytest.raises(smoke.StartupFailure) as caught:
        smoke._wait_ready(process, 1, ssl.create_default_context(), logs)
    assert caught.value.observation["state"] == "deadline"
    assert caught.value.observation["last_probe"] == "wrong_product_health"
    assert caught.value.observation["budget_ms"] == 8000 and max(timeouts) <= 0.5


def test_dead_process_fails_before_probe(monkeypatch):
    process, logs, timeouts, _ = readiness_fixture(
        monkeypatch, lambda *args: pytest.fail("probe"), exited=True
    )
    with pytest.raises(smoke.StartupFailure) as caught:
        smoke._wait_ready(process, 1, ssl.create_default_context(), logs)
    assert caught.value.observation["state"] == "exited" and not timeouts


@pytest.mark.parametrize(
    "exception, category",
    [
        (OSError("secret network detail"), "connection_unavailable"),
        (ssl.SSLCertVerificationError("secret TLS detail"), "certificate_rejected"),
        (ValueError("secret body"), "protocol_error"),
    ],
)
def test_probe_failures_are_classified_without_raw_exceptions(monkeypatch, exception, category):
    def fail(now, timeout):
        now[0] += timeout
        raise exception

    process, logs, timeouts, _ = readiness_fixture(monkeypatch, fail)
    with pytest.raises(smoke.StartupFailure) as caught:
        smoke._wait_ready(process, 1, ssl.create_default_context(), logs)
    assert caught.value.observation["last_probe"] == category
    assert "secret" not in str(caught.value) and timeouts[-1] < 0.5


def test_healthy_response_after_deadline_is_not_passed(monkeypatch):
    def late(now, timeout):
        now[0] = 8.01
        return 200, {"ok": True, "data": {"service": "collection-context", "development_candidate": True}}, {}

    process, logs, _, _ = readiness_fixture(monkeypatch, late)
    with pytest.raises(smoke.StartupFailure) as caught:
        smoke._wait_ready(process, 1, ssl.create_default_context(), logs)
    assert caught.value.observation["state"] == "deadline"
    assert caught.value.observation["budget_ms"] == 8000


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
