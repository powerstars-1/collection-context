"""Old Markdown is read through the normal authenticated UI/API, with no write lane."""

from __future__ import annotations

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from collection_context.application.contracts import ContextError
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.interfaces.server import main
from collection_context.library.store import LibraryStore

TOKEN = "synthetic_legacy_owner_" + "x" * 40


@pytest.fixture
def legacy_web(tmp_path):
    vault = tmp_path / "旧资料"
    inbox = vault / "00_素材收件箱/抖音"
    inbox.mkdir(parents=True)
    (inbox / "原创教程.md").write_text(
        "# 原创旧库样例\n\n## 基本信息\n- 平台: 抖音\n"
        "- 链接: https://www.douyin.com/video/123\n"
        "- 附件目录: 80_附件/抖音/123\n- 来源: 收藏\n\n"
        "## 原始材料\n自己的演示配色 Cerulean\n\n## 用户备注\n周末试一试\n",
        encoding="utf-8",
    )
    attachments = vault / "80_附件/抖音/123"
    attachments.mkdir(parents=True)
    (attachments / "画面文字.md").write_text("卡片圆角 24px，留白 32px", encoding="utf-8")
    with closing(LibraryStore.initialize(tmp_path / "独立访问配置")) as store:
        authority = store.files.root
        before = store.snapshot()
    policy = AccessPolicy(
        "http://127.0.0.1:8787",
        [
            Credential.from_token(
                "p_owner",
                TOKEN,
                permissions=frozenset({"collections:read", "collections:add", "ui:view", "ui:manage"}),
            )
        ],
    )
    with TestClient(create_app(authority, policy, legacy_vault=vault), base_url=policy.origin) as client:
        yield client, vault, authority, before


def snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_authenticated_search_read_session_and_no_writes(legacy_web):
    client, vault, authority, before = legacy_web
    files_before = snapshot(vault)
    assert client.post("/v1/collections/search", json={"query": "24px"}).status_code == 401
    login = client.post("/v1/session", json={"token": TOKEN})
    session = login.json()["data"]
    assert session["library_mode"] == "legacy_readonly"
    assert set(session["permissions"]) == {"collections:read", "ui:view"}
    headers = {"X-CSRF-Token": session["csrf_token"]}
    result = client.post("/v1/collections/search", json={"query": "24px"}, headers=headers)
    assert result.status_code == 200
    ref = result.json()["data"]["items"][0]["material_ref"]
    read = client.post(
        "/v1/collections/read", json={"material_ref": ref, "artifact": "screen"}, headers=headers
    )
    assert read.json()["data"]["text"] == "卡片圆角 24px，留白 32px"
    assert client.get(f"/v1/collections/{ref}/status").json()["data"]["accuracy"] == "not_verified"
    assert client.get("/v1/collections/overview").json()["data"]["total_items"] == 1
    assert client.get("/v1/session").json()["data"]["library_mode"] == "legacy_readonly"
    assert client.post("/v1/collections/read", json={"material_ref": ref}).status_code == 403
    for path in ("/", "/assets/app.js", "/assets/app.css"):
        assert client.get(path).status_code == 200
    assert client.post("/v1/session/logout", json={}, headers=headers).status_code == 200
    assert client.get("/v1/session").status_code == 401
    assert snapshot(vault) == files_before
    with closing(LibraryStore(authority)) as store:
        assert store.snapshot() == before


@pytest.mark.parametrize(
    "path",
    [
        "/v1/collections",
        "/v1/management/library/edit-confirm",
        "/v1/management/library/summary-confirm",
        "/v1/management/auto",
        "/v1/management/source-connect",
        "/v1/management/models",
    ],
)
def test_even_owner_cannot_write_either_library(legacy_web, path):
    client, vault, authority, before = legacy_web
    files_before = snapshot(vault)
    session = client.post("/v1/session", json={"token": TOKEN}).json()["data"]
    result = client.post(path, json={}, headers={"X-CSRF-Token": session["csrf_token"]})
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "permission_denied"
    assert snapshot(vault) == files_before
    with closing(LibraryStore(authority)) as store:
        assert store.snapshot() == before


def test_disallowed_media_and_job_reads_do_not_use_authority_library(legacy_web):
    client, _, _, _ = legacy_web
    headers = {"Authorization": "Bearer " + TOKEN}
    for path in ("/v1/jobs/j_example", "/v1/collections/a_example/frames"):
        assert client.get(path, headers=headers).status_code == 403


def test_overlap_and_model_runtime_rejected_before_library_access(tmp_path):
    policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_owner", TOKEN)])
    for authority in (tmp_path, tmp_path / "config"):
        with pytest.raises(ContextError, match="独立目录"):
            create_app(authority, policy, legacy_vault=tmp_path)
    with pytest.raises(ContextError, match="不能开启"):
        create_app(tmp_path / "other", policy, legacy_vault=tmp_path / "legacy", connection_runner=object())
    assert not list(tmp_path.iterdir())


def test_server_denies_legacy_write_flags_before_loading_secrets(tmp_path, capsys):
    code = main(
        [
            "--workspace",
            str(tmp_path / "config"),
            "--legacy-vault",
            str(tmp_path / "legacy"),
            "--allow-model-config",
            "--credential-dir",
            str(tmp_path / "secrets"),
        ]
    )
    assert code == 1 and "permission_denied" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
