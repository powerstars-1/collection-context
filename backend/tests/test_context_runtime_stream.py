"""Bounded synthetic socket streams; no external network or installed software."""

import hashlib
import os
from dataclasses import replace

import pytest
from test_context_runtime_download import plan

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_download as downloads
from collection_context.infrastructure import runtime_stream as streams
from collection_context.infrastructure.public_http import PublicHTTP


def transport(monkeypatch, responses):
    opened, requests, reads = [], [], []
    monkeypatch.setattr(streams, "public_addresses", lambda host: ("8.8.8.8",))

    class Response:
        def __init__(self, status=200, headers=None, body=b"original"):
            self.status, self.headers, self.body = status, headers or {}, body

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def read1(self, size):
            reads.append(size)
            data, self.body = self.body[:size], self.body[size:]
            return data

    class Connection:
        sock = None

        def __init__(self, host, address, timeout):
            self.closed = False
            self.host, self.address, self.timeout = host, address, timeout
            opened.append(self)

        def request(self, method, path, headers):
            requests.append((method, path, headers))

        def getresponse(self):
            return Response(**responses.pop(0))

        def close(self):
            self.closed = True

    monkeypatch.setattr(streams, "_PinnedHTTPS", Connection)
    return PublicHTTP(hosts=frozenset({"cdn.example", "cdn.playwright.dev"})), opened, requests, reads


def invoke(tmp_path, client, **kwargs):
    fd = os.open(tmp_path / "new-private", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return streams.download_into(client, "https://cdn.example/a", fd, check_cancel=lambda: None, **kwargs)
    finally:
        os.close(fd)


def test_bounded_chunks_digest_no_proxy_or_credentials(tmp_path, monkeypatch):
    body = b"original" * 20_000
    client, opened, requests, reads = transport(monkeypatch, [{"body": body}])
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid")
    count, digest = invoke(tmp_path, client, expected_bytes=len(body))
    assert count == len(body) and digest == hashlib.sha256(body).hexdigest()
    assert (tmp_path / "new-private").read_bytes() == body
    assert max(reads) <= 65_536 and len(reads) > 2
    assert opened[0].address == "8.8.8.8" and opened[0].timeout <= 30 and opened[0].closed
    assert set(requests[0][2]) == {"Host", "Accept-Encoding", "User-Agent"}


@pytest.mark.parametrize(
    "response",
    [
        {"headers": {"Content-Length": "7"}},
        {"body": b"short"},
        {"body": b"too much data"},
        {"headers": {"Content-Encoding": "gzip"}},
        {"status": 403, "body": b"private-detail"},
        {"status": 302, "headers": {"Location": "https://127.0.0.1/secret"}},
    ],
)
def test_failure_never_reports_complete_or_retries(tmp_path, monkeypatch, response):
    client, opened, requests, _ = transport(monkeypatch, [response])
    with pytest.raises(ContextError) as caught:
        invoke(tmp_path, client, expected_bytes=8)
    assert len(requests) == 1 and all(item.closed for item in opened)
    assert "private-detail" not in str(caught.value)


def test_each_redirect_rechecks_host_and_keeps_credentials_absent(tmp_path, monkeypatch):
    client, opened, requests, _ = transport(
        monkeypatch, [{"status": 307, "headers": {"Location": "https://cdn.playwright.dev/b"}}, {}]
    )
    invoke(tmp_path, client, expected_bytes=8)
    assert [request[2]["Host"] for request in requests] == ["cdn.example", "cdn.playwright.dev"]
    assert all(item.closed for item in opened)


def test_cancel_after_headers_closes_socket_before_body(tmp_path, monkeypatch):
    client, opened, _, reads = transport(monkeypatch, [{}])
    checks = []

    def cancel():
        checks.append(True)
        if len(checks) == 2:
            raise ContextError("runtime_install_cancelled", "cancelled")

    fd = os.open(tmp_path / "new-private", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(ContextError) as caught:
            streams.download_into(client, "https://cdn.example/a", fd, expected_bytes=8, check_cancel=cancel)
    finally:
        os.close(fd)
    assert caught.value.code == "runtime_install_cancelled" and not reads and opened[0].closed


def test_nonempty_or_permissive_destination_refused_before_network(tmp_path, monkeypatch):
    client, opened, _, _ = transport(monkeypatch, [])
    fd = os.open(tmp_path / "existing", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"retained")
        with pytest.raises(ContextError):
            streams.download_into(
                client, "https://cdn.example/a", fd, expected_bytes=8, check_cancel=lambda: None
            )
    finally:
        os.close(fd)
    assert not opened and (tmp_path / "existing").read_bytes() == b"retained"


def test_large_source_branch_uses_stream_and_scoped_private_archive(tmp_path, monkeypatch):
    body = b"synthetic archive"
    selected = plan(body)
    _, opened, _, _ = transport(monkeypatch, [{"body": body}])
    monkeypatch.setattr(downloads, "BUFFERED_DOWNLOAD_LIMIT", 1)
    monkeypatch.setattr(downloads.PublicHTTP, "get", lambda *a, **k: pytest.fail("buffered download"))
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        archive = source(selected)
        assert archive.read_bytes() == body and archive.stat().st_mode & 0o777 == 0o600
    assert not archive.exists() and not list(tmp_path.iterdir()) and opened[0].closed


def test_stream_hash_mismatch_leaves_no_installed_result(tmp_path, monkeypatch):
    selected = replace(plan(), sha256="0" * 64)
    transport(monkeypatch, [{"body": b"synthetic archive"}])
    monkeypatch.setattr(downloads, "BUFFERED_DOWNLOAD_LIMIT", 1)
    with downloads.RuntimeDownloads(tmp_path, catalog={selected.id: selected}) as source:
        with pytest.raises(ContextError) as caught:
            source(selected)
        assert caught.value.code == "runtime_download_integrity"
    assert not list(tmp_path.iterdir())


def test_partial_writes_preserve_all_bytes(tmp_path, monkeypatch):
    client, opened, _, _ = transport(monkeypatch, [{"body": b"original"}])
    original_write = os.write
    monkeypatch.setattr(streams.os, "write", lambda fd, data: original_write(fd, data[:2]))
    assert invoke(tmp_path, client, expected_bytes=8)[0] == 8
    assert (tmp_path / "new-private").read_bytes() == b"original" and opened[0].closed


def test_deadline_before_connection_does_not_retry(tmp_path, monkeypatch):
    client, opened, _, _ = transport(monkeypatch, [])
    readings = iter([0.0, 301.0])
    monkeypatch.setattr(streams.time, "monotonic", lambda: next(readings))
    with pytest.raises(ContextError) as caught:
        invoke(tmp_path, client, expected_bytes=8)
    assert caught.value.code == "runtime_download_failed" and not opened


def test_deadline_during_stream_closes_connection(tmp_path, monkeypatch):
    client, opened, _, reads = transport(monkeypatch, [{}])
    readings = iter([0.0, 1.0, 301.0])
    monkeypatch.setattr(streams.time, "monotonic", lambda: next(readings))
    with pytest.raises(ContextError) as caught:
        invoke(tmp_path, client, expected_bytes=8)
    assert caught.value.code == "runtime_download_failed" and not reads and opened[0].closed
