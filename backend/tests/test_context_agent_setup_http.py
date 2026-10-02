"""Authenticated static handoffs: original fixtures, no platform/model requests."""

import hashlib
import json
import socket
import subprocess
import time
import urllib.request
from contextlib import closing

import pytest
import requests
from fastapi.testclient import TestClient

from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.infrastructure.system_secrets import SystemSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import COOKIE_NAME, create_app
from collection_context.interfaces.security import AccessPolicy, Credential, Session
from collection_context.library.store import LibraryStore

TOKENS = {
    "owner": "synthetic-owner-" + "o" * 40,
    "viewer": "synthetic-viewer-" + "v" * 40,
    "reader": "synthetic-reader-" + "r" * 40,
}


def files_snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture(params=[False, True], ids=["managed", "legacy-readonly"])
def setup_web(tmp_path, request):
    legacy = request.param
    workspace = tmp_path / "产品访问配置"
    with closing(LibraryStore.initialize(workspace)) as store:
        if not legacy:
            store.upsert(
                {"native_id": "123", "title": "原创 UI 教程", "body": "原创演示 Cerulean"},
                kind="saved",
                scope_id="s_saved",
            )
    vault = None
    if legacy:
        vault = tmp_path / "旧资料库"
        inbox = vault / "00_素材收件箱/抖音"
        inbox.mkdir(parents=True)
        (inbox / "原创教程.md").write_text(
            "# 原创旧库教程\n\n## 基本信息\n- 平台: 抖音\n"
            "- 链接: https://www.douyin.com/video/123\n- 来源: 收藏\n\n"
            "## 原始材料\n原创演示 Cerulean\n",
            encoding="utf-8",
        )
    policy = AccessPolicy(
        "http://127.0.0.1:18798",
        [
            Credential.from_token(
                "p_owner",
                TOKENS["owner"],
                permissions=frozenset({"collections:read", "ui:view", "ui:manage"}),
            ),
            Credential.from_token(
                "p_viewer", TOKENS["viewer"], permissions=frozenset({"collections:read", "ui:view"})
            ),
            Credential.from_token("p_reader", TOKENS["reader"]),
        ],
    )
    with TestClient(create_app(workspace, policy, legacy_vault=vault), base_url=policy.origin) as client:
        yield client, policy, workspace, vault


def login(client, role="owner"):
    response = client.post("/v1/session", json={"token": TOKENS[role]})
    assert response.status_code == 200
    return response.json()["data"]


def assert_static_data(data, policy, workspace, vault):
    assert data["library_mode"] == ("legacy_readonly" if vault else "managed")
    assert data["http"]["base_url"] == policy.origin
    assert data["http"]["authentication"] == "Authorization: Bearer <专用只读产品口令>"
    assert data["http"]["read_paths"] == [
        {"method": "POST", "path": "/v1/collections/search"},
        {"method": "POST", "path": "/v1/collections/read"},
        {"method": "GET", "path": "/v1/collections/{material_ref}/status"},
    ]
    assert data["read_only_tools"] == ["search_collections", "read_collection", "collection_status"]
    assert data["model_requests"] == data["platform_requests"] == 0
    assert data["distribution_verified"] is False
    assert data["mcp"]["same_computer_only"] is True
    assert data["mcp"]["available"] is True
    server = data["mcp"]["configuration"]["mcpServers"]["collection-context"]
    assert server["args"][-(3 if vault else 2) :] == (
        ["--workspace", str(vault), "--legacy-vault"] if vault else ["--workspace", str(workspace)]
    )
    encoded = json.dumps(data, ensure_ascii=False)
    if vault:
        assert str(workspace) not in encoded
    for token in TOKENS.values():
        assert token not in encoded
    for forbidden in ("--token", "--allow-add", "csrf_token", "secret_sha256", "原始材料", "Cerulean"):
        assert forbidden not in encoded


@pytest.mark.parametrize("role", ["owner", "viewer"])
def test_owner_and_readonly_ui_cookies_receive_actual_service_configuration(setup_web, role):
    client, policy, workspace, vault = setup_web
    login(client, role)
    response = client.get("/v1/agent-setup")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert_static_data(response.json()["data"], policy, workspace, vault)


def test_no_session_denied_without_exposing_local_paths(setup_web):
    client, _, workspace, vault = setup_web
    response = client.get("/v1/agent-setup")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert str(workspace) not in response.text and (vault is None or str(vault) not in response.text)


@pytest.mark.parametrize("role", ["owner", "viewer", "reader"])
@pytest.mark.parametrize("with_cookie", [False, True])
def test_all_bearers_denied_even_owner_or_with_valid_browser_cookie(setup_web, role, with_cookie):
    client, _, workspace, vault = setup_web
    if with_cookie:
        login(client)
    response = client.get("/v1/agent-setup", headers={"Authorization": "Bearer " + TOKENS[role]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert str(workspace) not in response.text and (vault is None or str(vault) not in response.text)
    assert TOKENS[role] not in response.text


def test_low_permission_cannot_login_or_use_a_low_permission_cookie(setup_web):
    client, policy, _, _ = setup_web
    assert client.post("/v1/session", json={"token": TOKENS["reader"]}).status_code == 403
    # Direct policy injection models a stale/lowered-privilege session, not login bypass.
    policy.sessions["synthetic-low-session"] = Session("p_reader", time.monotonic() + 60, "synthetic-csrf")
    client.cookies.set(COOKIE_NAME, "synthetic-low-session")
    response = client.get("/v1/agent-setup")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


@pytest.mark.parametrize(
    "path,content",
    [
        ("/v1/agent-setup?workspace=/untrusted", None),
        ("/v1/agent-setup?origin=https://untrusted.example", None),
        ("/v1/agent-setup", b"{}"),
        ("/v1/agent-setup", b" "),
        ("/v1/agent-setup", b'{"command":"untrusted"}'),
    ],
)
def test_get_rejects_all_query_and_body_configuration(setup_web, path, content):
    client, _, _, _ = setup_web
    login(client)
    response = client.request("GET", path, content=content)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"
    assert "/untrusted" not in response.text and "untrusted.example" not in response.text


def test_origin_is_canonical_policy_not_forwarded_header(setup_web):
    client, policy, workspace, vault = setup_web
    login(client)
    response = client.get(
        "/v1/agent-setup",
        headers={"X-Forwarded-Host": "untrusted.example", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200
    assert_static_data(response.json()["data"], policy, workspace, vault)
    assert client.get("/v1/agent-setup", headers={"Host": "untrusted.example"}).status_code == 403


def test_setup_never_mutates_files_reads_keys_or_starts_external_operations(setup_web, monkeypatch):
    client, policy, workspace, vault = setup_web
    login(client)
    before = files_snapshot(workspace)
    legacy_before = files_snapshot(vault) if vault else None

    def forbidden(*args, **kwargs):
        pytest.fail("agent setup must not execute, read credentials, or write the library")

    with monkeypatch.context() as guarded:
        guarded.setattr(LibraryStore, "transact", forbidden)
        guarded.setattr(AccessRegistry, "create", forbidden)
        guarded.setattr(SafeFiles, "write", forbidden)
        guarded.setattr(FileSecrets, "get", forbidden)
        guarded.setattr(FileSecrets, "put", forbidden)
        guarded.setattr(SystemSecrets, "get", forbidden)
        guarded.setattr(SystemSecrets, "put", forbidden)
        guarded.setattr(requests.Session, "request", forbidden)
        guarded.setattr(urllib.request, "urlopen", forbidden)
        guarded.setattr(subprocess, "Popen", forbidden)
        guarded.setattr(socket, "create_connection", forbidden)
        response = client.get("/v1/agent-setup")
        assert response.status_code == 200
        assert_static_data(response.json()["data"], policy, workspace, vault)
    assert files_snapshot(workspace) == before
    if vault:
        assert files_snapshot(vault) == legacy_before


def test_agent_setup_does_not_break_normal_legacy_or_managed_http_reads(setup_web):
    client, _, _, _ = setup_web
    session = login(client)
    assert client.get("/v1/agent-setup").status_code == 200
    headers = {"X-CSRF-Token": session["csrf_token"]}
    result = client.post("/v1/collections/search", json={"query": "Cerulean"}, headers=headers)
    assert result.status_code == 200
    ref = result.json()["data"]["items"][0]["material_ref"]
    original = client.post(
        "/v1/collections/read", json={"material_ref": ref, "artifact": "original"}, headers=headers
    )
    assert original.status_code == 200 and "Cerulean" in original.json()["data"]["text"]
    assert client.get(f"/v1/collections/{ref}/status").status_code == 200
