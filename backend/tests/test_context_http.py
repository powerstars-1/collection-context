from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from collection_context.application.contracts import ContextError
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import HttpBoundary, create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore

READER = "fixture_reader_" + "r" * 40
OWNER = "fixture_owner_" + "o" * 40


@pytest.fixture
def web(tmp_path):
    store = LibraryStore.initialize(tmp_path / "资料库")
    item = store.upsert(
        {"native_id": "123", "title": "原创协议测试 UI", "body": "不是实际抓取内容"},
        kind="liked",
        scope_id="s_likes",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "Tailwind 1024，禁止额外文字。",
        processor_version="fixture_v1",
        expected_content_hash=item["content_hash"],
        coverage={"accuracy": "synthetic"},
    )
    FileIndex(store).rebuild()
    policy = AccessPolicy(
        "http://127.0.0.1:8787",
        [
            Credential.from_token("p_reader", READER),
            Credential.from_token("p_owner", OWNER, permissions=frozenset({"collections:read", "ui:view"})),
        ],
    )
    with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as client:
        yield client, policy, store, item
    store.close()


def auth(token=READER):
    return {"Authorization": "Bearer " + token}


def test_http_roundtrip_same_business_data_and_zero_writes(web):
    client, _, store, item = web
    before = store.snapshot()
    result = client.post("/v1/collections/search", json={"query": "UI 1024"}, headers=auth())
    assert result.status_code == 200
    assert result.json()["data"]["items"][0]["material_ref"] == item["id"]
    assert result.json()["data"]["model_requests"] == 0
    first = client.post(
        "/v1/collections/read",
        json={"material_ref": item["id"], "artifact": "screen", "max_chars": 5},
        headers=auth(),
    ).json()["data"]
    rest = client.post(
        "/v1/collections/read",
        json={
            "material_ref": item["id"],
            "artifact": "screen",
            "offset": first["next_offset"],
            "version": first["version"],
        },
        headers=auth(),
    ).json()["data"]
    assert first["text"] + rest["text"] == "Tailwind 1024，禁止额外文字。"
    status = client.get(f"/v1/collections/{item['id']}/status", headers=auth())
    assert status.json()["data"]["artifacts"]["audio"]["state"] == "missing"
    assert status.headers["cache-control"] == "no-store"
    assert status.headers["x-frame-options"] == "DENY"
    assert store.snapshot() == before


def test_ui_list_overview_and_static_assets(web):
    client, _, store, item = web
    listing = client.post("/v1/collections/list", json={}, headers=auth())
    assert listing.status_code == 200 and listing.json()["data"]["items"][0]["material_ref"] == item["id"]
    assert client.get("/v1/collections/overview", headers=auth()).json()["data"]["total_items"] == 1
    for path in ("/", "/connect", "/activity", "/assets/app.js", "/assets/app.css"):
        response = client.get(path)
        assert response.status_code == 200 and "content-security-policy" in response.headers
    assert client.get("/assets/http.py").status_code == 404
    assert client.post("/v1/collections/list", json={}).status_code == 401


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic invalid"}]
)
def test_authentication_is_required(web, headers):
    response = web[0].post("/v1/collections/search", json={"query": "UI"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_tokens_are_not_echoed_and_reader_cannot_create_ui_session(web):
    client = web[0]
    denied = client.post("/v1/session", json={"token": READER})
    assert denied.status_code == 403 and READER not in denied.text
    assert OWNER not in repr(web[1].credentials)


def test_host_origin_and_fetch_metadata_restrictions(web):
    client = web[0]
    for extra, expected in [
        ({"Host": "evil.example"}, "forbidden_host"),
        ({"Origin": "https://evil.example"}, "forbidden_origin"),
        ({"Origin": "null"}, "forbidden_origin"),
        ({"Sec-Fetch-Site": "cross-site"}, "forbidden_origin"),
    ]:
        response = client.post("/v1/collections/search", json={"query": "UI"}, headers={**auth(), **extra})
        assert response.status_code == 403 and response.json()["error"]["code"] == expected
    # Forwarded headers do not override Host or request scheme.
    response = client.post(
        "/v1/collections/search",
        json={"query": "UI"},
        headers={**auth(), "Host": "evil.example", "X-Forwarded-Host": "127.0.0.1:8787"},
    )
    assert response.status_code == 403


def test_duplicate_authorization_is_rejected(web):
    response = web[0].post(
        "/v1/collections/search",
        json={"query": "UI"},
        headers=[("Authorization", "Bearer " + READER), ("Authorization", "Bearer " + OWNER)],
    )
    assert response.status_code == 400


def test_cookie_session_is_http_only_csrf_bound_and_revocable(web):
    client, policy, _, _ = web
    login = client.post("/v1/session", json={"token": OWNER})
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    csrf = login.json()["data"]["csrf_token"]
    assert client.get("/v1/session").json()["data"]["principal"] == "p_owner"
    denied = client.post("/v1/collections/search", json={"query": "UI"})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "csrf_required"
    allowed = client.post("/v1/collections/search", json={"query": "UI"}, headers={"X-CSRF-Token": csrf})
    assert allowed.status_code == 200
    policy.revoke("p_owner")
    assert client.get("/v1/session").status_code == 401
    assert client.get("/v1/session", headers=auth(OWNER)).status_code == 401


def test_logout_invalidates_cookie_session(web):
    client = web[0]
    csrf = client.post("/v1/session", json={"token": OWNER}).json()["data"]["csrf_token"]
    assert client.post("/v1/session/logout", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/v1/session").status_code == 401


def test_file_revocation_refreshes_live_http_authorization(web):
    _, _, store, _ = web
    registry = AccessRegistry(store)
    access = registry.create("HTTP即时撤销验证", ui=True)
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    with TestClient(
        create_app(store.files.root, policy, refresh=lambda: registry.refresh(policy)), base_url=policy.origin
    ) as client:
        assert client.post("/v1/session", json={"token": access["token"]}).status_code == 200
        assert client.get("/v1/session").status_code == 200
        registry.revoke(access["principal"])
        assert client.get("/v1/session").status_code == 401
        assert client.get("/v1/session", headers=auth(access["token"])).status_code == 401


def test_corrupt_access_registry_fails_closed_not_using_previous_permissions(web):
    _, _, store, _ = web
    registry = AccessRegistry(store)
    access = registry.create("失败关闭验证")
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    with TestClient(
        create_app(store.files.root, policy, refresh=lambda: registry.refresh(policy)), base_url=policy.origin
    ) as client:
        assert client.get("/v1/session", headers=auth(access["token"])).status_code == 200
        store.files.write(".context/访问规则.json", b"{}", replace=True)
        assert client.get("/v1/session", headers=auth(access["token"])).status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "UI", "workspace": "/"},
        {"query": "UI", "limit": True},
        {"query": "UI", "filters": {"root": "/"}},
    ],
)
def test_unknown_or_invalid_arguments_cannot_expand_scope(web, payload):
    response = web[0].post("/v1/collections/search", json=payload, headers=auth())
    assert response.status_code == 400 and not response.json()["ok"]


def test_body_limits_and_json_validation(web):
    client = web[0]
    assert (
        client.post(
            "/v1/collections/search",
            content="x" * 65537,
            headers={**auth(), "Content-Type": "application/json"},
        ).status_code
        == 413
    )
    for body in ('{"query":"UI","query":"other"}', '{"query":NaN}', "[]", "not json"):
        response = client.post(
            "/v1/collections/search", content=body, headers={**auth(), "Content-Type": "application/json"}
        )
        assert response.status_code == 400
    assert client.post("/v1/collections/search", content="query=UI", headers=auth()).status_code == 415
    assert (
        client.post("/v1/collections/search?workspace=/", json={"query": "UI"}, headers=auth()).status_code
        == 400
    )


def test_missing_artifact_returns_real_error_not_success(web):
    response = web[0].post(
        "/v1/collections/read", json={"material_ref": web[3]["id"], "artifact": "audio"}, headers=auth()
    )
    assert response.status_code == 404 and response.json()["error"]["code"] == "artifact_missing"


def test_no_write_endpoint_or_arbitrary_file_route(web):
    client = web[0]
    for path in (
        "/v1/sync-jobs",
        "/v1/processing-jobs",
        "/v1/settings",
        "/.context/提交/CURRENT.json",
        "/docs",
    ):
        assert client.get(path, headers=auth()).status_code == 404


def test_policy_remote_requires_https_and_no_duplicate_tokens(tmp_path):
    credentials = [Credential.from_token("p_reader", READER)]
    with pytest.raises(ContextError):
        AccessPolicy("http://example.com:8787", credentials)
    with pytest.raises(ContextError):
        AccessPolicy("http://example.com:8787", credentials, remote=True)
    with pytest.raises(ContextError):
        AccessPolicy("http://127.0.0.1:8787", [*credentials, Credential.from_token("p_other", READER)])
    store = LibraryStore.initialize(tmp_path / "remote")
    policy = AccessPolicy("https://context.example", credentials, remote=True)
    with TestClient(create_app(store.files.root, policy), base_url="http://context.example") as client:
        assert client.get("/health").status_code == 403
        with TestClient(create_app(store.files.root, policy), base_url="https://context.example") as secure:
            assert secure.get("/health").status_code == 200
    store.close()


def test_rate_and_session_storage_are_bounded():
    policy = AccessPolicy(
        "http://127.0.0.1:8787", [Credential.from_token("p_reader", READER)], requests_per_minute=2
    )
    assert policy.rate_allowed("p_reader", now=100)
    assert policy.rate_allowed("p_reader", now=101)
    assert not policy.rate_allowed("p_reader", now=102)
    assert policy.rate_allowed("p_reader", now=161)
    for number in range(1100):
        policy.rate_allowed(str(number), now=170)
    assert len(policy.rates) == 1024


def test_actual_stream_limit_not_just_content_length():
    policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_reader", READER)])
    called = []

    async def inner(*_):
        called.append(True)

    boundary = HttpBoundary(inner, policy)

    async def run():
        chunks = iter(
            [
                {"type": "http.request", "body": b"x" * 33000, "more_body": True},
                {"type": "http.request", "body": b"x" * 33000},
            ]
        )
        sent = []

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        await boundary(
            {
                "type": "http",
                "scheme": "http",
                "path": "/v1/collections/search",
                "method": "POST",
                "headers": [
                    (b"host", b"127.0.0.1:8787"),
                    (b"authorization", ("Bearer " + READER).encode()),
                    (b"content-type", b"application/json"),
                ],
                "client": ("127.0.0.1", 1),
            },
            receive,
            send,
        )
        assert sent[0]["status"] == 413
        assert not called

    asyncio.run(run())
