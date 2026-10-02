"""Real loopback HTTPS transport smoke; synthetic content, no platform or model requests.

Uses the product's unmodified remote server, a short-lived trusted localhost test
certificate, and Python's verified HTTPS client. This is not a host-AI/client-cloud
acceptance claim. Credentials stay in memory; only their hashes persist temporarily.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

import collection_context
from collection_context.application.contracts import ContextError, digest
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.store import LibraryStore

REQUEST_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 256_000
STARTUP_TIMEOUT = 8.0
MAX_STARTUP_LOG_BYTES = 65_536


class StartupFailure(RuntimeError):
    def __init__(self, code: str, observation: dict[str, Any]):
        # Observations contain only our counters and fixed categories, never
        # subprocess text, request data, paths, tokens or exception strings.
        self.code = (
            code
            if code in {"product_https_server_exited", "product_https_server_not_ready"}
            else "product_https_startup_failed"
        )
        super().__init__(self.code)
        self.observation = observation

    def __str__(self) -> str:
        return self.code + "; " + json.dumps(self.observation, sort_keys=True)


class DiagnosticPipe(Protocol):
    def read1(self, size: int = -1, /) -> bytes: ...
    def close(self) -> None: ...


class StartupLogs:
    """Drain our own child's pipe; retain categories, not log contents/files."""

    def __init__(self, stream: DiagnosticPipe):
        self.stream = stream
        self._lock = threading.Lock()
        self._bytes = 0
        self._clipped = False
        self._category = "none"
        self._thread = threading.Thread(target=self._drain, daemon=True, name="isolated-https-startup")
        self._thread.start()

    def _drain(self) -> None:
        tail = b""
        categories = (
            (b"address already in use", "bind_failed"),
            (b"error while attempting to bind", "bind_failed"),
            (b"modulenotfounderror", "dependency_missing"),
            (b"importerror", "dependency_missing"),
            (b"sslerror", "tls_failed"),
            (b"access_setup_required:", "access_setup_required"),
            (b"storage_unavailable:", "storage_unavailable"),
            (b"traceback", "error_logged"),
            (b"error:", "error_logged"),
        )
        try:
            while chunk := self.stream.read1(1024):
                with self._lock:
                    remaining = MAX_STARTUP_LOG_BYTES - self._bytes
                    self._clipped |= len(chunk) > remaining
                    selected = chunk[:remaining]
                    self._bytes += len(selected)
                    window = tail + selected.lower()
                    # Even overflow is drained to prevent a blocked child pipe;
                    # unbounded output is not retained or inspected after cap.
                    if self._category in {"none", "error_logged"}:
                        for pattern, category in categories:
                            if pattern in window:
                                self._category = category
                                break
                    tail = window[-256:] if remaining else b""
        except (OSError, ValueError):
            with self._lock:
                self._category = "capture_unavailable"
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                with self._lock:
                    self._category = "capture_unavailable"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stderr_class": self._category,
                "stderr_inspected_bytes": self._bytes,
                "stderr_over_limit": self._clipped,
            }

    def finish(self) -> None:
        # Called after our child is stopped and its pipe should be at EOF.
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("https_startup_capture_not_stopped")


def _wait_ready(process: subprocess.Popen, port: int, context: ssl.SSLContext, logs: StartupLogs) -> dict:
    started = time.monotonic()
    deadline, attempts, last_probe = started + STARTUP_TIMEOUT, 0, "not_probed"

    def observation(state: str) -> dict[str, Any]:
        return {
            "state": state,
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "budget_ms": round(STARTUP_TIMEOUT * 1000),
            "attempts": attempts,
            "last_probe": last_probe,
            **logs.snapshot(),
        }

    while True:
        if process.poll() is not None:
            raise StartupFailure("product_https_server_exited", observation("exited"))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StartupFailure("product_https_server_not_ready", observation("deadline"))
        attempts += 1
        try:
            code, response, _ = _request(port, context, "/health", timeout=min(0.5, remaining))
            data = response.get("data") if isinstance(response, dict) else None
            if (
                code == 200
                and isinstance(response, dict)
                and response.get("ok") is True
                and isinstance(data, dict)
                and data.get("service") == "collection-context"
                and data.get("development_candidate") is True
            ):
                last_probe = "verified_product_health"
                if process.poll() is not None:
                    raise StartupFailure("product_https_server_exited", observation("exited"))
                if time.monotonic() > deadline:
                    raise StartupFailure("product_https_server_not_ready", observation("deadline"))
                return observation("ready")
            last_probe = "wrong_product_health"
        except ssl.SSLCertVerificationError:
            last_probe = "certificate_rejected"
        except (OSError, http.client.HTTPException):
            last_probe = "connection_unavailable"
        except (ValueError, TypeError):
            last_probe = "protocol_error"
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def _request(
    port: int,
    context: ssl.SSLContext,
    path: str,
    *,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, dict, dict]:
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=timeout)
    try:
        values = dict(headers or {})
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            values["Content-Type"] = "application/json"
        connection.request("GET" if body is None else "POST", path, body=body, headers=values)
        if connection.sock is not None:
            connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
        response = connection.getresponse()
        if connection.sock is not None:
            connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
            raise RuntimeError("https_response_limit")
        return response.status, json.loads(raw), dict(response.getheaders())
    finally:
        connection.close()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _certificate(directory: Path) -> tuple[Path, Path]:
    executable = shutil.which("openssl")
    if executable is None:
        raise RuntimeError("test_certificate_tool_missing")
    cert, key = directory / "localhost-test-cert.pem", directory / "localhost-test-key.pem"
    result = subprocess.run(
        [
            executable,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        env={"PATH": os.defpath},
    )
    if result.returncode != 0 or not cert.is_file() or not key.is_file():
        raise RuntimeError("test_certificate_generation_failed")
    cert.chmod(0o600)
    key.chmod(0o600)
    return cert, key


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="collection-context-https-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        cert, key = _certificate(directory)
        verified = ssl.create_default_context(cafile=str(cert))
        assert verified.check_hostname and verified.verify_mode == ssl.CERT_REQUIRED
        store = LibraryStore.initialize(directory / "原创合成 HTTPS 库")
        process: subprocess.Popen | None = None
        startup_logs: StartupLogs | None = None
        startup_failure: StartupFailure | None = None
        try:
            item = store.upsert(
                {"native_id": "99887766", "title": "原创 HTTPS 合成教程", "body": "不是实际同步作品"},
                kind="saved",
                scope_id="s_https_fixture",
            )["item"]
            text = "原创合成画面：UI 参数 1024，仅验证 TLS 传输与引用。"
            artifact = store.save_artifact(
                item["id"],
                "screen",
                text,
                processor_version="https_fixture_v1",
                expected_content_hash=item["content_hash"],
                coverage={"accuracy": "synthetic", "complete": False},
            )
            registry = AccessRegistry(store)
            reader = registry.create("临时 HTTPS 只读")
            owner = registry.create("临时 HTTPS 主人", ui=True, manage=True)
            original = store.snapshot()
            original_sha = hashlib.sha256(store.files.read(artifact["path"])).hexdigest()
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            origin = f"https://localhost:{port}"
            # No inherited keys, cookies or provider routing; no credentials in arguments.
            environment = {
                "PATH": os.defpath,
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(collection_context.__file__).resolve().parent.parent),
            }
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "collection_context.interfaces.server",
                    "--workspace",
                    str(store.files.root),
                    "--origin",
                    origin,
                    "--bind",
                    "127.0.0.1",
                    "--remote",
                    "--tls-cert",
                    str(cert),
                    "--tls-key",
                    str(key),
                ],
                cwd=directory,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            assert isinstance(process.stderr, io.BufferedReader)
            startup_logs = StartupLogs(process.stderr)
            startup = _wait_ready(process, port, verified, startup_logs)
            checks: list[str] = ["product_remote_server_real_tls_started"]
            # Default public trust must reject our temporary private test certificate.
            try:
                _request(port, ssl.create_default_context(), "/health")
            except ssl.SSLCertVerificationError:
                checks.append("untrusted_certificate_rejected")
            else:
                raise RuntimeError("untrusted_certificate_was_accepted")
            auth = {"Authorization": "Bearer " + reader["token"]}
            code, search, _ = _request(
                port, verified, "/v1/collections/search", payload={"query": "HTTPS UI 1024"}, headers=auth
            )
            assert code == 200 and search["ok"] and search["data"]["items"][0]["material_ref"] == item["id"]
            assert search["data"]["model_requests"] == 0
            checks.append("authenticated_search_with_provenance")
            code, read, _ = _request(
                port,
                verified,
                "/v1/collections/read",
                payload={"material_ref": item["id"], "artifact": "screen"},
                headers=auth,
            )
            assert code == 200 and read["data"]["text"] == text
            assert read["data"]["content_untrusted"] is True
            checks.append("authenticated_evidence_read")
            code, status, response_headers = _request(
                port, verified, f"/v1/collections/{item['id']}/status", headers=auth
            )
            assert code == 200 and status["data"]["artifacts"]["audio"]["state"] == "missing"
            assert response_headers["cache-control"] == "no-store"
            checks.append("authenticated_status_and_no_store")

            def denied(expected_code: int, expected_error: str, extra: dict[str, str]) -> None:
                result_code, result, _ = _request(
                    port, verified, "/v1/collections/search", payload={"query": "HTTPS"}, headers=extra
                )
                assert result_code == expected_code and result["error"]["code"] == expected_error
                assert reader["token"] not in json.dumps(result) and owner["token"] not in json.dumps(result)

            denied(401, "authentication_required", {})
            denied(401, "authentication_required", {"Authorization": "Bearer invalid-fixture"})
            denied(401, "authentication_required", {"Authorization": "Basic invalid"})
            checks.append("missing_wrong_and_malformed_auth_rejected")
            denied(
                403,
                "forbidden_host",
                {**auth, "Host": "evil.example", "X-Forwarded-Host": f"localhost:{port}"},
            )
            denied(403, "forbidden_origin", {**auth, "Origin": "https://evil.example"})
            denied(403, "forbidden_origin", {**auth, "Origin": "null"})
            denied(403, "forbidden_origin", {**auth, "Sec-Fetch-Site": "cross-site"})
            checks.append("host_origin_fetch_metadata_enforced_no_proxy_trust")
            code, result, _ = _request(port, verified, "/v1/management/overview", payload={}, headers=auth)
            assert code == 403 and result["error"]["code"] == "permission_denied"
            checks.append("reader_bearer_cannot_manage")
            code, session, headers = _request(
                port, verified, "/v1/session", payload={"token": owner["token"]}
            )
            assert code == 200 and session["ok"]
            cookie = headers["set-cookie"]
            assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
            checks.append("remote_owner_session_secure_cookie")
            registry.revoke(reader["principal"])
            denied(401, "authentication_required", auth)
            checks.append("live_reader_revocation_enforced")
            registry.revoke(owner["principal"])
            code, result, _ = _request(
                port, verified, "/v1/collections/overview", headers={"Cookie": cookie.split(";", 1)[0]}
            )
            assert code == 401 and result["error"]["code"] == "authentication_required"
            checks.append("live_owner_cookie_revocation_enforced")
            assert store.snapshot() == original
            assert hashlib.sha256(store.files.read(artifact["path"])).hexdigest() == original_sha
            checks.append("library_state_and_evidence_hash_unchanged")
            _stop(process)
            assert process.poll() is not None
            return {
                "result": "passed",
                "checks": checks,
                "client": {
                    "name": "Python stdlib http.client.HTTPSConnection",
                    "python": platform.python_version(),
                    "tls_library": ssl.OPENSSL_VERSION,
                    "certificate_verification": "CERT_REQUIRED_and_hostname",
                    "trust": "temporary_localhost_test_certificate_only",
                    "timeout_seconds": REQUEST_TIMEOUT,
                },
                "transport": "real_remote_mode_HTTPS_over_loopback",
                "startup": startup,
                "server": {
                    "entrypoint": "collection_context.interfaces.server",
                    "fastapi": version("fastapi"),
                    "uvicorn": version("uvicorn"),
                    "proxy_headers_trusted": False,
                },
                "model_requests": 0,
                "platform_requests": 0,
                "private_library_access": False,
                "server_stopped": True,
                "unverified": [
                    "external_host_AI_client",
                    "public_network_TLS_and_reverse_proxy",
                    "Windows_transport",
                ],
                "library_state_digest_unchanged": digest(original),
            }
        except StartupFailure as error:
            startup_failure = error
            raise
        finally:
            if process is not None:
                _stop(process)
            if startup_logs is not None:
                startup_logs.finish()
                if startup_failure is not None:
                    startup_failure.observation.update(startup_logs.snapshot())
            store.close()


def main() -> int:
    try:
        result = run_smoke()
    except StartupFailure as error:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "error": "controlled_https_smoke_failed",
                    "startup": error.observation,
                    "secrets_logged": False,
                }
            )
        )
        return 1
    except (
        ContextError,
        RuntimeError,
        OSError,
        AssertionError,
        subprocess.SubprocessError,
        http.client.HTTPException,
    ):
        # No request bodies, auth headers, private paths or subprocess outputs in errors.
        print(
            json.dumps(
                {"result": "failed", "error": "controlled_https_smoke_failed", "secrets_logged": False}
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
