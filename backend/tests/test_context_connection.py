"""Independent account/folder proof and owner selection; synthetic, not real platform compatibility."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_scheduling import env as env
from test_context_source_pages import AccountPage, account_payload

from collection_context.application.contracts import ContextError, digest
from collection_context.application.source_management import SourceManagement
from collection_context.cli import main
from collection_context.sources.account import self_account
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.collections import FolderBatch, folder_page
from collection_context.workflows.connection import ConnectionCatalog, ConnectionWorkflow

ACCOUNT = self_account(account_payload())


def folders(ids=("11", "12"), **changes):
    return {
        "status_code": 0,
        "collects_list": [{"collects_id_str": i, "collects_name": "同名教程"} for i in ids],
        "cursor": 10,
        "has_more": 0,
        **changes,
    }


def batch(**kwargs):
    result = FolderBatch()
    result.accept(folder_page(folders(**kwargs)), "0")
    return result


def record(env, **kwargs):
    catalog = ConnectionCatalog(env[0])
    return catalog.record(ACCOUNT, expected_version=catalog.status()["version"], **kwargs)


def args(version, **changes):
    return {
        "kind": "collection",
        "collection_id": "11",
        "connection_version": version,
        "limit": 5,
        "download": False,
        "source_confirmed": True,
        **changes,
    }


def test_no_proof_and_status_query_does_not_write_or_import_accounts(env, capsys):
    before = env[0].snapshot()
    assert ConnectionCatalog(env[0]).status()["state"] == "not_connected"
    assert main(["--workspace", str(env[0].files.root), "connection-snapshot"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["model_requests"] == 0
    assert env[0].snapshot() == before
    with pytest.raises(ContextError):
        SourceManagement(env[0]).create_self(**args("n_missing"))


@pytest.mark.parametrize("kind", ["liked", "saved", "collection"])
def test_owner_selects_only_verified_account_and_observed_folder_without_dispatch(env, kind):
    proof = record(env, folders=batch())
    result = SourceManagement(env[0]).create_self(
        **args(proof["version"], kind=kind, collection_id="11" if kind == "collection" else None)
    )
    assert result["kind"] == kind and result["model_requests"] == 0
    assert "account_ref" not in json.dumps(result)
    assert not env[0].snapshot()["jobs"] and not env[0].snapshot()["items"]
    public = ConnectionCatalog(env[0]).status()
    assert len(public["folders"]) == 2 and public["complete"]
    assert "sec_uid" not in json.dumps(env[0].snapshot())
    assert "account_ref" not in json.dumps(public) and '"123"' not in json.dumps(public)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"collection_id": "99"}, "source_folder_unverified"),
        ({"source_confirmed": False}, "source_authorization_required"),
        ({"source_confirmed": 1}, "source_authorization_required"),
        ({"kind": "creator"}, "invalid_source_scope"),
        ({"limit": 21}, "invalid_source_scope"),
        ({"download": "yes"}, "invalid_source_scope"),
        ({"kind": "liked"}, "invalid_source_scope"),
        ({"connection_version": "n_other"}, "source_connection_changed"),
    ],
)
def test_invalid_self_scope_refused_without_writes(env, changes, code):
    proof = record(env, folders=batch())
    before = env[0].snapshot()
    with pytest.raises(ContextError) as caught:
        SourceManagement(env[0]).create_self(**args(proof["version"], **changes))
    assert caught.value.code == code and env[0].snapshot() == before


def test_login_only_cannot_invent_folder_and_repeat_different_scope_not_overwritten(env):
    proof = record(env)
    with pytest.raises(ContextError) as caught:
        SourceManagement(env[0]).create_self(**args(proof["version"]))
    assert caught.value.code == "source_folder_unverified"
    options = args(proof["version"], kind="saved", collection_id=None)
    first = SourceManagement(env[0]).create_self(**options)
    assert SourceManagement(env[0]).create_self(**options) == first
    with pytest.raises(ContextError) as caught:
        SourceManagement(env[0]).create_self(**{**options, "limit": 10})
    assert caught.value.code == "source_scope_exists"


def test_connection_does_not_expire_with_time_but_actual_login_failure_clears_it(env):
    proof = record(env, folders=batch())
    state = env[0].snapshot()
    stamp = datetime.fromisoformat(proof["observed_at"])
    for now in [stamp + timedelta(seconds=901), stamp + timedelta(days=30)]:
        status = ConnectionCatalog.status_from_state(state, now=now.isoformat())
        assert status["state"] == "verified" and len(status["folders"]) == 2 and status["complete"]
        assert status["expires_at"] is None
    assert ConnectionCatalog.status_from_state(state, now=(stamp - timedelta(seconds=1)).isoformat())["state"] == "stale"

    def rejected(**kwargs):
        raise ContextError("source_login_required", "合成未登录")

    with pytest.raises(ContextError):
        ConnectionWorkflow(
            env[0], SimpleNamespace(account=rejected, connection_observation=nullcontext)
        ).observe()
    assert ConnectionCatalog(env[0]).status()["state"] == "unverified"
    with pytest.raises(ContextError):
        SourceManagement(env[0]).create_self(**args(proof["version"]))


def test_new_observation_during_source_registration_cas_prevents_stale_scope(env, monkeypatch):
    proof = record(env, folders=batch())
    manager = SourceManagement(env[0])
    original = manager.workflow.configure

    def changed(*a, **kw):
        record(env)
        return original(*a, **kw)

    monkeypatch.setattr(manager.workflow, "configure", changed)
    with pytest.raises(ContextError) as caught:
        manager.create_self(**args(proof["version"]))
    assert caught.value.code == "source_connection_changed" and not env[0].snapshot().get(
        "sync_current_scopes"
    )


def test_concurrent_observation_does_not_overwrite_new_account(env):
    catalog = ConnectionCatalog(env[0])
    proof = record(env)
    before = env[0].snapshot()
    with pytest.raises(ContextError) as caught:
        catalog.record(ACCOUNT, expected_version=None)
    assert caught.value.code == "source_connection_changed" and env[0].snapshot() == before
    with pytest.raises(ContextError):
        catalog.record(ACCOUNT, expected_version=proof["version"], folders=FolderBatch())


@pytest.mark.parametrize("code", ["source_site_unavailable", "source_access_required", "source_shape_changed"])
def test_folder_failure_preserves_actual_account_and_liked_saved_selection(env, code):
    def discover(**_):
        # Account recognition is visible before optional folder loading finishes.
        assert ConnectionCatalog(env[0]).status()["state"] == "verified"
        raise ContextError(code, "合成收藏夹失败")

    result = ConnectionWorkflow(env[0], SimpleNamespace(
        account=lambda **_: ACCOUNT, fetch_collections=discover,
        connection_observation=nullcontext,
    )).observe(discover=True)
    assert result["state"] == "verified" and result["error_code"] == code
    assert not result["folders_observed"] and not result["folders"]
    for kind in ("liked", "saved"):
        SourceManagement(env[0]).create_self(**args(result["version"], kind=kind, collection_id=None))
    with pytest.raises(ContextError) as caught:
        SourceManagement(env[0]).create_self(**args(result["version"]))
    assert caught.value.code == "source_folder_unverified"
    assert not env[0].snapshot()["jobs"] and not env[0].snapshot()["items"]


@pytest.mark.parametrize(
    "changes",
    [
        {"account": {"state": "authenticated"}},
        {"folders": [{"collection_id": "11", "name": "ok"}], "folders_observed": False},
        {"folders_observed": "yes"},
        {"complete": True, "folders_observed": False},
        {"observed_at": "tomorrow"},
    ],
)
def test_rehashed_corrupt_connection_still_rejected(env, changes):
    record(env)
    state = env[0].snapshot()
    value = {**state["source_connection"], **changes}
    value["version"] = "n_" + digest({k: v for k, v in value.items() if k != "version"})
    state["source_connection"] = value
    with pytest.raises(ContextError) as caught:
        ConnectionCatalog.status_from_state(state)
    assert caught.value.code == "source_connection_corrupt"


@pytest.mark.parametrize(
    "changes",
    [
        {"status_code": True},
        {"status_code": 9},
        {"collects_list": None},
        {"has_more": 2},
        {"cursor": "../secret"},
        {"collects_list": [{"collects_id_str": 11, "collects_name": "ok"}]},
        {"collects_list": [{"collects_id_str": "11", "collects_name": "\x00"}]},
        {
            "collects_list": [
                {"collects_id_str": "11", "collects_name": "one"},
                {"collects_id_str": "11", "collects_name": "two"},
            ]
        },
    ],
)
def test_invalid_folder_page_not_zero_success(changes):
    with pytest.raises(ContextError):
        folder_page(folders(**changes))


def test_folder_batch_partial_overlap_cursor_and_limit_are_honest():
    result = FolderBatch(2)
    result.accept(folder_page(folders(ids=("11",), has_more=1)), "0")
    result.accept(folder_page(folders(ids=("11", "12", "13"))), "10")
    assert list(result.folders) == ["11", "12"] and result.done and not result.complete
    with pytest.raises(ContextError):
        FolderBatch().accept(folder_page(folders()), "5")
    with pytest.raises(ContextError):
        FolderBatch().accept(folder_page(folders(cursor=0, has_more=1)), "0")


def test_observed_folder_browser_uses_account_before_after_and_closes(env, monkeypatch):
    class Page(AccountPage):
        url = "https://www.douyin.com/user/self?showTab=favorite_collection"

        def goto(self, *a, **kw):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com/aweme/v1/web/collects/list/?cursor=0",
                    status=200,
                    headers={},
                    body=lambda: json.dumps(folders()).encode(),
                )
            )

    page = Page([])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    accounts = iter([ACCOUNT, ACCOUNT])
    monkeypatch.setattr(source, "account", lambda **kw: next(accounts))
    found = source.fetch_collections(expected_account_ref=ACCOUNT.public()["account_ref"])
    assert found.complete and page.closed and len(found.folders) == 2


def test_account_switch_after_discovery_never_commits_folders(env, monkeypatch):
    other = self_account(account_payload(user={"uid": "999", "sec_uid": "Other", "nickname": "另一个账号"}))

    class Page(AccountPage):
        url = "https://www.douyin.com/user/self"

        def goto(self, *a, **kw):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com/aweme/v1/web/collects/list/?cursor=0",
                    status=200,
                    headers={},
                    body=lambda: json.dumps(folders()).encode(),
                )
            )

    page = Page([])
    source = DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))
    accounts = iter([ACCOUNT, ACCOUNT, other])
    monkeypatch.setattr(source, "account", lambda **kw: next(accounts))
    with pytest.raises(ContextError) as caught:
        ConnectionWorkflow(env[0], source).observe(discover=True)
    assert caught.value.code == "source_account_changed" and page.closed
    assert ConnectionCatalog(env[0]).status()["state"] == "unverified"
    assert not ConnectionCatalog(env[0]).status()["folders"]


@pytest.mark.parametrize("ending", ["success", "account_changed", "cancelled", "folder_rejected"])
def test_discovery_reuses_one_page_with_fresh_final_account_and_cleans_up(env, ending):
    other = account_payload(user={"uid": "999", "sec_uid": "Other", "nickname": "另一个账号"})

    class Page(AccountPage):
        def __init__(self):
            super().__init__([])
            self.visits, self.listeners, self.routes = [], [], []
            self.close_count = 0

        def on(self, event, callback):
            self.listeners.append(callback)

        def remove_listener(self, event, callback):
            self.listeners.remove(callback)

        def route(self, pattern, handler):
            self.routes.append(handler)

        def unroute(self, pattern, handler):
            self.routes.remove(handler)

        def goto(self, url, **kwargs):
            assert not self.closed and len(self.listeners) == len(self.routes) == 1
            self.visits.append(url)
            self.url = url
            if "favorite_collection" in url:
                assert kwargs["wait_until"] == "commit"
                assert "showSubTab=favorite_folder" in url
                path = "/aweme/v1/web/collects/list/?cursor=0"
                value = folders(status_code=8) if ending == "folder_rejected" else folders()
            else:
                path = "/aweme/v1/web/user/profile/self/"
                value = other if ending == "account_changed" and len(self.visits) == 3 else account_payload()
            for callback in tuple(self.listeners):
                callback(
                    SimpleNamespace(
                        url="https://www.douyin.com" + path,
                        status=200,
                        headers={},
                        body=lambda: json.dumps(value).encode(),
                    )
                )

        def close(self):
            self.close_count += 1
            super().close()

    page, allocations = Page(), []

    def new_page():
        allocations.append(1)
        return page

    source = DouyinBrowserSource(SimpleNamespace(headless=False, context=SimpleNamespace(new_page=new_page)))
    workflow = ConnectionWorkflow(env[0], source)

    def cancelled():
        return ending == "cancelled" and len(page.visits) >= 2

    if ending == "success":
        result = workflow.observe(discover=True, cancelled=cancelled)
        assert result["state"] == "verified" and len(result["folders"]) == 2
    elif ending == "folder_rejected":
        status = workflow.observe(discover=True, cancelled=cancelled)
        assert status["state"] == "verified"
        assert status["display_name"] == ACCOUNT.display_name
        assert status["error_code"] == "source_access_required"
        assert not status["folders_observed"] and not status["folders"]
    else:
        with pytest.raises(ContextError) as caught:
            workflow.observe(discover=True, cancelled=cancelled)
        assert (
            caught.value.code
            == {
                "account_changed": "source_account_changed",
                "cancelled": "connection_cancelled",
                "folder_rejected": "source_access_required",
            }[ending]
        )
        assert ConnectionCatalog(env[0]).status()["state"] == ("verified" if ending == "cancelled" else "unverified")
        assert not ConnectionCatalog(env[0]).status()["folders"]
    assert allocations == [1] and page.close_count == 1
    assert not page.listeners and not page.routes
    assert source._connection_page is None and source._connection_account is None
    assert page.visits == [
        "https://www.douyin.com/user/self",
        "https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection",
    ] + (["https://www.douyin.com/user/self"] if ending in {"success", "account_changed"} else [])
    assert not env[0].snapshot()["jobs"] and not env[0].snapshot()["items"]


def test_owner_http_and_default_read_permissions_unchanged(managed, env):
    proof = record(env, folders=batch())
    before = env[0].snapshot()
    response = post(managed, "connection", {}).json()
    assert response["ok"] and response["data"]["version"] == proof["version"]
    assert "account_ref" not in json.dumps(response) and env[0].snapshot() == before
    assert post(managed, "self-source-create", args(proof["version"])).json()["ok"]
    assert (
        post(managed, "self-source-create", {**args(proof["version"]), "account_ref": "s_fake"}).status_code
        == 400
    )
    for token in (managed[3]["token"], managed[4]["token"]):
        assert (
            managed[0]
            .post("/v1/management/connection", json={}, headers={"Authorization": "Bearer " + token})
            .status_code
            == 403
        )
