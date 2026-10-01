"""Account proof/creator pagination with synthetic responses, not real-login acceptance."""

import json
from types import SimpleNamespace

import pytest
from test_context_sources import raw_item

from collection_context.application.contracts import ContextError
from collection_context.library.store import LibraryStore
from collection_context.sources.account import self_account
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.pages import CreatorBatch, creator_page
from collection_context.workflows.ingestion import IngestionWorkflow


def account_payload(**changes):
    return {
        "status_code": 0,
        "user": {"uid": "123", "sec_uid": "MS4w-synthetic", "nickname": "原创测试账号"},
        **changes,
    }


def test_account_requires_platform_self_identity_not_cookie_or_button_state():
    account = self_account(account_payload())
    assert account.public()["state"] == "authenticated" and account.public()["display_name"] == "原创测试账号"
    assert account.public()["account_ref"].startswith("s_")
    assert "sec_uid" not in json.dumps(account.public()) and "MS4w-synthetic" not in repr(account)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status_code": True},
        {"status_code": 8, "status_msg": "private-key"},
        account_payload(user=None),
        account_payload(user={"uid": "0", "sec_uid": "anon", "nickname": "访客"}),
        account_payload(user={"uid": "123", "sec_uid": "", "nickname": "name"}),
    ],
)
def test_account_failures_never_assert_authenticated(payload):
    with pytest.raises(ContextError) as caught:
        self_account(payload)
    assert "private-key" not in str(caught.value)


def payload(ids=("81",), cursor=10, more=1, **changes):
    return {
        "status_code": 0,
        "aweme_list": [raw_item(aweme_id=identity) for identity in ids],
        "max_cursor": cursor,
        "has_more": more,
        **changes,
    }


def test_creator_page_author_identity_and_bounded_pagination():
    page = creator_page(payload(), "MS4w-synthetic")
    assert page.cursor == "10" and page.has_more
    assert page.items[0].author_id == "MS4w-synthetic"
    empty = creator_page(payload(ids=(), cursor=None, more=0), "MS4w-synthetic")
    assert not empty.items and not empty.has_more and empty.cursor is None


@pytest.mark.parametrize(
    "changes",
    [
        {"aweme_list": None},
        {"has_more": None},
        {"has_more": 2},
        {"max_cursor": "../secret"},
        {"max_cursor": None},
        {"status_code": 7},
        {"status_code": False},
    ],
)
def test_invalid_page_never_means_zero_success(changes):
    with pytest.raises(ContextError):
        creator_page(payload(**changes), "MS4w-synthetic")


def test_creator_page_rejects_other_author_and_allows_no_partial_page_commit():
    with pytest.raises(ContextError) as caught:
        creator_page(
            payload(
                aweme_list=[
                    raw_item(),
                    raw_item(aweme_id="82", author={"sec_uid": "another", "nickname": "其他"}),
                ]
            ),
            "MS4w-synthetic",
        )
    assert caught.value.code == "source_scope_mismatch"


def test_batch_deduplicates_page_overlap_preserves_limit_and_unknown_full_history():
    batch = CreatorBatch(3)
    batch.accept(creator_page(payload(ids=("81", "82"), cursor=10), "MS4w-synthetic"), "0")
    batch.accept(creator_page(payload(ids=("82", "83", "84"), cursor=20), "MS4w-synthetic"), "10")
    assert list(batch.items) == ["81", "82", "83"] and batch.done
    assert batch.coverage()["limit_reached"] and not batch.coverage()["complete"]
    assert not batch.coverage()["history_exhausted"]


@pytest.mark.parametrize("start", ["1", "../private"])
def test_batch_rejects_not_initial_cursor(start):
    with pytest.raises(ContextError) as caught:
        CreatorBatch(5).accept(creator_page(payload(), "MS4w-synthetic"), start)
    assert caught.value.code == "source_cursor_mismatch"


def test_batch_rejects_stalled_or_noncontiguous_pages():
    batch = CreatorBatch(5)
    batch.accept(creator_page(payload(cursor=10), "MS4w-synthetic"), "0")
    with pytest.raises(ContextError) as caught:
        batch.accept(creator_page(payload(cursor=20), "MS4w-synthetic"), "5")
    assert caught.value.code == "source_cursor_mismatch"
    with pytest.raises(ContextError) as caught:
        batch.accept(creator_page(payload(cursor=10), "MS4w-synthetic"), "10")
    assert caught.value.code == "source_cursor_stalled"


def test_sync_workflow_writes_creator_relation_and_honest_scope_without_model(tmp_path):
    store = LibraryStore.initialize(tmp_path / "new")
    batch = CreatorBatch(2)
    batch.accept(creator_page(payload(ids=("81", "82"), more=0), "MS4w-synthetic"), "0")
    try:
        source = SimpleNamespace(fetch_creator=lambda *a, **k: batch)
        result = IngestionWorkflow(store, source).sync_creator(
            "https://www.douyin.com/user/MS4w-synthetic", limit=2
        )
        assert result["status"] == "ready" and result["coverage"]["history_exhausted"]
        assert result["model_requests"] == 0 and len(result["items"]) == 2
        scope = store.snapshot()["scopes"][result["scope_id"]]
        assert scope["observed_count"] == 2 and not scope["complete"]
        assert all(
            next(iter(item["relations"].values()))["kind"] == "creator"
            for item in store.snapshot()["items"].values()
        )
    finally:
        store.close()


def test_sync_failure_reports_blocked_not_empty_success(tmp_path):
    store = LibraryStore.initialize(tmp_path / "new")

    def fetch(*a, **k):
        raise ContextError("source_access_required", "需要登录")

    try:
        with pytest.raises(ContextError):
            IngestionWorkflow(store, SimpleNamespace(fetch_creator=fetch)).sync_creator(
                "https://www.douyin.com/user/MS4w-synthetic"
            )
        scope = next(iter(store.snapshot()["scopes"].values()))
        assert scope["status"] == "blocked" and scope["observed_count"] == 0 and not scope["complete"]
        assert not store.snapshot()["items"]
    finally:
        store.close()


def test_invalid_sync_limit_does_not_write_scope_or_fetch(tmp_path):
    store = LibraryStore.initialize(tmp_path / "new")
    before = store.snapshot()
    try:
        with pytest.raises(ContextError):
            IngestionWorkflow(store, None).sync_creator(
                "https://www.douyin.com/user/MS4w-synthetic", limit=True
            )
        assert store.snapshot() == before
    finally:
        store.close()


class AccountPage:
    def __init__(self, responses):
        self.responses = responses
        self.closed = False

    def on(self, event, callback):
        self.callback = callback

    def route(self, *a):
        pass

    def goto(self, *a, **k):
        for path, value in self.responses:
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com" + path,
                    status=200,
                    headers={},
                    body=lambda value=value: json.dumps(value).encode(),
                )
            )

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


def test_account_browser_uses_only_self_response_not_other_profile():
    page = AccountPage(
        [
            (
                "/aweme/v1/web/user/profile/other/",
                account_payload(user={"uid": "555", "sec_uid": "other", "nickname": "别人的账号"}),
            ),
            ("/aweme/v1/web/user/profile/self/", account_payload()),
        ]
    )
    browser = SimpleNamespace(headless=True, context=SimpleNamespace(new_page=lambda: page))
    assert DouyinBrowserSource(browser).account().uid == "123" and page.closed


def test_interactive_login_rejects_headless_instead_of_importing_account():
    with pytest.raises(ContextError) as caught:
        DouyinBrowserSource(SimpleNamespace(headless=True, context=object())).account(interactive=True)
    assert caught.value.code == "desktop_login_required"


def test_covered_login_button_keeps_normal_window_available_until_self_proof():
    class LoginPage(AccountPage):
        def get_by_role(self, *a, **k):
            def click(**kwargs):
                raise RuntimeError("synthetic covered header button")

            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(click=click))

        def is_closed(self):
            return False

        def wait_for_timeout(self, _):
            self.callback(
                SimpleNamespace(
                    url="https://www.douyin.com/aweme/v1/web/user/profile/self/",
                    status=200,
                    headers={},
                    body=lambda: json.dumps(account_payload()).encode(),
                )
            )

    page = LoginPage([])
    browser = SimpleNamespace(headless=False, context=SimpleNamespace(new_page=lambda: page))
    assert DouyinBrowserSource(browser).account(interactive=True).uid == "123" and page.closed


def test_closed_login_window_is_cancelled_not_authenticated():
    class LoginPage(AccountPage):
        def get_by_role(self, *a, **k):
            def click(**kwargs):
                self.closed = True
                raise RuntimeError("synthetic closed window")

            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(click=click))

        def is_closed(self):
            return True

    page = LoginPage([])
    browser = SimpleNamespace(headless=False, context=SimpleNamespace(new_page=lambda: page))
    with pytest.raises(ContextError) as caught:
        DouyinBrowserSource(browser).account(interactive=True)
    assert caught.value.code == "source_login_cancelled" and page.closed


@pytest.mark.parametrize(
    "request_author,body,error_code",
    [
        ("MS4w-synthetic", payload(ids=("81", "82")), None),
        ("other", payload(), "source_scope_mismatch"),
        ("MS4w-synthetic", {"status_code": 8, "status_msg": "private-key"}, "source_access_required"),
    ],
)
def test_creator_browser_response_uses_proven_requested_author_and_closes_page(
    request_author, body, error_code
):
    class CreatorPage(AccountPage):
        url = "https://www.douyin.com/user/MS4w-synthetic"

        def goto(self, *a, **k):
            self.callback(
                SimpleNamespace(
                    url=f"https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id={request_author}&max_cursor=0",
                    status=200,
                    headers={},
                    body=lambda: json.dumps(body).encode(),
                )
            )

    page = CreatorPage([])
    browser = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
    if error_code:
        with pytest.raises(ContextError) as caught:
            DouyinBrowserSource(browser).fetch_creator(page.url, limit=1)
        assert caught.value.code == error_code and "private-key" not in str(caught.value)
    else:
        batch = DouyinBrowserSource(browser).fetch_creator(page.url, limit=1)
        assert list(batch.items) == ["81"] and batch.done and not batch.coverage()["complete"]
    assert page.closed
