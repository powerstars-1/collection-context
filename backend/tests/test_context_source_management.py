"""Owner page source registration and timers; zero browser, secret or cloud access."""

import json

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_scheduling import env as env

from collection_context.application.source_management import SourceManagement
from collection_context.workflows.source_schedule import SourceSchedule
from collection_context.workflows.synchronization import SynchronizationWorkflow


def create(managed, name="original_creator", **options):
    return post(
        managed,
        "source-create",
        {
            "creator_url": f"https://www.douyin.com/user/{name}?share_token=do-not-save",
            "limit": 5,
            "download": False,
            **options,
        },
    )


def configure_timer(managed, config_id, **options):
    return post(
        managed,
        "source-timer",
        {
            "config_id": config_id,
            "enabled": True,
            "interval_minutes": 60,
            "reset_blocked": False,
            "source_confirmed": True,
            **options,
        },
    )


def test_source_page_creates_original_scope_without_execution(managed, env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Page planning must not read secrets, browse or send requests")

    monkeypatch.setattr(env[1], "get", forbidden)
    monkeypatch.setattr("collection_context.processing.models.CloudModelClient._send", forbidden)
    monkeypatch.setattr("collection_context.sources.browser_source.DouyinBrowserSource.__init__", forbidden)
    value = create(managed).json()["data"]
    assert value["creator_url"] == "https://www.douyin.com/user/original_creator"
    assert value["model_requests"] == 0
    state = env[0].snapshot()
    assert not state["jobs"] and not state["settings"]["auto_sync"] and not state["settings"]["auto_process"]
    data = post(managed, "sources", {}).json()["data"]
    assert data["worker_online"] is False and data["scopes"][0]["status"] == "not_started"
    assert data["scopes"][0]["timer"] is None and not data["scopes"][0]["complete"]
    assert "do-not-save" not in json.dumps(state)


def test_repeat_registration_never_overwrites_different_fixed_scope(managed, env):
    first = create(managed).json()["data"]
    assert create(managed).json()["data"] == first
    before = env[0].snapshot()
    result = create(managed, limit=10).json()
    assert result["error"]["code"] == "source_scope_exists" and env[0].snapshot() == before


def test_manual_source_needs_authority_and_is_atomic_idempotent(managed, env):
    config = create(managed).json()["data"]["config_id"]
    args = {"config_id": config, "idempotency_key": "same-source-operation", "source_confirmed": False}
    before = env[0].snapshot()
    assert post(managed, "source-submit", args).json()["error"]["code"] == "source_authorization_required"
    assert env[0].snapshot() == before
    args["source_confirmed"] = True
    first = post(managed, "source-submit", args).json()["data"]
    assert first["state"] == "queued" and first["model_requests"] == 0
    assert post(managed, "source-submit", args).json()["data"] == first
    before = env[0].snapshot()
    args["idempotency_key"] = "another-click"
    assert post(managed, "source-submit", args).json()["error"]["code"] == "source_sync_busy"
    assert env[0].snapshot() == before
    scope = post(managed, "sources", {}).json()["data"]["scopes"][0]
    assert scope["pending_count"] == 1 and scope["latest_job"] == {
        "job_id": first["job_id"],
        "state": "queued",
        "error_code": None,
    }
    assert not next(iter(before["jobs"].values()))["calls"]


def test_manual_submission_of_old_page_config_is_refused(managed, env):
    old = create(managed).json()["data"]
    SynchronizationWorkflow(env[0]).configure("creator", creator_url=old["creator_url"], limit=6)
    before = env[0].snapshot()
    result = post(
        managed,
        "source-submit",
        {"config_id": old["config_id"], "idempotency_key": "old-page", "source_confirmed": True},
    )
    assert result.json()["error"]["code"] == "source_plan_changed" and env[0].snapshot() == before


def test_timer_pause_does_not_cancel_manual_or_authorize_models(managed, env):
    config = create(managed).json()["data"]["config_id"]
    before = env[0].snapshot()
    assert (
        configure_timer(managed, config, source_confirmed=False).json()["error"]["code"]
        == "source_authorization_required"
    )
    assert env[0].snapshot() == before
    assert configure_timer(managed, config).json()["data"]["enabled_count"] == 1
    job = post(
        managed, "source-submit", {"config_id": config, "idempotency_key": "manual", "source_confirmed": True}
    ).json()["data"]
    assert (
        configure_timer(managed, config, enabled=False, source_confirmed=False).json()["data"][
            "enabled_count"
        ]
        == 0
    )
    data = post(managed, "sources", {}).json()["data"]
    assert data["scopes"][0]["timer"]["enabled"] is False and data["scopes"][0]["pending_count"] == 1
    assert env[0].snapshot()["jobs"][job["job_id"]]["state"] == "queued"
    assert not env[0].snapshot()["settings"]["auto_process"]


def test_blocked_timer_requires_explicit_reset_and_retains_old_record(managed, env):
    config = create(managed).json()["data"]["config_id"]
    configure_timer(managed, config)
    workflow = SynchronizationWorkflow(env[0])
    schedule = SourceSchedule(workflow)
    original = schedule.admit_due()[0]
    workflow.jobs.cancel(original)
    configure_timer(managed, config)
    assert schedule.admit_due() == []
    data = post(managed, "sources", {}).json()["data"]["scopes"][0]
    assert data["timer"]["blocked_job_id"] == original
    assert configure_timer(managed, config, reset_blocked=True).json()["ok"]
    retry = schedule.admit_due()[0]
    assert retry != original and env[0].snapshot()["jobs"][original]["state"] == "cancelled"


def test_fixed_timer_revision_is_visible_and_pinned_pause_survives(managed, env):
    original = create(managed).json()["data"]
    configure_timer(managed, original["config_id"])
    revised = SynchronizationWorkflow(env[0]).configure(
        "creator", creator_url=original["creator_url"], limit=6
    )
    data = post(managed, "sources", {}).json()["data"]["scopes"][0]
    assert data["config_id"] == revised["config_id"] and not data["timer_matches_current"]
    assert configure_timer(managed, original["config_id"], enabled=False, source_confirmed=False).json()["ok"]
    assert (
        SourceSchedule(SynchronizationWorkflow(env[0])).status()["scopes"][0]["config_id"]
        == original["config_id"]
    )


def test_bounded_scope_page_no_account_or_cookie_disclosure(env):
    workflow = SynchronizationWorkflow(env[0])
    for n in range(25):
        workflow.configure("creator", creator_url=f"https://www.douyin.com/user/creator_{n}")
    account = "s_private_account_never_return"
    workflow.configure("saved", account_ref=account)
    data = SourceManagement(env[0]).overview()
    later = SourceManagement(env[0]).overview(offset=20)
    assert data["total_scopes"] == 26 and len(data["scopes"]) == 20 and data["next_offset"] == 20
    assert len(later["scopes"]) == 6 and later["next_offset"] is None
    assert account not in json.dumps([data, later]) and "account_ref" not in json.dumps([data, later])


@pytest.mark.parametrize(
    "action,args",
    [
        ("sources", {"offset": True}),
        ("sources", {"offset": -1}),
        ("source-create", {"creator_url": "https://untrusted.invalid", "limit": 5, "download": False}),
        ("source-create", {"creator_url": "https://www.douyin.com/video/123", "limit": 5, "download": False}),
        ("source-create", {"creator_url": "https://v.douyin.com/abc/", "limit": 5, "download": False}),
        ("source-create", {"creator_url": "https://www.douyin.com/user/abc", "limit": 21, "download": False}),
        ("source-create", {"creator_url": "https://www.douyin.com/user/abc", "limit": 5, "download": "yes"}),
        (
            "source-create",
            {
                "creator_url": "https://www.douyin.com/user/abc",
                "limit": 5,
                "download": False,
                "account_ref": "s_other",
            },
        ),
        ("source-submit", {"config_id": "../../secret", "idempotency_key": "x", "source_confirmed": True}),
        ("source-submit", {"config_id": "y_missing", "idempotency_key": "x", "source_confirmed": 1}),
        (
            "source-timer",
            {
                "config_id": "y_missing",
                "enabled": True,
                "interval_minutes": 60,
                "reset_blocked": False,
                "source_confirmed": "yes",
            },
        ),
    ],
)
def test_invalid_source_management_never_writes(managed, env, action, args):
    before = env[0].snapshot()
    assert not post(managed, action, args).json()["ok"] and env[0].snapshot() == before


@pytest.mark.parametrize(
    "action,args",
    [
        ("sources", {}),
        (
            "source-create",
            {"creator_url": "https://www.douyin.com/user/original", "limit": 5, "download": False},
        ),
        ("source-submit", {"config_id": "y_missing", "idempotency_key": "x", "source_confirmed": True}),
        (
            "source-timer",
            {
                "config_id": "y_missing",
                "enabled": True,
                "interval_minutes": 60,
                "reset_blocked": False,
                "source_confirmed": True,
            },
        ),
    ],
)
def test_source_management_requires_owner_cookie_csrf_not_ai_bearer(managed, env, action, args):
    client, headers, _, owner, _, viewer = managed
    before = env[0].snapshot()
    url = "/v1/management/" + action
    assert client.post(url, json=args).status_code == 403
    assert (
        client.post(url, json=args, headers={**headers, "Origin": "https://untrusted.invalid"}).status_code
        == 403
    )
    assert (
        client.post(url, json=args, headers={"Authorization": "Bearer " + owner["token"]}).status_code == 403
    )
    csrf = client.post("/v1/session", json={"token": viewer["token"]}).json()["data"]["csrf_token"]
    assert client.post(url, json=args, headers={"X-CSRF-Token": csrf}).status_code == 403
    assert env[0].snapshot() == before
