"""Model page authority, non-secret defaults, immutable queued work and conservative key rollback."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_model_registry import environment as environment
from test_context_model_registry import prepared, requests
from test_context_scheduling import env as env

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.model_setup import ModelSetup, separate_credentials
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.interfaces.server import main as server_main
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

KEY = "synthetic-owner-key-never-real"


def args(**options):
    return {
        "role": "vision",
        "base_url": "https://fixture.invalid/v1",
        "model": "original-vision",
        "protocol": "chat",
        "parameters": {"max_tokens": 300, "temperature": 0.2},
        "timeout": 120,
        "api_key": KEY,
        "expected_profile_id": None,
        "credential_confirmed": True,
        **options,
    }


@pytest.fixture
def setup_page(environment):
    store, secrets, _ = environment
    registry = AccessRegistry(store)
    owner = registry.create("模型主人", ui=True, manage=True)
    viewer = registry.create("模型只读", ui=True)
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    with TestClient(
        create_app(store.files.root, policy, model_secrets=secrets, refresh=lambda: registry.refresh(policy)),
        base_url=policy.origin,
    ) as client:
        csrf = client.post("/v1/session", json={"token": owner["token"]}).json()["data"]["csrf_token"]
        yield client, {"X-CSRF-Token": csrf}, registry, owner, viewer


def send(page, action, body):
    return page[0].post("/v1/management/" + action, headers=page[1], json=body)


def test_server_and_page_default_do_not_open_or_save_credentials(managed, env, capsys):
    value = post(managed, "models", {}).json()["data"]
    assert value["configuration_enabled"] is False and value["model_requests"] == 0
    assert "credential_ref" not in json.dumps(value)
    before = env[0].snapshot()
    assert post(managed, "model-save", args()).json()["error"]["code"] == "model_setup_disabled"
    assert env[0].snapshot() == before
    argv = ["--workspace", str(env[0].files.root)]
    assert server_main([*argv, "--allow-model-config"]) == 1
    assert "model_setup_disabled" in capsys.readouterr().err
    assert server_main([*argv, "--credential-dir", str(env[1].files.root)]) == 1
    assert "model_setup_disabled" in capsys.readouterr().err


def test_owner_save_stores_outside_library_never_resolves_or_requests(setup_page, environment, monkeypatch):
    store, secrets, _ = environment

    def forbidden(*a, **k):
        raise AssertionError("Setup does not read credentials or perform inference")

    monkeypatch.setattr(secrets, "get", forbidden)
    monkeypatch.setattr("collection_context.processing.models.CloudModelClient._send", forbidden)
    before_files = set(secrets.files.root.iterdir())
    result = send(setup_page, "model-save", args())
    assert result.status_code == 200 and result.json()["ok"]
    value = result.json()["data"]
    assert value["roles"]["vision"]["credential_state"] == "registered_not_verified"
    assert value["roles"]["vision"]["profile"]["model"] == "original-vision"
    assert "credential_ref" not in json.dumps(value) and KEY not in result.text
    files = set(secrets.files.root.iterdir()) - before_files
    assert len(files) == 1 and next(iter(files)).read_text() == KEY
    assert os.stat(next(iter(files))).st_mode & 0o777 == 0o600
    assert os.stat(secrets.files.root).st_mode & 0o777 == 0o700
    assert KEY not in json.dumps(store.snapshot()) and not store.snapshot()["jobs"]
    assert all(
        KEY.encode() not in file.read_bytes() for file in store.files.root.rglob("*") if file.is_file()
    )
    assert not store.snapshot()["settings"]["auto_process"]


def test_blank_reuses_key_rotation_retains_old_and_stale_page_creates_none(setup_page, environment):
    store, secrets, _ = environment
    first = send(setup_page, "model-save", args()).json()["data"]["roles"]["vision"]["profile_id"]
    original_ref = ModelCatalog(store).get(first)["credential_ref"]
    count = len(list(secrets.files.root.iterdir()))
    second = send(
        setup_page, "model-save", args(api_key="", model="original-vision-revised", expected_profile_id=first)
    ).json()["data"]["roles"]["vision"]["profile_id"]
    assert ModelCatalog(store).get(second)["credential_ref"] == original_ref
    assert len(list(secrets.files.root.iterdir())) == count
    before = store.snapshot()
    assert (
        send(setup_page, "model-save", args(expected_profile_id=first)).json()["error"]["code"]
        == "model_config_conflict"
    )
    assert store.snapshot() == before and len(list(secrets.files.root.iterdir())) == count
    third = send(
        setup_page, "model-save", args(api_key="synthetic-rotation-key", expected_profile_id=second)
    ).json()["data"]["roles"]["vision"]["profile_id"]
    assert ModelCatalog(store).get(third)["credential_ref"] != original_ref
    assert secrets.get(original_ref) == KEY and len(list(secrets.files.root.iterdir())) == count + 1


def test_default_change_does_not_mutate_or_break_already_queued_job(environment, monkeypatch):
    store, secrets, _ = environment
    setup = ModelSetup(store, secrets)
    setup.save(**args())
    setup.save(**args(role="summary", model="original-summary"))
    _, input_id = prepared(store)
    workflow = ExtractionWorkflow(store, secrets.get)
    job = workflow.submit(input_id, idempotency_key="fixed-owner-setup", max_calls=3)
    fixed = (
        job["payload"]["extraction"]["model_profiles"]
        if "extraction" in job["payload"]
        else job["payload"]["model_profiles"]
    )
    current = setup.settings()["roles"]["vision"]["profile_id"]
    setup.save(**args(model="changed-default", api_key="synthetic-rotated", expected_profile_id=current))
    assert workflow.executor.jobs.get(job["id"])["payload"] == job["payload"]
    assert ModelCatalog(store).get(fixed["vision"])["model"] == "original-vision"
    sent = requests(monkeypatch)
    assert workflow.run(job["id"])["state"] == "succeeded"
    assert [request["model"] for request in sent] == [
        "original-vision",
        "original-vision",
        "original-summary",
    ]


def test_atomic_compare_and_swap_precedes_private_key_save(environment):
    store, secrets, _ = environment

    def save(number):
        try:
            ModelSetup(store, secrets).save(**args(api_key=f"synthetic-concurrent-{number}"))
            return "saved"
        except ContextError as error:
            return error.code

    before = len(list(secrets.files.root.iterdir()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [1, 2]))
    assert results.count("saved") == 1
    assert set(results) <= {"saved", "model_config_conflict", "writer_unavailable", "writer_busy"}
    assert len(list(secrets.files.root.iterdir())) == before + 1
    assert save(3) == "model_config_conflict"
    assert len(list(secrets.files.root.iterdir())) == before + 1


@pytest.mark.parametrize("committed", [False, True])
def test_failed_commit_only_discards_proven_unreferenced_new_key(environment, monkeypatch, committed):
    store, secrets, original = environment
    saved = store.transact
    before = len(list(secrets.files.root.iterdir()))

    def failing(change):
        if committed:
            saved(change)
        else:
            change(store.snapshot())
        raise ContextError("storage_unavailable", "synthetic commit fault")

    monkeypatch.setattr(store, "transact", failing)
    with pytest.raises(ContextError) as error:
        ModelSetup(store, secrets).save(**args())
    assert error.value.code == "storage_unavailable"
    assert len(list(secrets.files.root.iterdir())) == before + int(committed)
    assert secrets.get(original) == "synthetic-key-not-real"
    if committed:
        identity = store.snapshot()["settings"]["model_roles"]["vision"]
        assert secrets.get(ModelCatalog(store).get(identity)["credential_ref"]) == KEY


def test_unknown_commit_retains_key_and_does_not_claim_safe_rollback(environment, monkeypatch):
    store, secrets, _ = environment
    snapshot = store.snapshot
    failed = [False]

    def read():
        if failed[0]:
            raise ContextError("storage_unavailable", "synthetic unavailable read")
        return snapshot()

    def failing(change):
        change(snapshot())
        failed[0] = True
        raise ContextError("storage_unavailable", "synthetic unknown commit")

    before = len(list(secrets.files.root.iterdir()))
    monkeypatch.setattr(store, "snapshot", read)
    monkeypatch.setattr(store, "transact", failing)
    with pytest.raises(ContextError) as error:
        ModelSetup(store, secrets).save(**args())
    assert error.value.code == "model_config_outcome_unknown" and KEY not in str(error.value)
    assert len(list(secrets.files.root.iterdir())) == before + 1


@pytest.mark.parametrize(
    "changes",
    [
        {"credential_confirmed": False},
        {"credential_confirmed": 1},
        {"api_key": ""},
        {"api_key": KEY + "\n"},
        {"api_key": None},
        {"role": "other"},
        {"base_url": "http://untrusted.invalid/v1"},
        {"base_url": "https://fixture.invalid/v1?key=" + KEY},
        {"model": KEY},
        {"parameters": {"temperature": KEY}},
        {"parameters": {"messages": []}},
        {"parameters": []},
        {"timeout": True},
        {"protocol": "transcription"},
        {"expected_profile_id": "../../secret"},
    ],
)
def test_invalid_save_does_not_write_or_echo(setup_page, environment, changes):
    store, secrets, _ = environment
    before, files = store.snapshot(), set(secrets.files.root.iterdir())
    result = send(setup_page, "model-save", args(**changes))
    assert not result.json()["ok"] and KEY not in result.text
    assert store.snapshot() == before and set(secrets.files.root.iterdir()) == files


@pytest.mark.parametrize("action,body", [("models", {}), ("model-save", args())])
def test_ai_bearer_read_session_bad_origin_csrf_and_revocation_cannot_configure(
    setup_page, environment, action, body
):
    client, headers, registry, owner, viewer = setup_page
    store, secrets, _ = environment
    before, files = store.snapshot(), set(secrets.files.root.iterdir())
    url = "/v1/management/" + action
    assert client.post(url, json=body).status_code == 403
    assert (
        client.post(url, json=body, headers={**headers, "Origin": "https://untrusted.invalid"}).status_code
        == 403
    )
    assert (
        client.post(url, json=body, headers={"Authorization": "Bearer " + owner["token"]}).status_code == 403
    )
    csrf = client.post("/v1/session", json={"token": viewer["token"]}).json()["data"]["csrf_token"]
    assert client.post(url, json=body, headers={"X-CSRF-Token": csrf}).status_code == 403
    client.post("/v1/session", json={"token": owner["token"]})
    registry.revoke(owner["principal"])
    assert client.post(url, json=body, headers=headers).status_code == 401
    assert store.snapshot() == before and set(secrets.files.root.iterdir()) == files


def test_overlapping_private_directory_rejected_before_creation_and_model_management(environment, capsys):
    store, _, _ = environment
    registry = AccessRegistry(store)
    registry.create("主人", ui=True, manage=True)
    inside = store.files.root / "must-not-create"
    for root in (inside, store.files.root, store.files.root.parent):
        with pytest.raises(ContextError):
            separate_credentials(store.files.root, root)
    assert (
        server_main(
            ["--workspace", str(store.files.root), "--allow-model-config", "--credential-dir", str(inside)]
        )
        == 1
    )
    assert "secret_directory_overlap" in capsys.readouterr().err and not inside.exists()
    secret = FileSecrets.initialize(store.files.root / "created-for-constructor-test")
    try:
        with pytest.raises(ContextError):
            ManagementService(store, model_secrets=secret)
    finally:
        secret.close()
