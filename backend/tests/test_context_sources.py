"""Original source contracts, using fictional bounded platform responses; no private login."""

import json
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.infrastructure.browser import BrowserSession
from collection_context.infrastructure.public_http import Download, PublicDownloadError
from collection_context.library.store import LibraryStore
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.douyin import normalize_item
from collection_context.sources.downloads import DouyinDownloads, media_mime
from collection_context.sources.links import parse_link
from collection_context.workflows.ingestion import IngestionWorkflow


def raw_item(**changes):
    return {
        "aweme_id": "81",
        "desc": "合成教程\n保留完整正文 #参数",
        "create_time": 1700000000,
        "author": {"nickname": "原创夹具", "sec_uid": "MS4w-synthetic"},
        "video": {"play_addr": {"url_list": ["https://example.douyinvod.com/a?signature=synthetic-secret"]}},
        **changes,
    }


@pytest.mark.parametrize(
    "value,kind,identity,url",
    [
        ("分享 https://v.douyin.com/Ab9x/ 复制", "short", None, "https://v.douyin.com/Ab9x/"),
        ("https://www.douyin.com/video/81?signature=secret", "item", "81", "https://www.douyin.com/video/81"),
        ("https://www.iesdouyin.com/share/video/81/", "item", "81", "https://www.douyin.com/video/81"),
        ("https://douyin.com/note/82", "item", "82", "https://www.douyin.com/note/82"),
        ("https://www.douyin.com/?modal_id=81", "item", "81", "https://www.douyin.com/video/81"),
        (
            "https://www.douyin.com/user/MS4w-sec?track=secret",
            "creator",
            "MS4w-sec",
            "https://www.douyin.com/user/MS4w-sec",
        ),
    ],
)
def test_links_are_bounded_and_remove_private_queries(value, kind, identity, url):
    assert parse_link(value).kind == kind
    assert parse_link(value).identity == identity
    assert parse_link(value).url == url


@pytest.mark.parametrize(
    "value",
    [
        "http://www.douyin.com/video/81",
        "https://www.douyin.com.evil.invalid/video/81",
        "https://user:secret@www.douyin.com/video/81",
        "https://www.douyin.com:8443/video/81",
        "https://127.0.0.1/video/81",
        "https://www.douyin.com/%2e%2e/video/81",
        "https://v.douyin.com/a/b",
        "https://www.douyin.com/passport",
        "https://www.douyin.com/video/abc",
        "https://www.douyin.com/?modal_id=81&modal_id=82",
        "https://www.douyin.com/video/81\x00secret",
        "https://www.douyin.com/video/81 https://www.douyin.com/video/82",
    ],
)
def test_rejects_non_platform_and_ambiguous_links(value):
    with pytest.raises(ContextError):
        parse_link(value)


def test_detail_keeps_body_and_never_infers_action_time_or_serializes_signed_url():
    item = normalize_item(raw_item(), expected_id="81")
    assert item.source["body"] == "合成教程\n保留完整正文 #参数"
    assert item.source["title"] == "合成教程"
    assert item.source["published_at"] == "2023-11-14T22:13:20+00:00"
    assert "action_at" not in item.source
    assert "signature" not in json.dumps(item.source) and "synthetic-secret" not in repr(item)
    assert item.author_id == "MS4w-synthetic" and len(item.assets) == 1


def test_images_keep_original_page_order_not_covers_or_video_placeholder():
    item = normalize_item(
        raw_item(
            images=[
                {"url_list": ["https://fixture.byteimg.com/1"]},
                {"url_list": ["https://fixture.byteimg.com/2"]},
            ]
        )
    )
    assert item.source["media_type"] == "image"
    assert [asset.page_index for asset in item.assets] == [0, 1]
    assert all(asset.kind == "image" for asset in item.assets)


@pytest.mark.parametrize(
    "changes",
    [
        {"aweme_id": 81},
        {"desc": None},
        {"author": {}},
        {"create_time": "2023"},
        {"status": {"is_delete": True}},
        {"status": {"private_status": 1}},
        {"video": {}},
        {"images": "unexpected"},
        {"images": [{}]},
        {"images": [{"url_list": ["https://fixture.byteimg.com/1"]}] * 241},
        {"video": {"play_addr": {"url_list": ["http://fixture.byteimg.com/a"]}}},
    ],
)
def test_response_failures_are_explicit_not_empty_success(changes):
    with pytest.raises(ContextError):
        normalize_item(raw_item(**changes))


def test_expected_identity_does_not_accept_recommended_item():
    with pytest.raises(ContextError, match="不同作品"):
        normalize_item(raw_item(), expected_id="82")


class Page:
    def __init__(self, payload, url="https://www.douyin.com/video/81", status=200):
        self.payload, self.url, self.status = payload, url, status
        self.closed = False
        self.main_frame = object()

    def route(self, _, callback):
        self.route_callback = callback

    def on(self, _, callback):
        self.callback = callback

    def goto(self, *args, **kwargs):
        self.callback(
            SimpleNamespace(
                url="https://www.douyin.com/aweme/v1/web/aweme/detail/",
                status=self.status,
                headers={},
                body=lambda: json.dumps(self.payload).encode(),
            )
        )

    def wait_for_timeout(self, _):
        raise AssertionError("No fixture should wait for data")

    def close(self):
        self.closed = True


def source(page):
    return DouyinBrowserSource(SimpleNamespace(context=SimpleNamespace(new_page=lambda: page)))


def test_browser_observer_only_uses_requested_detail_and_closes_page():
    page = Page({"status_code": 0, "aweme_detail": raw_item()})
    assert source(page).fetch_item("https://www.douyin.com/video/81").source["native_id"] == "81"
    assert page.closed


def test_browser_observer_rejects_access_failure_without_returning_upstream_text():
    page = Page({"status_code": 8, "status_msg": "private-cookie=fixture-secret"})
    with pytest.raises(ContextError) as caught:
        source(page).fetch_item("https://www.douyin.com/video/81")
    assert caught.value.code == "source_access_required" and "fixture-secret" not in str(caught.value)
    assert page.closed


def test_browser_does_not_navigate_creator_as_single_work():
    with pytest.raises(ContextError) as caught:
        source(Page({})).fetch_item("https://www.douyin.com/user/MS4w-test")
    assert caught.value.code == "source_scope_mismatch"


def test_profile_rejects_foreign_directory_before_starting_runtime(tmp_path):
    profile = tmp_path / "foreign"
    profile.mkdir()
    (profile / "old-account").write_text("do-not-import")
    with pytest.raises(ContextError) as caught:
        with BrowserSession(profile):
            pass
    assert caught.value.code == "foreign_login_profile"
    assert (profile / "old-account").read_text() == "do-not-import"
    assert not (profile / "collection-browser-profile.json").exists()


def test_ingestion_commits_original_and_search_without_model_or_download(tmp_path):
    store = LibraryStore.initialize(tmp_path / "new-library")
    observed = normalize_item(raw_item())
    source = SimpleNamespace(fetch_item=lambda _: observed)
    try:
        workflow = IngestionWorkflow(store, source)
        first = workflow.add_link("https://www.douyin.com/video/81")
        second = workflow.import_item(observed, kind="saved", scope_id="s_saved")
        assert first["created"] and not second["created"] and first["model_requests"] == 0
        assert first["download"] == "not_requested"
        material = store.get(first["material_ref"])
        assert len(material["relations"]) == 2
        assert all(r["action_at"] is None for r in material["relations"].values())
        assert "signature" not in json.dumps(store.snapshot())
        assert "保留完整正文" in ContextService(store).read(material["id"])["text"]
        assert ContextService(store).search("完整正文")["items"]
    finally:
        store.close()


def test_failed_download_keeps_truthful_metadata_and_does_not_create_input_or_paid_job(tmp_path):
    store = LibraryStore.initialize(tmp_path / "new-library")
    observed = normalize_item(raw_item())

    class Downloads:
        def fetch(self, _):
            raise ContextError("download_rejected", "合成403")

    try:
        result = IngestionWorkflow(
            store, SimpleNamespace(fetch_item=lambda _: observed), Downloads()
        ).add_link("fixture", download=True)
        assert result["metadata"] == "ready" and result["download"] == "blocked"
        assert result["error"]["code"] == "download_rejected"
        assert result["model_requests"] == 0 and not store.snapshot()["jobs"]
        assert not store.get(result["material_ref"]).get("prepared_input")
    finally:
        store.close()


def test_download_uses_order_and_rejects_html_not_a_media_header():
    item = normalize_item(
        raw_item(
            images=[
                {"url_list": ["https://fixture.byteimg.com/1"]},
                {"url_list": ["https://fixture.byteimg.com/2"]},
            ]
        )
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Download(b"\x89PNG\r\n\x1a\nsynthetic", "image/png")

    assert len(DouyinDownloads(SimpleNamespace(get=get)).fetch(item)) == 2
    assert calls == ["https://fixture.byteimg.com/1", "https://fixture.byteimg.com/2"]
    with pytest.raises(ContextError) as caught:
        media_mime(b"<html>login</html>", "video")
    assert caught.value.code == "source_media_format"


def test_frame_failure_does_not_discard_download_and_retry_reuses_original(tmp_path, monkeypatch):
    from collection_context.processing.inputs import PreparedInputs
    from collection_context.sources.douyin import source_asset_identity

    store = LibraryStore.initialize(tmp_path / "new-library")
    observed = normalize_item(raw_item())
    media = b"\x00\x00\x00\x14ftypisom" + b"synthetic"
    downloads = []

    def fetch(_):
        downloads.append(True)
        return [(media, "video/mp4")]

    def prepare(*args, **kwargs):
        raise ContextError("frame_limit", "合成选帧上限")

    monkeypatch.setattr(PreparedInputs, "prepare_video", prepare)
    try:
        workflow = IngestionWorkflow(store, SimpleNamespace(fetch_item=lambda _: observed), SimpleNamespace(fetch=fetch))
        first = workflow.add_link("fixture", download=True)
        assert first["download"] == "ready" and first["preparation"] == "blocked"
        assert first["preparation_error"]["code"] == "frame_limit" and first["model_requests"] == 0
        assert not store.get(first["material_ref"]).get("prepared_input")
        assert PreparedInputs(store).source_media(first["material_ref"], source_asset_identity(observed)) == [(media, "video/mp4")]
        from collection_context.application.library_management import LibraryManagement
        management = LibraryManagement(store, authorize=lambda: None)
        assert management.overview()["storage"]["media_bytes"] == len(media)
        exported = management._export_files(store.snapshot(), first["material_ref"], "all")
        assert media in exported.values() and "media-evidence.json" not in exported
        second = workflow.add_link("fixture", download=True)
        assert second["download"] == "ready" and second["download_reused"] and len(downloads) == 1
    finally:
        store.close()


def test_expired_list_media_refreshes_only_exact_work_once(tmp_path, monkeypatch):
    from collection_context.processing.inputs import PreparedInputs
    observed = normalize_item(raw_item())
    fresh = normalize_item(raw_item(video={"play_addr": {"url_list": ["https://fixture.douyinvod.com/fresh"]}}))
    fetched, opened = [], []
    store = LibraryStore.initialize(tmp_path / "new-library")

    def fetch(item):
        fetched.append(item)
        if len(fetched) == 1:
            raise PublicDownloadError(403)
        return [(b"\x00\x00\x00\x14ftypisomsynthetic", "video/mp4")]

    def reopen(url):
        opened.append(url)
        return fresh

    def prepare(*args, **kwargs):
        raise ContextError("frame_limit", "合成选帧上限")

    monkeypatch.setattr(PreparedInputs, "prepare_video", prepare)
    try:
        result = IngestionWorkflow(store, SimpleNamespace(fetch_item=reopen), SimpleNamespace(fetch=fetch)).import_item(
            observed, kind="saved", scope_id="s_saved", download=True,
        )
        assert result["download"] == "ready" and fetched == [observed, fresh]
        assert opened == [observed.source["source_url"]] and result["model_requests"] == 0
        assert len(store.get(result["material_ref"])["relations"]) == 1
    finally:
        store.close()


def test_only_bounded_platform_offered_mirrors_are_used_after_cdn_rejection():
    item = normalize_item(
        raw_item(
            video={"play_addr": {"url_list": ["https://one.douyinvod.com/a", "https://two.douyinvod.com/b"]}}
        )
    )
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise PublicDownloadError(403)
        return Download(b"\x00\x00\x00\x14ftypisom" + b"synthetic", "video/mp4")

    result = DouyinDownloads(SimpleNamespace(get=get)).fetch(item)
    assert result[0][1] == "video/mp4" and len(calls) == 2
    assert all(kwargs == {"timeout": 300, "max_bytes": 1_024_000_000} for _, kwargs in calls)


@pytest.mark.parametrize(
    "error", [PublicDownloadError(429), ContextError("unsafe_public_address", "private")]
)
def test_rate_limit_and_security_rejection_never_switch_mirrors(error):
    item = normalize_item(
        raw_item(
            video={"play_addr": {"url_list": ["https://one.douyinvod.com/a", "https://two.douyinvod.com/b"]}}
        )
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise error

    with pytest.raises(ContextError) as caught:
        DouyinDownloads(SimpleNamespace(get=get)).fetch(item)
    assert caught.value.code == error.code and len(calls) == 1
