"""Single-link owner admission, durable source work and zero model authority."""

import io
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image
from test_context_management import managed as managed
from test_context_scheduling import env as env
from test_context_sources import raw_item

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.service import ContextService
from collection_context.cli import main, no_model_authority, source_session
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.sources.douyin import normalize_item
from collection_context.workflows.addition import AdditionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker


@pytest.fixture
def addition(tmp_path):
    store = LibraryStore.initialize(tmp_path / "单条任务库")
    seen = []
    source = SimpleNamespace(fetch_item=lambda *a, **k: normalize_item(raw_item()))

    @contextmanager
    def factory():
        seen.append("open")
        try:
            yield source
        finally:
            seen.append("close")

    try:
        yield store, AdditionWorkflow(store, factory), source, seen, factory
    finally:
        store.close()


def submit(addition, *, key="one", url="https://www.douyin.com/video/81", download=False):
    return addition[1].submit(url=url, download=download, idempotency_key=key, source_confirmed=True)


def test_submit_is_local_normalized_idempotent_and_does_not_admit_models(addition):
    job = submit(addition, url="分享 https://www.douyin.com/video/81?secret=hidden")
    assert submit(addition)["id"] == job["id"]
    assert not addition[3] and not addition[0].snapshot()["items"]
    assert job["budget"]["max_calls"] == 0 and not job["calls"]
    assert job["payload"] == {
        "link": {"schema_version": 1, "url": "https://www.douyin.com/video/81", "download": False}
    }
    assert "hidden" not in json.dumps(addition[0].snapshot())
    with pytest.raises(ContextError, match="已有待执行"):
        submit(addition, key="different")
    with pytest.raises(ContextError) as caught:
        submit(addition, download=True)
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "options",
    [
        {"source_confirmed": False},
        {"source_confirmed": 1},
        {"download": 1},
        {"url": "https://www.douyin.com/user/MS4w-synthetic"},
        {"url": "https://127.0.0.1/video/81"},
        {"url": "https://www.douyin.com/video/81 https://www.douyin.com/video/82"},
    ],
)
def test_unsafe_or_unconfirmed_submission_does_not_write_or_open(addition, options):
    before = addition[0].snapshot()
    args = dict(
        url="https://www.douyin.com/video/81", download=False, source_confirmed=True, idempotency_key="one"
    )
    args.update(options)
    with pytest.raises(ContextError):
        addition[1].submit(**args)
    assert addition[0].snapshot() == before and not addition[3]


def test_worker_source_authority_only_and_searchable_one_work(addition):
    job = submit(addition)
    worker = BackgroundWorker(
        ExtractionWorkflow(addition[0], no_model_authority), SynchronizationWorkflow(addition[0], addition[4])
    )
    assert worker.serve(allow_model_calls=True, once=True)["handled"] == 0
    assert not addition[3]
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 1
    result = addition[1].jobs.get(job["id"])
    assert result["state"] == "succeeded" and not result["calls"]
    assert addition[3] == ["open", "close"]
    assert len(addition[0].snapshot()["items"]) == 1
    assert ContextService(addition[0]).search("完整正文")["items"]
    assert worker.serve(allow_model_calls=False, allow_source_sync=True, once=True)["handled"] == 0


def test_download_partial_preserves_metadata_no_hidden_retries(addition, monkeypatch):
    def download(*a):
        raise ContextError("download_rejected", "合成下载失败")

    monkeypatch.setattr("collection_context.sources.downloads.DouyinDownloads.fetch", download)
    job = submit(addition, download=True)
    result = addition[1].run(job["id"])
    assert result["state"] == "partial"
    public = ManagementService(addition[0]).overview()["jobs"][0]
    assert public["link_result"]["metadata"] == "ready"
    assert public["link_result"]["download_error_code"] == "download_rejected"
    assert not result["calls"] and len(addition[0].snapshot()["items"]) == 1
    assert "signature" not in json.dumps(result)


def recover(addition):
    with ExecutorLease(addition[0].files.root) as lease:
        addition[1].jobs.recover_interrupted(lease=lease)


def test_ready_metadata_checkpoint_survives_interruption_without_reobservation(addition, monkeypatch):
    job = submit(addition)
    original = addition[1].jobs.finish
    monkeypatch.setattr(addition[1].jobs, "finish", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        addition[1].run(job["id"])
    assert len(addition[0].snapshot()["items"]) == 1
    monkeypatch.setattr(addition[1].jobs, "finish", original)
    recover(addition)
    assert addition[1].run(job["id"])["state"] == "succeeded"
    assert addition[3] == ["open", "close"]


def test_checkpoint_resume_does_not_overwrite_changed_source(addition, monkeypatch):
    job = submit(addition, download=True)

    def interrupt(*a):
        raise SystemExit()

    monkeypatch.setattr(IngestionWorkflow, "prepare_download", interrupt)
    with pytest.raises(SystemExit):
        addition[1].run(job["id"])
    assert addition[1].jobs.get(job["id"])["stages"]["link_import"]["result"]["result"]["metadata"] == "ready"
    addition[2].fetch_item = lambda *a, **k: normalize_item(raw_item(desc="改变后的正文"))
    recover(addition)
    result = addition[1].run(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "source_snapshot_changed"
    assert "完整正文" in next(iter(addition[0].snapshot()["items"].values()))["body"]


def test_cancellation_during_observation_prevents_import(addition):
    job = submit(addition)

    def observe(*a, **k):
        addition[1].jobs.cancel(job["id"])
        assert k["cancelled"]()
        return normalize_item(raw_item())

    addition[2].fetch_item = observe
    assert addition[1].run(job["id"])["state"] == "cancelled"
    assert not addition[0].snapshot()["items"]


def test_repeated_image_ingestion_reuses_media_without_cloud(addition, monkeypatch):
    raw = raw_item(
        aweme_type=68,
        images=[{"url_list": ["https://example.douyinvod.com/page1?signature=secret"]}],
    )
    addition[2].fetch_item = lambda *a, **k: normalize_item(raw)
    image = io.BytesIO()
    Image.new("RGB", (80, 80)).save(image, "PNG")
    calls = []

    def download(*a):
        calls.append(1)
        return [(image.getvalue(), "image/png")]

    monkeypatch.setattr("collection_context.sources.downloads.DouyinDownloads.fetch", download)
    first = submit(addition, download=True)
    assert addition[1].run(first["id"])["state"] == "succeeded"
    second = submit(addition, key="two", download=True)
    result = addition[1].run(second["id"])
    assert result["state"] == "succeeded" and len(calls) == 1
    assert result["stages"]["link_import"]["result"]["result"]["download_reused"] is True
    assert len(addition[0].snapshot()["items"]) == 1


def test_owner_http_link_actions_are_cookie_csrf_only_and_strict(managed, env):
    client, headers, _, owner, _, viewer = managed
    args = dict(
        url="https://www.douyin.com/video/81",
        download=False,
        idempotency_key="http-one",
        source_confirmed=True,
    )
    assert client.post("/v1/management/link-submit", json=args).status_code == 403
    assert (
        client.post(
            "/v1/management/link-submit", json=args, headers={"Authorization": "Bearer " + owner["token"]}
        ).status_code
        == 403
    )
    assert not env[0].snapshot()["jobs"]
    result = client.post("/v1/management/link-submit", json=args, headers=headers).json()
    assert result["ok"] and result["data"]["max_calls"] == 0
    ref = result["data"]["job_id"]
    assert (
        client.post("/v1/management/link-status", json={"job_id": ref}, headers=headers).json()["data"][
            "state"
        ]
        == "queued"
    )
    assert (
        client.post(
            "/v1/management/link-submit", json={**args, "browser_dir": "/tmp/other"}, headers=headers
        ).json()["error"]["code"]
        == "invalid_argument"
    )
    csrf = client.post("/v1/session", json={"token": viewer["token"]}).json()["data"]["csrf_token"]
    assert (
        client.post("/v1/management/link-submit", json=args, headers={"X-CSRF-Token": csrf}).status_code
        == 403
    )


def test_cli_submit_uses_same_local_admission_without_browser(addition, capsys):
    args = [
        "--workspace",
        str(addition[0].files.root),
        "submit-link",
        "--url",
        "https://www.douyin.com/video/81",
        "--idempotency-key",
        "cli-one",
    ]
    assert main(args) == 1
    assert not addition[0].snapshot()["jobs"]
    assert main([*args, "--allow-source-sync"]) == 0
    output = json.loads(capsys.readouterr().out.splitlines()[-1])["data"]
    assert output["kind"] == "add" and output["state"] == "queued" and not addition[3]


def test_immediate_cli_reuses_durable_owner_use_case(addition, capsys, monkeypatch):
    monkeypatch.setattr("collection_context.cli.source_session", lambda *a, **k: addition[4]())
    args = [
        "--workspace",
        str(addition[0].files.root),
        "add-link",
        "--url",
        "https://www.douyin.com/video/81",
        "--browser-dir",
        "unused-original-test-directory",
        "--idempotency-key",
        "direct-cli",
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)["data"]
    assert output["state"] == "succeeded" and output["metadata"] == "ready"
    assert main(args) == 0
    assert len(addition[0].snapshot()["jobs"]) == 1 and addition[3] == ["open", "close"]


def test_changed_library_metadata_cannot_reuse_ready_checkpoint(addition, monkeypatch):
    job = submit(addition)
    original = addition[1].jobs.finish
    monkeypatch.setattr(addition[1].jobs, "finish", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        addition[1].run(job["id"])
    monkeypatch.setattr(addition[1].jobs, "finish", original)
    addition[0].upsert(
        normalize_item(raw_item(desc="外部修改的文案")).source, kind="saved", scope_id="s_saved"
    )
    recover(addition)
    result = addition[1].run(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "source_snapshot_changed"
    assert addition[3] == ["open", "close"]


def test_download_time_content_change_cannot_register_old_media(addition, monkeypatch):
    addition[2].fetch_item = lambda *a, **k: normalize_item(
        raw_item(images=[{"url_list": ["https://example.douyinvod.com/page1"]}])
    )
    image = io.BytesIO()
    Image.new("RGB", (80, 80)).save(image, "PNG")

    def download(*a):
        changed = normalize_item(
            raw_item(desc="下载期间更改", images=[{"url_list": ["https://example.douyinvod.com/page1"]}])
        )
        addition[0].upsert(changed.source, kind="saved", scope_id="s_saved")
        return [(image.getvalue(), "image/png")]

    monkeypatch.setattr("collection_context.sources.downloads.DouyinDownloads.fetch", download)
    job = submit(addition, download=True)
    result = addition[1].run(job["id"])
    assert result["state"] == "partial"
    assert result["stages"]["link_import"]["result"]["result"]["error"]["code"] == "version_changed"
    assert not addition[0].snapshot().get("prepared_inputs")


@pytest.mark.parametrize("change", [{"model_requests": 1}, {"download": "ready"}, {"created": 1}])
def test_invalid_checkpoint_is_blocked_without_reobservation(addition, monkeypatch, change):
    job = submit(addition)
    original = addition[1].jobs.finish
    monkeypatch.setattr(addition[1].jobs, "finish", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        addition[1].run(job["id"])
    monkeypatch.setattr(addition[1].jobs, "finish", original)

    def tamper(state):
        state["jobs"][job["id"]]["stages"]["link_import"]["result"]["result"].update(change)

    addition[0].transact(tamper)
    recover(addition)
    result = addition[1].run(job["id"])
    assert result["state"] == "blocked" and result["error"]["code"] == "invalid_link_checkpoint"
    assert addition[3] == ["open", "close"]


def test_source_profile_parent_link_cannot_hide_library_overlap(addition, tmp_path):
    alias = tmp_path / "看似库外的目录"
    alias.symlink_to(addition[0].files.root, target_is_directory=True)
    with pytest.raises(ContextError) as caught:
        with source_session(addition[0], alias / "登录目录"):
            pytest.fail("Must reject before any browser launch")
    assert caught.value.code == "unsafe_login_profile"
    assert not (addition[0].files.root / "登录目录").exists()
