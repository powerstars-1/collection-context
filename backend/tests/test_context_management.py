"""Owner-only management shares durable planning, with no secret resolution or paid requests."""

import json

import pytest
from test_context_scheduling import env as env
from test_context_scheduling import prepared

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential


@pytest.fixture
def managed(env):
    store = env[0]
    registry = AccessRegistry(store)
    owner = registry.create("主人管理", ui=True, manage=True)
    reader = registry.create("AI只读")
    viewer = registry.create("页面只读", ui=True)
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    with TestClient(
        create_app(store.files.root, policy, refresh=lambda: registry.refresh(policy)), base_url=policy.origin
    ) as client:
        csrf = client.post("/v1/session", json={"token": owner["token"]}).json()["data"]["csrf_token"]
        yield client, {"X-CSRF-Token": csrf}, registry, owner, reader, viewer


def post(managed, action, body):
    return managed[0].post("/v1/management/" + action, json=body, headers=managed[1])


def test_explicit_management_permission_never_inherited_by_existing_tokens(env):
    registry = AccessRegistry(env[0])
    read = registry.create("默认只读")
    view = registry.create("页面只读", ui=True)
    assert read["permissions"] == ["collections:read"]
    assert "ui:manage" not in view["permissions"]
    with pytest.raises(ContextError):
        registry.create("隐式提升", manage=True)
    with pytest.raises(ContextError):
        Credential.from_token("p_bad", "z" * 40, permissions=frozenset({"ui:manage"}))


def test_manager_bearer_is_not_a_management_ai_api(managed, env):
    for token in (managed[3]["token"], managed[4]["token"]):
        result = managed[0].post(
            "/v1/management/overview", json={}, headers={"Authorization": "Bearer " + token}
        )
        assert result.status_code == 403
    assert not env[0].snapshot()["jobs"]


def test_viewer_page_session_cannot_manage_even_with_valid_csrf(managed, env):
    csrf = managed[0].post("/v1/session", json={"token": managed[5]["token"]}).json()["data"]["csrf_token"]
    result = managed[0].post(
        "/v1/management/automatic",
        json={"enabled": False, "max_calls": 2, "max_new_tasks": 5, "fee_confirmed": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert result.status_code == 403
    assert env[0].snapshot()["settings"]["auto_process"] is False


def test_management_csrf_origin_and_live_revocation(managed):
    client, headers, registry, owner, *_ = managed
    assert client.post("/v1/management/overview", json={}).status_code == 403
    assert (
        client.post(
            "/v1/management/overview", json={}, headers={**headers, "Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert post(managed, "overview", {}).status_code == 200
    registry.revoke(owner["principal"])
    assert post(managed, "overview", {}).status_code == 401


def test_auto_requires_fee_authority_and_only_registers_rule(managed, env):
    before = env[0].snapshot()
    args = {"enabled": True, "max_calls": 2, "max_new_tasks": 3, "fee_confirmed": False}
    assert post(managed, "automatic", args).json()["error"]["code"] == "processing_authorization_required"
    assert env[0].snapshot() == before
    args["fee_confirmed"] = True
    result = post(managed, "automatic", args)
    assert result.status_code == 200 and result.json()["data"]["max_new_tasks"] == 3
    assert not env[0].snapshot()["jobs"]
    args.update(enabled=False, fee_confirmed=False)
    assert post(managed, "automatic", args).json()["data"]["enabled"] is False


def test_selected_history_is_atomic_idempotent_and_does_not_call_models(managed, env, monkeypatch):
    _, identity = prepared(env)

    def denied(*args, **kwargs):
        raise AssertionError("Management must not resolve secrets or send model requests")

    monkeypatch.setattr(env[1], "get", denied)
    monkeypatch.setattr("collection_context.processing.models.CloudModelClient._send", denied)
    args = {"input_ids": [identity], "idempotency_key": "same-history", "max_calls": 2, "fee_confirmed": True}
    first = post(managed, "history", args).json()
    assert first["ok"] and first["data"]["count"] == 1 and first["data"]["max_calls"] == 2
    assert post(managed, "history", args).json()["data"] == first["data"]
    assert len(env[0].snapshot()["jobs"]) == 1
    job_id = first["data"]["job_ids"][0]
    assert env[3].executor.jobs.get(job_id)["state"] == "queued"
    result = post(managed, "cancel", {"job_id": job_id}).json()["data"]
    assert result["state"] == "cancelled" and result["recorded_calls"] == 0


def test_management_cannot_cancel_foreign_principal_jobs(managed, env):
    job = env[3].executor.jobs.submit("sync", {}, idempotency_key="foreign", principal="p_other")
    assert post(managed, "cancel", {"job_id": job["id"]}).status_code == 404
    assert env[3].executor.jobs.get(job["id"], principal="p_other")["state"] == "queued"


@pytest.mark.parametrize(
    "action,args",
    [
        ("overview", {"offset": True}),
        ("overview", {"offset": -1}),
        ("overview", {"shell": "ignored"}),
        ("automatic", {"enabled": True}),
        ("automatic", {"enabled": "yes", "max_calls": 2, "max_new_tasks": 5, "fee_confirmed": True}),
        ("automatic", {"enabled": True, "max_calls": 2, "max_new_tasks": 5, "fee_confirmed": "yes"}),
        ("history", {"input_ids": [], "idempotency_key": "x", "max_calls": 2, "fee_confirmed": True}),
        ("history", {"input_ids": [], "idempotency_key": "x", "max_calls": 2, "fee_confirmed": False}),
        ("run", {}),
        ("cancel", {"job_id": "../../key"}),
    ],
)
def test_invalid_management_arguments_never_write(managed, env, action, args):
    before = env[0].snapshot()
    assert not post(managed, action, args).json()["ok"]
    assert env[0].snapshot() == before


def test_bounded_public_management_result_no_payload_or_credentials(env):
    manager = ManagementService(env[0])
    for n in range(25):
        env[3].executor.jobs.submit(
            "sync", {"fixture_secret": "secret-body-never-public"}, idempotency_key=str(n)
        )
    data = manager.overview()
    assert len(data["jobs"]) == 20 and data["next_offset"] == 20 and data["total_jobs"] == 25
    assert len(manager.overview(offset=20)["jobs"]) == 5
    assert "secret-body-never-public" not in json.dumps(data)
    assert "payload" not in data["jobs"][0] and data["jobs"][0]["calls"] == []
    limited = manager._automatic({"paused_job_ids": ["j_" + str(n) for n in range(30)], "paused_count": 30})
    assert len(limited["paused_job_ids"]) == 20 and limited["paused_count"] == 30


def test_selected_resume_shares_preview_guard_and_does_not_run(managed, env):
    env[4].configure(True, allow_model_calls=True, max_calls=2)
    prepared(env)
    job_id = env[4].admit_new()[0]
    env[4].configure(False)
    env[4].configure(True, allow_model_calls=True, max_calls=2)
    preview = post(managed, "resume-preview", {"job_ids": [job_id]}).json()["data"]
    result = post(
        managed,
        "resume",
        {"job_ids": [job_id], "preview_token": preview["preview_token"], "fee_confirmed": True},
    )
    assert result.status_code == 200
    assert env[3].executor.jobs.get(job_id)["state"] == "queued"
