"""Actual fixed-plan/owner HTTP recovery with original media and injected transports, never cloud."""

import copy

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_media import PNG
from test_context_model_registry import configure, requests
from test_context_scheduling import env as env
from test_context_scheduling import prepared

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.model_recovery import ModelRecovery
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.policy import execution_allowed
from collection_context.workflows.worker import BackgroundWorker


def interrupted(env, monkeypatch, outcome="unknown"):
    _, identity = prepared(env)
    workflow = env[3]
    job = workflow.submit(identity, idempotency_key="original", max_calls=2)

    def first(sent):
        if len(sent) != 1:
            return
        if outcome == "cancel":
            workflow.executor.jobs.cancel(job["id"])
        elif outcome == "failed":
            raise ContextError("upstream_rejected", "原创模拟明确失败。")
        else:
            raise ContextError("upstream_outcome_unknown", "原创模拟结果未明。", possibly_charged=True)

    sent = requests(monkeypatch, first)
    result = workflow.run(job["id"])
    vision = next(d["name"] for d in result["payload"]["plan"] if d["paid"])
    return result, vision, sent


def arguments(preview, *, key="retry-once"):
    return {
        "job_id": preview["job_id"],
        "stages": preview["selected_stages"],
        "preview_token": preview["preview_token"],
        "idempotency_key": key,
        "max_calls": preview["max_calls"],
        "fee_confirmed": True,
        "reviewed_call_ids": [c["call_id"] for c in preview["unknown_calls"]],
        "duplicate_charge_confirmed": bool(preview["unknown_calls"]),
    }


def test_owner_http_unknown_preview_requires_each_review_and_explicit_risk_then_runs_once(
    env, managed, monkeypatch
):
    original, vision, sent = interrupted(env, monkeypatch)
    old = copy.deepcopy(original)
    first = post(managed, "retry-preview", {"job_id": original["id"], "stages": [vision]}).json()
    assert first["ok"]
    preview = first["data"]
    assert preview["model_requests"] == 0 and len(sent) == 1
    assert preview["unknown_calls"][0]["call_id"] == original["calls"][0]["id"]
    args = arguments(preview)
    for changed in ({"duplicate_charge_confirmed": False}, {"reviewed_call_ids": []}):
        result = post(managed, "retry", {**args, **changed}).json()
        assert result["error"]["code"] == "unknown_review_required"
    assert len(env[0].snapshot()["jobs"]) == 1
    created = post(managed, "retry", args).json()
    assert created["ok"] and created["data"]["attempt"] == 2
    ref = created["data"]["job_id"]
    assert ref != original["id"] and created["data"]["parent_job_id"] == original["id"]
    assert post(managed, "retry", args).json()["data"]["job_id"] == ref
    assert len(env[0].snapshot()["jobs"]) == 2 and len(sent) == 1
    assert env[3].executor.jobs.get(original["id"]) == old
    result = BackgroundWorker(env[3]).serve(allow_model_calls=True, once=True, max_jobs=1)
    assert result["handled"] == 1
    done = env[3].executor.jobs.get(ref)
    assert done["state"] == "succeeded" and len(done["calls"]) == 2 and len(sent) == 3
    assert env[3].executor.jobs.get(original["id"]) == old


def test_cancelled_late_paid_result_is_reused_by_new_summary_attempt(env, managed, monkeypatch):
    original, _, sent = interrupted(env, monkeypatch, "cancel")
    assert original["state"] == "cancelled" and original["calls"][0]["state"] == "completed"
    before = copy.deepcopy(original)
    preview = post(managed, "retry-preview", {"job_id": original["id"], "stages": ["summary"]}).json()["data"]
    assert preview["unknown_calls"] == [] and preview["max_calls"] == 1
    configure(env[0], env[2], "summary", "new-default-must-not-be-used")
    created = post(managed, "retry", arguments(preview)).json()["data"]
    done = env[3].run(created["job_id"])
    assert done["state"] == "succeeded" and len(done["calls"]) == 1
    assert [r["model"] for r in sent] == ["vision-before", "summary-before"]
    assert env[3].executor.jobs.get(original["id"]) == before
    assert done["stages"][original["calls"][0]["stage"]]["result"] == original["calls"][0]["result"]


def test_unselected_failure_is_not_dispatched_by_targeted_summary_retry(env, managed, monkeypatch):
    original, vision, sent = interrupted(env, monkeypatch, "failed")
    assert original["state"] == "partial"
    preview = post(managed, "retry-preview", {"job_id": original["id"], "stages": [vision]}).json()["data"]
    # Retry the failed evidence and its dependent summary, not a successful paid stage.
    assert preview["affected_stages"][0] == vision
    rejected = post(managed, "retry-preview", {"job_id": original["id"], "stages": ["audio_not_applicable"]})
    assert rejected.json()["error"]["code"] == "invalid_retry_selection"
    created = post(managed, "retry", arguments(preview)).json()["data"]
    done = env[3].run(created["job_id"])
    assert done["state"] == "succeeded"
    assert len(done["calls"]) == 2 and len(sent) == len(original["calls"]) + 2


def test_targeted_retry_does_not_retry_other_failed_frames(env, managed, monkeypatch):
    item, _ = prepared(env)
    identity = PreparedInputs(env[0]).prepare_images(item["id"], [(PNG, "image/png"), (PNG, "image/png")])
    original = env[3].submit(identity, idempotency_key="two-gaps", max_calls=3)

    def fail_first_two(sent):
        if len(sent) <= 2:
            raise ContextError("upstream_rejected", "仅原创两页的明确失败。")

    sent = requests(monkeypatch, fail_first_two)
    original = env[3].run(original["id"])
    frames = [d["name"] for d in original["payload"]["plan"] if d["paid"] and d["name"] != "summary"]
    assert len(frames) == 2
    assert all(original["stages"][name]["state"] == "failed" for name in frames)
    preview = ModelRecovery(env[0]).preview(original["id"], [frames[0]])
    assert frames[1] not in preview["affected_stages"]
    ref = post(managed, "retry", arguments(preview)).json()["data"]["job_id"]
    before = len(sent)
    result = env[3].run(ref)
    assert result["state"] == "partial"
    assert result["stages"][frames[1]] == original["stages"][frames[1]]
    assert [call["stage"] for call in result["calls"]] == [frames[0], "summary"]
    assert len(sent) - before == 2


def test_unknown_review_does_not_unblock_other_jobs_or_new_unknown_call(env, managed, monkeypatch):
    original, vision, sent = interrupted(env, monkeypatch)
    recovery = ModelRecovery(env[0])
    preview = recovery.preview(original["id"], [vision])
    args = arguments(preview)
    ref = post(managed, "retry", args).json()["data"]["job_id"]
    # A different manually submitted attempt has no authorization and stays blocked.
    other = env[3].submit(
        original["payload"]["extraction"]["input_id"], idempotency_key="not-reviewed", max_calls=2
    )
    assert env[3].run(other["id"])["state"] == "blocked" and len(sent) == 1

    # Another unknown call added after review is NOT authorized by the earlier snapshot.
    def extra(state):
        call = copy.deepcopy(state["jobs"][original["id"]]["calls"][0])
        call["id"] = "c_new_unreviewed"
        state["jobs"][original["id"]]["calls"].append(call)

    env[0].transact(extra)
    assert env[3].run(ref)["state"] == "blocked" and len(sent) == 1


def test_changed_request_snapshot_invalidates_confirmation_without_new_attempt(env, managed, monkeypatch):
    original, vision, _ = interrupted(env, monkeypatch)
    preview = ModelRecovery(env[0]).preview(original["id"], [vision])
    env[0].transact(
        lambda state: state["jobs"][original["id"]]["calls"][0].update(upstream_request_id="late-id")
    )
    result = post(managed, "retry", arguments(preview)).json()
    assert result["error"]["code"] == "retry_preview_changed"
    assert len(env[0].snapshot()["jobs"]) == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ({"stages": []}, "invalid_retry_selection"),
        ({"max_calls": 0}, "retry_budget_insufficient"),
        ({"fee_confirmed": False}, "processing_authorization_required"),
        ({"reviewed_call_ids": "everything"}, "invalid_argument"),
        ({"duplicate_charge_confirmed": "yes"}, "processing_authorization_required"),
    ],
)
def test_recovery_rejects_implicit_invalid_or_insufficient_authority(env, managed, monkeypatch, change, code):
    original, vision, _ = interrupted(env, monkeypatch)
    preview = ModelRecovery(env[0]).preview(original["id"], [vision])
    before = env[0].snapshot()
    result = post(managed, "retry", {**arguments(preview), **change}).json()
    assert result["error"]["code"] == code
    assert env[0].snapshot() == before


def test_recovery_http_requires_owner_session_csrf_and_cannot_read_foreign_task(env, managed, monkeypatch):
    original, _, _ = interrupted(env, monkeypatch)
    client, headers, _, owner, reader, viewer = managed
    args = {"job_id": original["id"]}
    assert client.post("/v1/management/retry-preview", json=args).status_code == 403
    assert (
        client.post(
            "/v1/management/retry-preview", json=args, headers={"Authorization": "Bearer " + owner["token"]}
        ).status_code
        == 403
    )
    foreign = copy.deepcopy(original)
    foreign.update(id="j_foreign", principal="p_foreign")
    env[0].transact(lambda state: state["jobs"].update({foreign["id"]: foreign}))
    assert post(managed, "retry-preview", {"job_id": foreign["id"]}).status_code == 404
    csrf = client.post("/v1/session", json={"token": viewer["token"]}).json()["data"]["csrf_token"]
    assert (
        client.post("/v1/management/retry-preview", json=args, headers={"X-CSRF-Token": csrf}).status_code
        == 403
    )


def test_prepared_budget_uses_actual_registered_media_and_no_model_dispatch(env, monkeypatch):
    prepared(env)
    sent = requests(monkeypatch)
    value = ManagementService(env[0]).overview()["prepared"][0]
    assert value["audio_segments"] == 0 and value["visual_frames"] == 1
    assert value["planned_calls_before_reuse"] == 2 and sent == []


def test_retry_reviews_same_stage_unknown_even_when_other_plan_has_changed_summary_limits(
    env, managed, monkeypatch
):
    original, vision, sent = interrupted(env, monkeypatch)
    newer = env[3].submit(
        original["payload"]["extraction"]["input_id"],
        idempotency_key="different-summary-plan",
        max_calls=2,
        summary_input_chars=50_000,
    )
    assert env[3].run(newer["id"])["state"] == "blocked"
    preview = ModelRecovery(env[0]).preview(newer["id"], [vision])
    assert [call["call_id"] for call in preview["unknown_calls"]] == [original["calls"][0]["id"]]
    ref = post(managed, "retry", arguments(preview)).json()["data"]["job_id"]
    assert env[3].run(ref)["state"] == "succeeded"
    assert len(sent) == 3


@pytest.mark.parametrize("part", ["grant", "payload", "scope", "budget", "identity", "unknown"])
def test_retry_policy_binds_exact_job_plan_scope_calls_and_budget(env, managed, monkeypatch, part):
    original, vision, sent = interrupted(env, monkeypatch)
    preview = ModelRecovery(env[0]).preview(original["id"], [vision])
    ref = post(managed, "retry", arguments(preview)).json()["data"]["job_id"]
    state = env[0].snapshot()
    job = state["jobs"][ref]
    assert job["payload"]["dispatch"]["mode"] == "model_retry"
    assert execution_allowed(state, job)
    if part == "grant":
        state["model_retries"] = {}
    elif part == "payload":
        job["payload"]["extraction"]["summary_input_chars"] = 30_000
    elif part == "scope":
        job["recovery"]["affected_stages"].append("unselected")
    elif part == "budget":
        job["budget"]["max_calls"] += 1
    elif part == "identity":
        job["id"] = "j_copied_grant"
    elif part == "unknown":
        job["recovery"]["unknown_authorizations"] = []
    with pytest.raises(ContextError) as caught:
        execution_allowed(state, job)
    assert caught.value.code == "invalid_dispatch_policy"
    assert len(sent) == 1
