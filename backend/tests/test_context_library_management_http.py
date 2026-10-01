from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.store import LibraryStore

OWNER = "owner_fixture_" + "o" * 40
READER = "reader_fixture_" + "r" * 40
PREFIX = "/v1/management/library/"


@pytest.fixture
def client(tmp_path):
    store = LibraryStore.initialize(tmp_path / "HTTP 管理演示库")
    item = store.upsert(
        {"native_id": "95000001", "title": "导出合成教程", "body": "原文保持"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "已保存文字",
        processor_version="fixture",
        expected_content_hash=item["content_hash"],
    )
    policy = AccessPolicy(
        "http://127.0.0.1:8787",
        [
            Credential.from_token(
                "p_owner", OWNER, permissions=frozenset({"collections:read", "ui:view", "ui:manage"})
            ),
            Credential.from_token("p_reader", READER, permissions=frozenset({"collections:read", "ui:view"})),
        ],
    )
    with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as web:
        csrf = web.post("/v1/session", json={"token": OWNER}).json()["data"]["csrf_token"]
        yield web, store, item["id"], {"X-CSRF-Token": csrf}, policy
    store.close()


def test_owner_export_confirmation_download_and_no_paths(client):
    web, _, ref, headers, _ = client
    preview = web.post(PREFIX + "export-preview", json={"material_ref": ref}, headers=headers)
    assert preview.status_code == 200
    payload = {
        "material_ref": ref,
        "media_scope": "none",
        "preview_token": preview.json()["data"]["preview_token"],
        "confirmed": True,
    }
    download = web.post(PREFIX + "export", json=payload, headers=headers)
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert download.headers["cache-control"] == "no-store"
    assert "attachment" in download.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.read("artifacts/screen.md").decode() == "已保存文字"
    assert b".context/" not in download.content
    payload["confirmed"] = False
    assert not web.post(PREFIX + "export", json=payload, headers=headers).json()["ok"]


def test_exclusion_restore_read_visibility_immediately(client):
    web, _, ref, headers, _ = client
    for excluded in (True, False):
        payload = {"material_ref": ref, "excluded": excluded}
        preview = web.post(PREFIX + "exclusion-preview", json=payload, headers=headers).json()["data"]
        assert web.post(
            PREFIX + "exclusion-confirm",
            json={**payload, "preview_token": preview["preview_token"], "confirmed": True},
            headers=headers,
        ).json()["ok"]
        assert web.get(f"/v1/collections/{ref}/status").status_code == (404 if excluded else 200)
        listing = web.post(PREFIX + "excluded", json={}, headers=headers).json()["data"]
        assert listing["total_items"] == int(excluded)


def test_reader_and_owner_bearer_cannot_use_management(client):
    web, _, ref, headers, _ = client
    assert web.post(PREFIX + "overview", json={}).status_code == 403  # CSRF
    for token in (OWNER, READER):
        assert (
            web.post(PREFIX + "overview", json={}, headers={"Authorization": "Bearer " + token}).status_code
            == 403
        )
    csrf = web.post("/v1/session", json={"token": READER}).json()["data"]["csrf_token"]
    assert (
        web.post(
            PREFIX + "export-preview", json={"material_ref": ref}, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"material_ref": "invalid", "path": "/etc/passwd"},
        {"material_ref": []},
        {"material_ref": "invalid", "media_scope": []},
        {},
    ],
)
def test_no_arbitrary_paths_missing_fields_or_invalid_types(client, payload):
    web, _, _, headers, _ = client
    assert web.post(PREFIX + "export-preview", json=payload, headers=headers).status_code == 400


def test_revoked_session_and_query_are_rejected(client):
    web, _, _, headers, policy = client
    assert web.post(PREFIX + "overview?path=secret", json={}, headers=headers).status_code == 400
    policy.revoke("p_owner")
    assert web.post(PREFIX + "overview", json={}, headers=headers).status_code == 401
