"""Own source timer contracts; no production clock overrides or platform/model calls."""

import json
from datetime import timedelta

import pytest
from test_context_synchronization import ACCOUNT
from test_context_synchronization import syncenv as syncenv

from collection_context.application.contracts import ContextError, utc_now
from collection_context.cli import main, no_model_authority
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.source_schedule import SourceSchedule, moment
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def timer(syncenv):
    current = [utc_now()]
    schedule = SourceSchedule(syncenv[1], clock=lambda: current[0])
    config = syncenv[1].configure("saved", account_ref=ACCOUNT.public()["account_ref"])
    return schedule, config, current


def enable(timer, **kwargs):
    return timer[0].configure(timer[1]["config_id"], enabled=True, allow_source_sync=True, **kwargs)


def advance(timer, minutes):
    timer[2][0] = (moment(timer[2][0]) + timedelta(minutes=minutes)).isoformat()


def test_default_disabled_and_idle_is_read_only(syncenv, timer):
    before = syncenv[0].snapshot()
    assert timer[0].status() == {"scopes": [], "enabled_count": 0, "model_requests": 0}
    assert timer[0].admit_due() == [] and syncenv[0].snapshot() == before
    assert not before["settings"]["auto_sync"] and not syncenv[3]


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"allow_source_sync": False},
        {"allow_source_sync": 1},
        {"allow_source_sync": True, "interval_minutes": 59},
        {"allow_source_sync": True, "interval_minutes": 1441},
        {"allow_source_sync": True, "interval_minutes": True},
        {"allow_source_sync": True, "reset_blocked": "yes"},
    ],
)
def test_invalid_or_unconfirmed_timer_never_writes(syncenv, timer, options):
    before = syncenv[0].snapshot()
    with pytest.raises(ContextError):
        timer[0].configure(timer[1]["config_id"], enabled=True, **options)
    assert syncenv[0].snapshot() == before and not syncenv[3]


def test_due_atomic_one_job_and_sleep_has_no_catchup(syncenv, timer):
    status = enable(timer)
    assert status["scopes"][0]["interval_minutes"] == 60
    assert not syncenv[0].snapshot()["settings"]["auto_process"]
    first = timer[0].admit_due()
    assert len(first) == 1 and not syncenv[3]
    before = syncenv[0].snapshot()
    advance(timer, 60 * 72)
    assert timer[0].admit_due() == [] and syncenv[0].snapshot() == before
    assert syncenv[1].run(first[0])["state"] == "succeeded"
    second = timer[0].admit_due()
    assert len(second) == 1 and second != first
    assert len(syncenv[0].snapshot()["jobs"]) == 2
    syncenv[1].run(second[0])
    assert len(syncenv[0].snapshot()["items"]) == 2
    before = syncenv[0].snapshot()
    assert timer[0].admit_due() == [] and syncenv[0].snapshot() == before
    assert all(not j["calls"] for j in before["jobs"].values())


def test_due_waits_from_completion_and_clock_reversal_no_burst(syncenv, timer):
    enable(timer)
    job = timer[0].admit_due()[0]
    syncenv[1].run(job)
    advance(timer, 59)
    assert timer[0].admit_due() == []
    advance(timer, -120)
    assert timer[0].admit_due() == []
    advance(timer, 121)
    # Real completion is later than the timer's first clock tick, so advance one more minute.
    advance(timer, 1)
    assert len(timer[0].admit_due()) == 1


def test_pause_blocks_queued_but_manual_is_independent(syncenv, timer):
    enable(timer)
    ref = timer[0].admit_due()[0]
    timer[0].configure(timer[1]["config_id"], enabled=False)
    with pytest.raises(ContextError, match="暂停"):
        syncenv[1].run(ref)
    manual = syncenv[1].submit(timer[1]["config_id"], idempotency_key="manual-while-paused")
    assert syncenv[1].run(manual["id"])["state"] == "succeeded"
    assert syncenv[1].jobs.get(ref)["state"] == "queued"
    enable(timer)
    assert timer[0].admit_due() == []  # Re-enable the same bounded checkpoint, not another batch.
    assert syncenv[1].run(ref)["state"] == "succeeded"


def test_global_pause_also_blocks_dispatch_without_touching_models(syncenv, timer):
    enable(timer)
    ref = timer[0].admit_due()[0]
    syncenv[0].transact(lambda state: state["settings"].update(auto_sync=False))
    before = syncenv[0].snapshot()
    assert timer[0].admit_due() == [] and syncenv[0].snapshot() == before
    assert not timer[0].status()["scopes"][0]["awaiting_worker"]
    with pytest.raises(ContextError):
        syncenv[1].run(ref)
    assert not syncenv[3] and not before["settings"]["auto_process"]


@pytest.mark.parametrize("state", ["blocked", "failed", "partial", "cancelled"])
def test_terminal_failure_cannot_timer_retry_without_explicit_reset(syncenv, timer, state):
    enable(timer)
    ref = timer[0].admit_due()[0]
    if state == "cancelled":
        syncenv[1].jobs.cancel(ref)
    else:
        syncenv[1].jobs.start(ref)
        syncenv[1].jobs.finish(ref, state)
    advance(timer, 60 * 72)
    assert timer[0].admit_due() == []
    enable(timer)
    assert timer[0].admit_due() == []
    assert timer[0].status()["scopes"][0]["blocked_job_id"] == ref
    enable(timer, reset_blocked=True)
    retry = timer[0].admit_due()
    assert len(retry) == 1 and retry != [ref]
    assert syncenv[1].jobs.get(ref)["state"] == state


def test_reset_same_timestamp_uses_new_atomic_sequence(syncenv, timer):
    enable(timer)
    first = timer[0].admit_due()[0]
    syncenv[1].jobs.cancel(first)
    enable(timer, reset_blocked=True)
    second = timer[0].admit_due()[0]
    assert second != first and syncenv[1].jobs.get(second)["state"] == "queued"


def test_running_pause_commits_one_checkpoint_then_resume(syncenv, timer, monkeypatch):
    enable(timer)
    ref = timer[0].admit_due()[0]
    original = IngestionWorkflow.import_item

    def pause_after_first(self, observed, **kwargs):
        result = original(self, observed, **kwargs)
        timer[0].configure(timer[1]["config_id"], enabled=False)
        return result

    monkeypatch.setattr(IngestionWorkflow, "import_item", pause_after_first)
    result = syncenv[1].run(ref)
    assert result["state"] == "queued"
    assert len(result["stages"]["source_sync"]["result"]["imported"]) == 1
    assert len(syncenv[0].snapshot()["items"]) == 1
    assert syncenv[0].snapshot()["scopes"][timer[1]["scope_id"]]["status"] == "paused"
    assert syncenv[3] == ["opened", "closed"]
    monkeypatch.setattr(IngestionWorkflow, "import_item", original)
    enable(timer)
    assert syncenv[1].run(ref)["state"] == "succeeded"
    assert len(syncenv[0].snapshot()["items"]) == 2


def test_scope_revision_does_not_hot_swap_active_job(syncenv, timer):
    enable(timer)
    ref = timer[0].admit_due()[0]
    revised = syncenv[1].configure("saved", account_ref=ACCOUNT.public()["account_ref"], limit=1)
    with pytest.raises(ContextError) as error:
        timer[0].configure(revised["config_id"], enabled=True, allow_source_sync=True)
    assert error.value.code == "source_sync_busy"
    assert syncenv[1].plan(syncenv[1].jobs.get(ref)["payload"]["sync"]["config_id"])["limit"] == 5


def test_manual_scope_pending_prevents_timer_admission_and_idle_write(syncenv, timer):
    manual = syncenv[1].submit(timer[1]["config_id"], idempotency_key="manual-first")
    enable(timer)
    before = syncenv[0].snapshot()
    assert timer[0].admit_due() == [] and syncenv[0].snapshot() == before
    syncenv[1].run(manual["id"])
    assert len(timer[0].admit_due()) == 1


def test_worker_source_and_model_authority_stay_separate(syncenv, timer):
    enable(timer)
    worker = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1])
    assert worker.serve(allow_model_calls=True, once=True)["handled"] == 0
    assert not syncenv[0].snapshot()["jobs"] and not syncenv[3]
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 1
    assert len(syncenv[0].snapshot()["items"]) == 2
    assert all(j["kind"] == "sync" and not j["calls"] for j in syncenv[0].snapshot()["jobs"].values())


def test_timer_failure_stops_scope_but_not_other_sources(syncenv, timer):
    enable(timer)
    liked = syncenv[1].configure("liked", account_ref=ACCOUNT.public()["account_ref"])
    timer[0].configure(liked["config_id"], enabled=True, allow_source_sync=True)

    def fetch(kind, **kwargs):
        if kind == "saved":
            raise ContextError("source_login_required", "需重新登录。")
        from test_context_synchronization import batch

        return batch()

    syncenv[2].fetch_self = fetch
    worker = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1])
    assert (
        worker.serve(allow_model_calls=False, allow_source_sync=True, once=True, max_jobs=20)["handled"] == 2
    )
    advance(timer, 120)
    due = timer[0].admit_due()
    assert len(due) == 1
    assert syncenv[1].jobs.get(due[0])["payload"]["sync"]["config_id"] == liked["config_id"]


def test_corrupt_timer_stops_before_source_access(syncenv, timer):
    enable(timer)
    identity = timer[0].status()["scopes"][0]["schedule_id"]
    syncenv[0].transact(lambda state: state["sync_schedule_policies"][identity].update(interval_minutes=1))
    with pytest.raises(ContextError) as error:
        timer[0].admit_due()
    assert error.value.code == "invalid_sync_schedule" and not syncenv[3]


def test_forged_timer_dispatch_cannot_start(syncenv, timer):
    enable(timer)
    identity = timer[0].status()["scopes"][0]["schedule_id"]
    fake = syncenv[1].jobs.submit(
        "sync",
        {
            "sync": {"config_id": timer[1]["config_id"]},
            "dispatch": {"mode": "scheduled_sync", "schedule_id": identity},
        },
        idempotency_key="forged-registration",
    )
    with pytest.raises(ContextError) as error:
        syncenv[1].run(fake["id"])
    assert error.value.code == "invalid_dispatch_policy" and not syncenv[3]


def test_cli_configure_timer_is_separate_confirmed_local_action(syncenv, timer, capsys):
    prefix = ["--workspace", str(syncenv[0].files.root)]
    command = [*prefix, "configure-auto-sync", "--config-id", timer[1]["config_id"], "--enabled", "yes"]
    assert main(command) == 1
    capsys.readouterr()
    assert main([*command, "--allow-source-sync"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["enabled_count"] == 1
    before = syncenv[0].snapshot()
    assert main([*prefix, "sync-settings"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["scopes"][0]["interval_minutes"] == 60
    assert syncenv[0].snapshot() == before and not syncenv[3]
