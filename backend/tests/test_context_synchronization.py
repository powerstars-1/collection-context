"""Original fixed source jobs; synthetic observation, recovery and separate fee authority."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_context_source_pages import AccountPage, account_payload
from test_context_sources import raw_item

from collection_context.application.contracts import ContextError
from collection_context.cli import main, no_model_authority
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.sources.account import self_account
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.pages import CreatorBatch, work_page
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker

ACCOUNT = self_account(account_payload())


def batch(ids=("81", "82"), limit=5, more=False):
    result = CreatorBatch(limit)
    result.accept(
        work_page(
            {
                "status_code": 0,
                "aweme_list": [raw_item(aweme_id=i) for i in ids],
                "cursor": 10,
                "has_more": int(more),
            },
            cursor_field="cursor",
        ),
        "0",
    )
    return result


@pytest.fixture
def syncenv(tmp_path):
    store = LibraryStore.initialize(tmp_path / "原创同步库")
    seen = []
    source = SimpleNamespace(fetch_creator=lambda *a, **kw: batch(), fetch_self=lambda *a, **kw: batch())

    @contextmanager
    def factory():
        seen.append("opened")
        try:
            yield source
        finally:
            seen.append("closed")

    workflow = SynchronizationWorkflow(store, factory)
    try:
        yield store, workflow, source, seen
    finally:
        store.close()


def queued(syncenv, kind="saved", **kwargs):
    plan = syncenv[1].configure(
        kind, account_ref=ACCOUNT.public()["account_ref"] if kind != "creator" else None, **kwargs
    )
    return syncenv[1].submit(plan["config_id"], idempotency_key="fixed-" + kind), plan


@pytest.mark.parametrize("kind", ["liked", "saved", "collection", "creator"])
def test_each_scope_uses_same_queue_and_preserves_unknown_action_time(syncenv, kind):
    kwargs = (
        {"collection_id": "9"}
        if kind == "collection"
        else {"creator_url": "https://www.douyin.com/user/MS4w-synthetic"}
        if kind == "creator"
        else {}
    )
    job, plan = queued(syncenv, kind, **kwargs)
    result = syncenv[1].run(job["id"])
    assert result["state"] == "succeeded" and not result["calls"]
    assert syncenv[3] == ["opened", "closed"]
    assert len(syncenv[0].snapshot()["items"]) == 2
    for item in syncenv[0].snapshot()["items"].values():
        relation = next(iter(item["relations"].values()))
        assert relation["kind"] == kind and relation["action_at"] is None
    scope = syncenv[0].snapshot()["scopes"][plan["scope_id"]]
    assert scope["committed_count"] == 2 and scope["complete"] is False


def test_registered_scope_revision_and_idempotency_are_fixed_no_browser_at_submit(syncenv):
    job, old = queued(syncenv)
    new = syncenv[1].configure("saved", account_ref=ACCOUNT.public()["account_ref"], limit=1)
    assert new["scope_id"] == old["scope_id"] and new["config_id"] != old["config_id"]
    assert syncenv[1].submit(old["config_id"], idempotency_key="fixed-saved")["id"] == job["id"]
    assert syncenv[1].plan(job["payload"]["sync"]["config_id"])["limit"] == 5 and not syncenv[3]


@pytest.mark.parametrize(
    "kind,options",
    [
        ("liked", {}),
        ("saved", {"account_ref": "s_account", "creator_url": "https://www.douyin.com/user/other"}),
        ("collection", {"account_ref": "s_account", "collection_id": "name"}),
        ("creator", {"creator_url": "https://www.douyin.com/video/81"}),
        ("creator", {"creator_url": "https://www.douyin.com/user/author", "account_ref": "s_account"}),
        ("saved", {"account_ref": "s_account", "limit": True}),
        ("saved", {"account_ref": "s_account", "limit": 21}),
    ],
)
def test_invalid_scope_never_mutates_or_starts_browser(syncenv, kind, options):
    before = syncenv[0].snapshot()
    with pytest.raises(ContextError):
        syncenv[1].configure(kind, **options)
    assert syncenv[0].snapshot() == before and not syncenv[3]


def test_tampered_registered_plan_and_nonzero_fee_budget_are_refused(syncenv):
    job, plan = queued(syncenv)
    syncenv[0].transact(lambda state: state["sync_scope_configs"][plan["config_id"]].update(limit=20))
    with pytest.raises(ContextError):
        syncenv[1].run(job["id"])
    assert not syncenv[3] and syncenv[1].jobs.get(job["id"])["state"] == "queued"


def test_platform_failure_is_blocked_not_successful_empty_and_is_not_retried(syncenv):
    job, plan = queued(syncenv)

    def rejected(*a, **kw):
        raise ContextError("source_login_required", "需要登录")

    syncenv[2].fetch_self = rejected
    worker = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1])
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 1
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 0
    assert syncenv[1].jobs.get(job["id"])["state"] == "blocked"
    assert syncenv[0].snapshot()["scopes"][plan["scope_id"]]["status"] == "blocked"
    assert not syncenv[0].snapshot()["items"] and syncenv[3] == ["opened", "closed"]


def test_source_only_worker_does_not_run_or_admit_model_tasks(syncenv):
    source_job, _ = queued(syncenv)
    model_job = syncenv[1].jobs.submit(
        "process", {"extraction": {"untrusted": "placeholder"}}, idempotency_key="model", max_calls=2
    )
    result = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1]).serve(
        allow_model_calls=False, allow_source_sync=True, once=True
    )
    assert result["handled"] == 1
    assert syncenv[1].jobs.get(source_job["id"])["state"] == "succeeded"
    assert syncenv[1].jobs.get(model_job["id"])["state"] == "queued"


def test_missing_source_setup_does_not_mutate_queue(syncenv):
    job, _ = queued(syncenv)
    with pytest.raises(ContextError) as caught:
        BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority)).serve(
            allow_model_calls=False, allow_source_sync=True, once=True
        )
    assert (
        caught.value.code == "source_setup_required" and syncenv[1].jobs.get(job["id"])["state"] == "queued"
    )


def test_restart_retains_per_work_checkpoint_and_never_reimports_confirmed_work(syncenv, monkeypatch):
    job, _ = queued(syncenv)
    real = IngestionWorkflow.import_item
    visits = []

    def interrupted(self, item, **kwargs):
        visits.append(item.source["native_id"])
        if item.source["native_id"] == "82":
            raise KeyboardInterrupt
        return real(self, item, **kwargs)

    monkeypatch.setattr(IngestionWorkflow, "import_item", interrupted)
    with pytest.raises(KeyboardInterrupt):
        syncenv[1].run(job["id"])
    assert syncenv[1].jobs.get(job["id"])["state"] == "running" and len(syncenv[0].snapshot()["items"]) == 1
    monkeypatch.setattr(IngestionWorkflow, "import_item", real)
    worker = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1])
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 1
    recovered = syncenv[1].jobs.get(job["id"])
    assert (
        recovered["state"] == "succeeded"
        and recovered["stages"]["source_sync"]["result"]["coverage"]["reobserved_after_restart"]
    )
    assert len(syncenv[0].snapshot()["items"]) == 2 and visits == ["81", "82"]


def test_cancel_during_observation_prevents_import_and_closes_source(syncenv):
    job, _ = queued(syncenv)

    def observed(*a, **kw):
        syncenv[1].jobs.cancel(job["id"])
        return batch()

    syncenv[2].fetch_self = observed
    assert syncenv[1].run(job["id"])["state"] == "cancelled"
    assert not syncenv[0].snapshot()["items"] and syncenv[3] == ["opened", "closed"]


def test_true_empty_response_and_partial_page_are_distinct(syncenv):
    job, plan = queued(syncenv)
    syncenv[2].fetch_self = lambda *a, **kw: batch(())
    assert syncenv[1].run(job["id"])["state"] == "succeeded"
    assert syncenv[0].snapshot()["scopes"][plan["scope_id"]]["history_exhausted"] is True
    another = syncenv[1].submit(plan["config_id"], idempotency_key="partial-page")
    syncenv[2].fetch_self = lambda *a, **kw: batch(("81",), more=True)
    assert syncenv[1].run(another["id"])["state"] == "partial"


def test_cli_scope_registration_and_queue_no_platform_or_model(syncenv, capsys):
    root = str(syncenv[0].files.root)
    assert (
        main(
            [
                "--workspace",
                root,
                "configure-sync",
                "--kind",
                "saved",
                "--account-ref",
                ACCOUNT.public()["account_ref"],
            ]
        )
        == 0
    )
    config = json.loads(capsys.readouterr().out)["data"]["config_id"]
    assert (
        main(["--workspace", root, "submit-sync", "--config-id", config, "--idempotency-key", "cli-same"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "queued" and not syncenv[3]
    assert main(["--workspace", root, "worker", "--once", "--allow-source-sync"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "source_setup_required"


@pytest.mark.parametrize(
    "kind,path,cursor",
    [
        ("liked", "/aweme/v1/web/aweme/favorite/?sec_user_id=MS4w-synthetic&max_cursor=0", "max_cursor"),
        ("saved", "/aweme/v1/web/aweme/listcollection/?cursor=0", "cursor"),
        ("collection", "/aweme/v1/web/collects/video/list/?collects_id=9&cursor=0", "cursor"),
    ],
)
def test_self_browser_request_is_account_and_scope_bound_and_page_closed(monkeypatch, kind, path, cursor):
    payload = {"status_code": 0, "aweme_list": [raw_item()], "has_more": 0, cursor: 10}

    class SelfPage(AccountPage):
        url = "https://www.douyin.com/user/self"

        def goto(self, *args, **kwargs):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com" + path,
                    status=200,
                    headers={},
                    body=lambda: json.dumps(payload).encode(),
                )
            )

    page = SelfPage([])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    monkeypatch.setattr(source, "account", lambda: ACCOUNT)
    observed = source.fetch_self(
        kind,
        expected_account_ref=ACCOUNT.public()["account_ref"],
        collection_id="9" if kind == "collection" else None,
        limit=1,
    )
    assert list(observed.items) == ["81"] and page.closed


def test_account_change_before_list_refused_without_opening_private_page(monkeypatch):
    source = DouyinBrowserSource(SimpleNamespace(context=None))
    monkeypatch.setattr(source, "account", lambda: ACCOUNT)
    with pytest.raises(ContextError) as caught:
        source.fetch_self("saved", expected_account_ref="s_other")
    assert caught.value.code == "source_account_changed"


@pytest.mark.parametrize(
    "kind,path,error",
    [
        ("liked", "/aweme/v1/web/aweme/favorite/?sec_user_id=other&max_cursor=0", "source_scope_mismatch"),
        ("saved", "/aweme/v1/web/aweme/listcollection/?collects_id=9&cursor=0", "source_scope_mismatch"),
        ("collection", "/aweme/v1/web/collects/video/list/?collects_id=8&cursor=0", "source_scope_mismatch"),
        (
            "collection",
            "/aweme/v1/web/collects/video/list/?collects_id=9&collects_id=8&cursor=0",
            "source_scope_mismatch",
        ),
        ("saved", "/aweme/v1/web/aweme/listcollection/?cursor=0&cursor=10", "source_cursor_invalid"),
    ],
)
def test_self_wrong_or_ambiguous_request_scope_never_becomes_empty_success(monkeypatch, kind, path, error):
    class SelfPage(AccountPage):
        url = "https://www.douyin.com/user/self"

        def goto(self, *args, **kwargs):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com" + path, status=200, headers={}, body=lambda: b"{}"
                )
            )

    page = SelfPage([])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    monkeypatch.setattr(source, "account", lambda: ACCOUNT)
    with pytest.raises(ContextError) as caught:
        source.fetch_self(
            kind,
            expected_account_ref=ACCOUNT.public()["account_ref"],
            collection_id="9" if kind == "collection" else None,
        )
    assert caught.value.code == error and page.closed


def test_changed_account_after_observation_discards_private_batch(monkeypatch):
    class SelfPage(AccountPage):
        url = "https://www.douyin.com/user/self"

        def goto(self, *args, **kwargs):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0",
                    status=200,
                    headers={},
                    body=lambda: json.dumps(
                        {"status_code": 0, "aweme_list": [raw_item()], "cursor": 10, "has_more": 0}
                    ).encode(),
                )
            )

    page = SelfPage([])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    accounts = iter(
        [
            ACCOUNT,
            self_account(account_payload(user={"uid": "456", "sec_uid": "other", "nickname": "其他账号"})),
        ]
    )
    monkeypatch.setattr(source, "account", lambda: next(accounts))
    with pytest.raises(ContextError) as caught:
        source.fetch_self("saved", expected_account_ref=ACCOUNT.public()["account_ref"], limit=1)
    assert caught.value.code == "source_account_changed" and page.closed


def test_source_cannot_expand_fixed_job_quantity(syncenv):
    config = syncenv[1].configure("saved", account_ref=ACCOUNT.public()["account_ref"], limit=1)
    job = syncenv[1].submit(config["config_id"], idempotency_key="quantity")
    assert syncenv[1].run(job["id"])["error"]["code"] == "source_limit"
    assert not syncenv[0].snapshot()["items"]


def test_account_page_route_preserves_outer_context_handler():
    routes = []

    class ChainedPage(AccountPage):
        def route(self, pattern, callback):
            self.handler = callback

        def goto(self, *args, **kwargs):
            for kind in ("document", "media"):
                self.handler(
                    SimpleNamespace(
                        request=SimpleNamespace(resource_type=kind),
                        fallback=lambda: routes.append("fallback"),
                        abort=lambda: routes.append("abort"),
                    )
                )
            return super().goto(*args, **kwargs)

    page = ChainedPage([("/aweme/v1/web/user/profile/self/", account_payload())])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    assert source.account().uid == "123" and routes == ["fallback", "abort"]


def test_checkpoint_content_change_after_restart_is_blocked_not_silently_skipped(syncenv, monkeypatch):
    job, _ = queued(syncenv)
    real = IngestionWorkflow.import_item

    def crash(self, item, **kw):
        if item.source["native_id"] == "82":
            raise KeyboardInterrupt
        return real(self, item, **kw)

    monkeypatch.setattr(IngestionWorkflow, "import_item", crash)
    with pytest.raises(KeyboardInterrupt):
        syncenv[1].run(job["id"])
    monkeypatch.setattr(IngestionWorkflow, "import_item", real)
    changed = batch()
    changed.items["81"].source["body"] = "恢复时内容已经改变"
    syncenv[2].fetch_self = lambda *a, **kw: changed
    result = BackgroundWorker(ExtractionWorkflow(syncenv[0], no_model_authority), syncenv[1]).serve(
        allow_model_calls=False, allow_source_sync=True, once=True
    )
    assert result["handled"] == 1
    assert syncenv[1].jobs.get(job["id"])["error"]["code"] == "source_snapshot_changed"
    assert len(syncenv[0].snapshot()["items"]) == 1


def test_invalid_checkpoint_is_blocked_before_browser_or_reimport(syncenv):
    job, config = queued(syncenv)
    syncenv[1].jobs.start(job["id"])
    syncenv[1].jobs.set_stage(
        job["id"],
        "source_sync",
        state_name="running",
        input_hash=config["config_id"],
        result={"imported": {"81": {"source_hash": "bad"}}, "coverage": None},
    )
    with ExecutorLease(syncenv[0].files.root) as lease:
        syncenv[1].jobs.recover_interrupted(lease=lease)
    assert syncenv[1].run(job["id"])["error"]["code"] == "invalid_sync_checkpoint"
    assert not syncenv[3] and not syncenv[0].snapshot()["items"]
