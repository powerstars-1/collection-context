"""Optional AI addition boundaries with fictional local sources and no paid calls."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_context_media import PNG
from test_context_model_registry import requests
from test_context_scheduling import env as env
from test_context_sources import raw_item

from collection_context.application.agent_addition import AgentAdditionGateway
from collection_context.application.contracts import ContextError
from collection_context.cli import no_model_authority
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.interfaces.access import ACCESS_FILE, AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.douyin import normalize_item
from collection_context.workflows.addition import AdditionWorkflow, link_plan
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.jobs import JobManager
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def ai_add(tmp_path):
    store = LibraryStore.initialize(tmp_path / "原创AI添加库")
    registry = AccessRegistry(store)
    reader = registry.create("只读宿主")
    agent = registry.create("可添加宿主", add=True)
    other = registry.create("另一个可添加宿主", add=True)
    seen = []
    source = SimpleNamespace(fetch_item=lambda *a, **k: normalize_item(raw_item()))

    @contextmanager
    def factory():
        seen.append("open")
        try:
            yield source
        finally:
            seen.append("close")

    workflow = AdditionWorkflow(store, factory, agent_authority=registry.authorize_add)
    value = SimpleNamespace(
        store=store,
        registry=registry,
        reader=reader,
        agent=agent,
        other=other,
        source=source,
        seen=seen,
        factory=factory,
        workflow=workflow,
        gateway=AgentAdditionGateway(workflow, agent["principal"]),
    )
    try:
        yield value
    finally:
        store.close()


def add(value, native="81", key="original-one", gateway=None):
    return (gateway or value.gateway).dispatch(
        "add_collection", {"url": f"https://www.douyin.com/video/{native}", "idempotency_key": key}
    )


def worker(value, *, authority=True):
    return BackgroundWorker(
        ExtractionWorkflow(value.store, no_model_authority),
        SynchronizationWorkflow(value.store, value.factory),
        addition_authority=value.registry.authorize_add if authority else None,
    )


def assert_error(result, code):
    assert result["ok"] is False and result["error"]["code"] == code


def test_readonly_default_and_explicit_grant_are_separate(ai_add):
    value = ai_add
    assert value.reader["permissions"] == ["collections:read"]
    assert set(value.agent["permissions"]) == {"collections:read", "collections:add"}
    reader = AgentAdditionGateway(value.workflow, value.reader["principal"])
    assert_error(add(value, gateway=reader), "permission_denied")
    assert not value.store.snapshot()["jobs"] and not value.seen
    result = add(value)
    assert result["ok"] and result["data"]["max_calls"] == 0
    assert result["data"]["download"] == "not_requested"
    assert result["data"]["model_requests"] == 0
    assert not value.store.snapshot()["items"] and not value.seen
    serialized = json.dumps(value.store.snapshot()) + value.store.files.read(ACCESS_FILE).decode()
    assert value.agent["token"] not in serialized


@pytest.mark.parametrize(
    "extra",
    [
        {"download": True},
        {"principal": "local_owner"},
        {"max_calls": 1},
        {"model": "paid"},
        {"browser_dir": "/other"},
        {"allow_model_calls": True},
        {"source_confirmed": True},
        {"workspace": "/"},
    ],
)
def test_public_fields_cannot_grant_media_model_or_path_authority(ai_add, extra):
    result = ai_add.gateway.dispatch(
        "add_collection",
        {
            "url": "https://www.douyin.com/video/81",
            "idempotency_key": "strict",
            **extra,
        },
    )
    assert_error(result, "invalid_argument")
    assert not ai_add.store.snapshot()["jobs"] and not ai_add.seen


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": "https://www.douyin.com/video/81"},
        {"url": 81, "idempotency_key": "wrong"},
        {"url": "https://www.douyin.com/video/81", "idempotency_key": True},
        {"url": "https://www.douyin.com/video/81", "idempotency_key": "x" * 129},
        {"url": "https://www.douyin.com/video/81", "idempotency_key": ""},
    ],
)
def test_missing_or_invalid_fields_fail_without_job(ai_add, payload):
    assert not ai_add.gateway.dispatch("add_collection", payload)["ok"]
    assert not ai_add.store.snapshot()["jobs"]


@pytest.mark.parametrize("action", ["sync", "delete", "process", "cancel", "retry", "settings"])
def test_optional_gateway_never_exposes_other_write_actions(ai_add, action):
    assert_error(ai_add.gateway.dispatch(action, {}), "permission_denied")
    assert not ai_add.store.snapshot()["jobs"]


def test_idempotency_is_identity_scoped_and_parameters_cannot_change(ai_add):
    first = add(ai_add)
    assert add(ai_add)["data"]["job_id"] == first["data"]["job_id"]
    assert_error(add(ai_add, native="82"), "idempotency_conflict")
    other = AgentAdditionGateway(ai_add.workflow, ai_add.other["principal"])
    second = add(ai_add, gateway=other)
    assert second["ok"] and second["data"]["job_id"] != first["data"]["job_id"]
    assert len(ai_add.store.snapshot()["jobs"]) == 2
    assert_error(other.dispatch("get_job", {"job_id": first["data"]["job_id"]}), "not_found")
    assert_error(ai_add.gateway.dispatch("get_job", {"job_id": second["data"]["job_id"]}), "not_found")


def test_own_job_is_public_subset_and_nonadmitted_own_job_is_hidden(ai_add):
    job_id = add(ai_add)["data"]["job_id"]
    public = ai_add.gateway.dispatch("get_job", {"job_id": job_id})
    assert public["ok"] and public["data"]["state"] == "queued"
    assert not {"principal", "payload", "calls", "token", "source_url"} & public["data"].keys()
    jobs = JobManager(ai_add.store)
    foreign_owner = jobs.submit("add", {}, idempotency_key="owner-only")
    nonadmitted = jobs.submit(
        "add",
        {"link": link_plan("https://www.douyin.com/video/82", False)},
        idempotency_key="not-admitted",
        principal=ai_add.agent["principal"],
    )
    for identity in (foreign_owner["id"], nonadmitted["id"]):
        assert_error(ai_add.gateway.dispatch("get_job", {"job_id": identity}), "not_found")


def test_five_pending_cap_duplicate_retry_and_other_identity(ai_add):
    original = None
    for i in range(5):
        result = add(ai_add, native=str(81 + i), key=f"cap-{i}")
        assert result["ok"]
        original = original or result["data"]["job_id"]
    assert add(ai_add, key="cap-0")["data"]["job_id"] == original
    assert_error(add(ai_add, native="99", key="sixth"), "agent_queue_limited")
    other = AgentAdditionGateway(ai_add.workflow, ai_add.other["principal"])
    assert add(ai_add, native="99", key="sixth", gateway=other)["ok"]
    ai_add.workflow.jobs.cancel(original, principal=ai_add.agent["principal"])
    assert add(ai_add, native="99", key="sixth")["ok"]


def test_explicit_download_cannot_bypass_gateway_at_workflow(ai_add):
    with pytest.raises(ContextError) as caught:
        ai_add.workflow.submit(
            url="https://www.douyin.com/video/81",
            download=True,
            idempotency_key="hidden-download",
            source_confirmed=True,
            principal=ai_add.agent["principal"],
        )
    assert caught.value.code == "permission_denied"
    assert not ai_add.store.snapshot()["jobs"]


def test_revoked_queued_job_blocks_before_browser_and_retry_is_denied(ai_add):
    identity = add(ai_add)["data"]["job_id"]
    ai_add.registry.revoke(ai_add.agent["principal"])
    assert_error(add(ai_add), "permission_denied")
    assert_error(ai_add.gateway.dispatch("get_job", {"job_id": identity}), "permission_denied")
    result = worker(ai_add).serve(allow_model_calls=False, allow_source_sync=True, once=True)
    assert result["handled"] == 1
    job = ai_add.workflow.jobs.get(identity, principal=ai_add.agent["principal"])
    assert job["state"] == "blocked" and job["error"]["code"] == "permission_denied"
    assert not ai_add.seen and not ai_add.store.snapshot()["items"]


def test_revocation_during_observation_does_not_commit_material(ai_add):
    identity = add(ai_add)["data"]["job_id"]

    def observed(*args, **kwargs):
        ai_add.registry.revoke(ai_add.agent["principal"])
        with pytest.raises(ContextError) as caught:
            kwargs["cancelled"]()
        assert caught.value.code == "permission_denied"
        return normalize_item(raw_item())

    ai_add.source.fetch_item = observed
    result = ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    assert result["state"] == "blocked" and result["error"]["code"] == "permission_denied"
    assert not ai_add.store.snapshot()["items"] and ai_add.seen == ["open", "close"]


def test_each_guarded_commit_rechecks_current_access(ai_add):
    before = ai_add.store.snapshot()
    with ai_add.workflow.ingestion_store(ai_add.agent["principal"]) as guarded:

        def revoke_inside_mutation(state):
            # Simulate a permission change between admission and the guarded commit.
            ai_add.registry._save([])
            state["settings"]["should_never_commit"] = True

        with pytest.raises(ContextError) as caught:
            guarded.transact(revoke_inside_mutation)
        assert caught.value.code == "permission_denied"
    assert ai_add.store.snapshot() == before


def test_admission_rechecks_revocation_inside_submission_transaction(ai_add):
    checks = []

    def authority(principal):
        checks.append(principal)
        if len(checks) == 3:
            ai_add.registry._save([])
        ai_add.registry.authorize_add(principal)

    ai_add.workflow.agent_authority = authority
    before = ai_add.store.snapshot()
    assert_error(add(ai_add), "permission_denied")
    assert len(checks) == 3 and ai_add.store.snapshot() == before and not ai_add.seen


def test_idempotent_retry_rechecks_current_authority_inside_transaction(ai_add):
    add(ai_add)
    checks = []

    def authority(principal):
        checks.append(principal)
        if len(checks) == 3:
            ai_add.registry._save([])
        ai_add.registry.authorize_add(principal)

    ai_add.workflow.agent_authority = authority
    before = ai_add.store.snapshot()
    assert_error(add(ai_add), "permission_denied")
    assert len(checks) == 3 and ai_add.store.snapshot() == before


@pytest.mark.parametrize("marker_change", ["principal", "plan_hash", "missing"])
def test_invalid_durable_admission_marker_does_not_execute_or_expose_job(ai_add, marker_change):
    identity = add(ai_add)["data"]["job_id"]

    def change(state):
        if marker_change == "missing":
            state["agent_link_admissions"].pop(identity)
        else:
            state["agent_link_admissions"][identity][marker_change] = "p_not_authorized"

    ai_add.store.transact(change)
    assert_error(ai_add.gateway.dispatch("get_job", {"job_id": identity}), "not_found")
    result = worker(ai_add).serve(allow_model_calls=False, allow_source_sync=True, once=True)
    assert result["handled"] == 0 and not ai_add.seen
    assert ai_add.workflow.jobs.get(identity, principal=ai_add.agent["principal"])["state"] == "queued"


def test_revocation_between_ingestion_transactions_prevents_later_artifacts(ai_add, monkeypatch):
    identity = add(ai_add)["data"]["job_id"]
    original = LibraryStore.upsert

    def upsert_then_revoke(store, *args, **kwargs):
        result = original(store, *args, **kwargs)
        if store._mutation_guard is not None:
            ai_add.registry.revoke(ai_add.agent["principal"])
        return result

    monkeypatch.setattr(LibraryStore, "upsert", upsert_then_revoke)
    result = ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    assert result["state"] == "blocked" and result["error"]["code"] == "permission_denied"
    item = next(iter(ai_add.store.snapshot()["items"].values()))
    assert item["first_observed_principal"] == ai_add.agent["principal"]
    assert not item["artifacts"] and "source_asset_hash" not in item


def test_untrusted_runtime_authority_and_owner_identity_cannot_create_agent_gateway(ai_add):
    with pytest.raises(ContextError) as caught:
        AgentAdditionGateway(ai_add.workflow, "local_owner")
    assert caught.value.code == "permission_denied"
    workflow = AdditionWorkflow(ai_add.store, ai_add.factory)
    gateway = AgentAdditionGateway(workflow, ai_add.agent["principal"])
    assert_error(add(ai_add, gateway=gateway), "permission_denied")
    assert not ai_add.store.snapshot()["jobs"]


def test_revocation_when_source_closes_records_blocked_not_success(ai_add):
    @contextmanager
    def revoking_factory():
        try:
            yield ai_add.source
        finally:
            ai_add.registry.revoke(ai_add.agent["principal"])

    ai_add.workflow.source_factory = revoking_factory
    identity = add(ai_add)["data"]["job_id"]
    result = ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    assert result["state"] == "blocked" and result["error"]["code"] == "permission_denied"
    assert len(ai_add.store.snapshot()["items"]) == 1 and not result["calls"]


@pytest.mark.parametrize("resume_checkpoint", [False, True])
def test_success_commit_checks_revocation_inside_transaction(ai_add, monkeypatch, resume_checkpoint):
    identity = add(ai_add)["data"]["job_id"]
    original = ai_add.workflow.jobs.finish
    if resume_checkpoint:
        monkeypatch.setattr(
            ai_add.workflow.jobs,
            "finish",
            lambda *a, **k: (_ for _ in ()).throw(SystemExit()),
        )
        with pytest.raises(SystemExit):
            ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
        with ExecutorLease(ai_add.store.files.root) as lease:
            ai_add.workflow.jobs.recover_interrupted(lease=lease, job_ids={identity})
    authorized_commits = []

    def revoked_finish(*args, **kwargs):
        authorization = kwargs.get("authorization")
        if authorization is not None:

            def revoke_at_atomic_authorization():
                # The callback runs inside the job finish writer transaction.
                authorized_commits.append(True)
                ai_add.registry._save([])
                authorization()

            kwargs["authorization"] = revoke_at_atomic_authorization
        return original(*args, **kwargs)

    monkeypatch.setattr(ai_add.workflow.jobs, "finish", revoked_finish)
    result = ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    assert authorized_commits == [True]
    assert result["state"] == "blocked" and result["error"]["code"] == "permission_denied"
    assert len(ai_add.store.snapshot()["items"]) == 1 and not result["calls"]
    assert ai_add.seen == ["open", "close"]


def test_agent_material_keeps_own_observer_and_no_model_or_download(ai_add, monkeypatch):
    monkeypatch.setattr(
        "collection_context.sources.downloads.DouyinDownloads.fetch",
        lambda *a: pytest.fail("AI metadata addition must not download"),
    )
    identity = add(ai_add)["data"]["job_id"]
    result = ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    assert result["state"] == "succeeded" and not result["calls"]
    item = next(iter(ai_add.store.snapshot()["items"].values()))
    assert item["first_observed_principal"] == ai_add.agent["principal"]
    assert item["relations"] and all(r["action_at"] is None for r in item["relations"].values())
    assert "original" in item["artifacts"] and "prepared_input" not in item
    assert "synthetic-secret" not in json.dumps(result) + json.dumps(item)


def test_agent_discovery_does_not_inherit_owner_automatic_fee_policy(env, monkeypatch):
    store, _, _, extraction, schedule = env
    registry = AccessRegistry(store)
    agent = registry.create("费用隔离宿主", add=True)
    schedule.configure(True, allow_model_calls=True, max_calls=2)
    source = SimpleNamespace(
        fetch_item=lambda *a, **k: normalize_item(
            raw_item(aweme_type=68, images=[{"url_list": ["https://example.douyinvod.com/page"]}])
        )
    )

    @contextmanager
    def factory():
        yield source

    addition = AdditionWorkflow(store, factory, agent_authority=registry.authorize_add)
    gateway = AgentAdditionGateway(addition, agent["principal"])
    result = gateway.dispatch(
        "add_collection",
        {
            "url": "https://www.douyin.com/video/81",
            "idempotency_key": "fee-proof",
        },
    )
    addition.run(result["data"]["job_id"], principal=agent["principal"])
    item = next(iter(store.snapshot()["items"].values()))
    PreparedInputs(store).prepare_images(item["id"], [(PNG, "image/png")])
    sent = requests(monkeypatch)
    assert schedule.admit_new() == []
    assert BackgroundWorker(extraction).serve(allow_model_calls=True, once=True)["handled"] == 0
    assert not sent and len(store.snapshot()["jobs"]) == 1


def test_worker_requires_both_source_authority_and_agent_admission(ai_add):
    identity = add(ai_add)["data"]["job_id"]
    assert worker(ai_add).serve(allow_model_calls=True, once=True)["handled"] == 0
    assert (
        worker(ai_add, authority=False).serve(allow_model_calls=False, allow_source_sync=True, once=True)[
            "handled"
        ]
        == 0
    )
    jobs = JobManager(ai_add.store)
    other_jobs = []
    for kind, payload in (
        ("process", {"extraction": {}}),
        ("sync", {"sync": {}}),
        ("add", {"link": link_plan("https://www.douyin.com/video/82", False)}),
    ):
        other_jobs.append(
            jobs.submit(kind, payload, idempotency_key=f"foreign-{kind}", principal=ai_add.other["principal"])
        )
    result = worker(ai_add).serve(allow_model_calls=True, allow_source_sync=True, once=True, max_jobs=20)
    assert result["handled"] == 1
    assert jobs.get(identity, principal=ai_add.agent["principal"])["state"] == "succeeded"
    assert all(
        jobs.get(j["id"], principal=ai_add.other["principal"])["state"] == "queued" for j in other_jobs
    )


def test_worker_recovers_only_admitted_agent_jobs_and_owner_jobs(ai_add, monkeypatch):
    identity = add(ai_add)["data"]["job_id"]
    original = ai_add.workflow.jobs.finish
    monkeypatch.setattr(ai_add.workflow.jobs, "finish", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        ai_add.workflow.run(identity, principal=ai_add.agent["principal"])
    monkeypatch.setattr(ai_add.workflow.jobs, "finish", original)
    foreign = ai_add.workflow.jobs.submit(
        "process", {"extraction": {}}, idempotency_key="foreign-running", principal=ai_add.other["principal"]
    )
    ai_add.workflow.jobs.start(foreign["id"], principal=ai_add.other["principal"])
    owner = ai_add.workflow.submit(
        url="https://www.douyin.com/video/83",
        download=False,
        idempotency_key="owner-regression",
        source_confirmed=True,
    )
    ai_add.source.fetch_item = lambda *a, **k: normalize_item(raw_item(aweme_id="83"))
    result = worker(ai_add).serve(allow_model_calls=False, allow_source_sync=True, once=True, max_jobs=20)
    assert result["handled"] == 2
    assert ai_add.workflow.jobs.get(identity, principal=ai_add.agent["principal"])["state"] == "succeeded"
    assert ai_add.workflow.jobs.get(owner["id"])["state"] == "succeeded"
    assert ai_add.workflow.jobs.get(foreign["id"], principal=ai_add.other["principal"])["state"] == "running"
    assert ai_add.seen == ["open", "close", "open", "close"]


def test_revoked_interrupted_agent_is_recovered_then_blocked_without_source(ai_add):
    identity = add(ai_add)["data"]["job_id"]
    ai_add.workflow.jobs.start(identity, principal=ai_add.agent["principal"])
    ai_add.registry.revoke(ai_add.agent["principal"])
    events = []
    result = worker(ai_add).serve(
        allow_model_calls=False, allow_source_sync=True, once=True, emit=events.append
    )
    assert result["handled"] == 1 and not ai_add.seen
    assert any(e["event"] == "recovered" and e["job_id"] == identity for e in events)
    assert ai_add.workflow.jobs.get(identity, principal=ai_add.agent["principal"])["state"] == "blocked"


@pytest.fixture
def agent_web(ai_add):
    from fastapi.testclient import TestClient

    policy = AccessPolicy("http://127.0.0.1:8787", ai_add.registry.credentials())
    with TestClient(
        create_app(ai_add.store.files.root, policy, refresh=lambda: ai_add.registry.refresh(policy)),
        base_url=policy.origin,
    ) as client:
        yield client


def bearer(record):
    return {"Authorization": "Bearer " + record["token"]}


def test_http_returns_202_own_status_and_does_not_wait_for_source(ai_add, agent_web):
    payload = {"url": "https://www.douyin.com/video/81", "idempotency_key": "http-one"}
    assert agent_web.post("/v1/collections", json=payload).status_code == 401
    assert agent_web.post("/v1/collections", json=payload, headers=bearer(ai_add.reader)).status_code == 403
    result = agent_web.post("/v1/collections", json=payload, headers=bearer(ai_add.agent))
    assert result.status_code == 202 and result.json()["ok"]
    identity = result.json()["data"]["job_id"]
    assert not ai_add.seen and not ai_add.store.snapshot()["items"]
    assert (
        agent_web.get(f"/v1/jobs/{identity}", headers=bearer(ai_add.agent)).json()["data"]["state"]
        == "queued"
    )
    assert agent_web.get(f"/v1/jobs/{identity}", headers=bearer(ai_add.other)).status_code == 404
    assert agent_web.get(f"/v1/jobs/{identity}", headers=bearer(ai_add.reader)).status_code == 403
    assert (
        agent_web.get(f"/v1/jobs/{identity}?principal=local_owner", headers=bearer(ai_add.agent)).status_code
        == 400
    )
    assert (
        agent_web.post(
            "/v1/collections", json={**payload, "download": True}, headers=bearer(ai_add.agent)
        ).status_code
        == 400
    )
    assert (
        agent_web.post("/v1/management/link-submit", json=payload, headers=bearer(ai_add.agent)).status_code
        == 403
    )
    ai_add.registry.revoke(ai_add.agent["principal"])
    assert agent_web.get(f"/v1/jobs/{identity}", headers=bearer(ai_add.agent)).status_code == 401


def test_http_cookie_requires_ui_and_csrf_even_for_explicit_add(ai_add, agent_web):
    assert agent_web.post("/v1/session", json={"token": ai_add.agent["token"]}).status_code == 403
    ui_agent = ai_add.registry.create("页面可添加宿主", ui=True, add=True)
    login = agent_web.post("/v1/session", json={"token": ui_agent["token"]})
    assert login.status_code == 200
    payload = {"url": "https://www.douyin.com/video/81", "idempotency_key": "csrf-one"}
    assert agent_web.post("/v1/collections", json=payload).status_code == 403
    allowed = agent_web.post(
        "/v1/collections", json=payload, headers={"X-CSRF-Token": login.json()["data"]["csrf_token"]}
    )
    assert allowed.status_code == 202 and not ai_add.seen
