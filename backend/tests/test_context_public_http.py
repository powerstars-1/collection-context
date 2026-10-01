"""Pinned HTTPS transport tests; synthetic DNS/responses, zero external network."""

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import public_http
from collection_context.infrastructure.public_http import PublicHTTP, public_addresses


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.douyinvod.com/a",
        "https://douyinvod.com.evil.invalid/a",
        "https://127.0.0.1/a",
        "https://secret@cdn.douyinvod.com/a",
        "https://cdn.douyinvod.com:8443/a",
        "https://cdn.douyinvod.com/a#fragment",
        "https://cdn.douyinvod.com/a\r\nHeader:secret",
    ],
)
def test_url_allowlist(url):
    with pytest.raises(ContextError) as caught:
        PublicHTTP(hosts=frozenset(), suffixes=frozenset({"douyinvod.com"})).validate(url)
    assert caught.value.code == "unsafe_public_url"


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["169.254.169.254"], ["::1"], ["8.8.8.8", "10.0.0.1"], ["224.0.0.1"], []]
)
def test_dns_rejects_entire_answer_if_any_address_is_not_public(monkeypatch, addresses):
    monkeypatch.setattr(
        public_http.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, (ip, 443)) for ip in addresses],
    )
    with pytest.raises(ContextError) as caught:
        public_addresses("cdn.douyinvod.com")
    assert caught.value.code == "unsafe_public_address"


def transport(monkeypatch, responses):
    opened, requests = [], []
    monkeypatch.setattr(public_http, "public_addresses", lambda host: ("8.8.8.8",))

    class Response:
        def __init__(self, status=200, headers=None, body=b"synthetic"):
            self.status, self.headers, self.body = status, headers or {}, body

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

        def read1(self, size):
            result, self.body = self.body[:size], self.body[size:]
            return result

    class Connection:
        sock = None

        def __init__(self, host, address, timeout):
            self.host, self.address, self.closed = host, address, False
            opened.append(self)

        def request(self, method, path, headers):
            requests.append((method, path, headers))

        def getresponse(self):
            return Response(**responses.pop(0))

        def close(self):
            self.closed = True

    monkeypatch.setattr(public_http, "_PinnedHTTPS", Connection)
    return opened, requests


def test_download_pins_ip_and_does_not_send_credentials_or_inherit_proxy(monkeypatch):
    opened, requests = transport(
        monkeypatch, [{"headers": {"Content-Length": "9", "Content-Type": "video/mp4"}}]
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://fixture-secret@localhost:3128")
    result = PublicHTTP(hosts=frozenset({"cdn.example"})).get(
        "https://cdn.example/a?signature=synthetic", max_bytes=9
    )
    assert result.data == b"synthetic" and result.content_type == "video/mp4"
    assert opened[0].address == "8.8.8.8" and opened[0].closed
    assert set(requests[0][2]) == {"Host", "Accept-Encoding", "User-Agent"}
    assert "synthetic" not in repr(result)


def test_redirect_to_private_or_unlisted_host_is_rejected_before_connect(monkeypatch):
    opened, _ = transport(
        monkeypatch, [{"status": 302, "headers": {"Location": "https://127.0.0.1/private"}}]
    )
    with pytest.raises(ContextError) as caught:
        PublicHTTP(hosts=frozenset({"cdn.example"})).get("https://cdn.example/a")
    assert caught.value.code == "unsafe_public_url" and len(opened) == 1 and opened[0].closed


def test_each_redirect_gets_its_own_checked_dns_and_host_header(monkeypatch):
    opened, requests = transport(
        monkeypatch, [{"status": 302, "headers": {"Location": "https://other.example/b"}}, {}]
    )
    hosts = []
    monkeypatch.setattr(public_http, "public_addresses", lambda host: hosts.append(host) or ("8.8.8.8",))
    PublicHTTP(hosts=frozenset({"cdn.example", "other.example"})).get("https://cdn.example/a")
    assert hosts == ["cdn.example", "other.example"]
    assert [r[2]["Host"] for r in requests] == hosts and all(c.closed for c in opened)


@pytest.mark.parametrize(
    "response,code",
    [
        ({"headers": {"Content-Length": "500"}}, "download_limit"),
        ({"body": b"0123456789"}, "download_limit"),
        ({"headers": {"Content-Length": "8"}, "body": b"short"}, "download_incomplete"),
        ({"body": b""}, "download_incomplete"),
        ({"headers": {"Content-Encoding": "gzip"}}, "download_encoding_unsupported"),
        ({"status": 403, "body": b"fixture-secret"}, "download_rejected"),
    ],
)
def test_download_failure_never_returns_partial_blob_or_upstream_body(monkeypatch, response, code):
    opened, _ = transport(monkeypatch, [response])
    with pytest.raises(ContextError) as caught:
        PublicHTTP(hosts=frozenset({"cdn.example"})).get("https://cdn.example/a", max_bytes=9)
    assert caught.value.code == code and "fixture-secret" not in str(caught.value)
    assert all(c.closed for c in opened)
