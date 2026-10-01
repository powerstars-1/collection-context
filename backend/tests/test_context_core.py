from __future__ import annotations

import json
import os

import pytest

from collection_context.application.contracts import ContextError, item_id, validate_source
from collection_context.infrastructure.ownership import ExecutorLease
from collection_context.library.store import LibraryStore
from collection_context.workflows.jobs import JobManager


@pytest.fixture
def store(tmp_path):
    library = LibraryStore.initialize(tmp_path / "有空格的 新库")
    yield library
    library.close()


def source(native="123456789"):
    return {"native_id": native, "title": "页面 UI 教程", "body": "原始文字", "media_type": "video"}


def assert_error(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def test_init_does_not_overwrite_existing_directory(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / "personal.md").write_text("私有资料", encoding="utf-8")
    assert_error("workspace_not_empty", lambda: LibraryStore.initialize(root))
    assert (root / "personal.md").read_text(encoding="utf-8") == "私有资料"


def test_initialization_off_by_default_and_reopen(store):
    original = store.snapshot()
    assert original["settings"] == {"auto_sync": False, "auto_process": False}
    other = LibraryStore(store.files.root)
    try:
        assert other.snapshot() == original
    finally:
        other.close()


def test_multisource_keeps_one_content_and_unknown_action_time(store):
    for kind, scope in [
        ("liked", "s_likes"),
        ("saved", "s_saved"),
        ("collection", "s_folder1"),
        ("collection", "s_folder2"),
        ("creator", "s_creator"),
    ]:
        store.upsert(source(), kind=kind, scope_id=scope)
    items = store.snapshot()["items"]
    assert len(items) == 1
    item = next(iter(items.values()))
    assert len(item["relations"]) == 5
    assert all(r["action_at"] is None for r in item["relations"].values())
    store.upsert(source(), kind="liked", scope_id="s_likes")
    assert len(store.get(item["id"])["relations"]) == 5


def test_actual_action_time_requires_evidence(store):
    assert_error(
        "invalid_time",
        lambda: store.upsert(
            source(), kind="liked", scope_id="s_likes", action_at="2026-10-01T08:00:00+08:00"
        ),
    )
    result = store.upsert(
        source(),
        kind="liked",
        scope_id="s_likes",
        action_at="2026-10-01T08:00:00+08:00",
        action_basis="平台明确字段",
    )
    relation = next(iter(result["item"]["relations"].values()))
    assert relation["action_at"] == "2026-10-01T00:00:00+00:00"


def test_version_change_marks_machine_outputs_stale_not_user_note(store):
    item = store.upsert(source(), kind="saved", scope_id="s_saved")["item"]
    for kind in ("audio", "user_note"):
        store.save_artifact(
            item["id"], kind, "已保存", processor_version="p1", expected_content_hash=item["content_hash"]
        )
    result = store.upsert({**source(), "body": "作者更新"}, kind="saved", scope_id="s_saved")
    assert result["content_changed"]
    assert result["item"]["artifacts"]["audio"]["state"] == "stale"
    assert result["item"]["artifacts"]["user_note"]["state"] == "ready"
    assert_error(
        "version_changed",
        lambda: store.save_artifact(
            item["id"],
            "screen",
            "旧输入提取",
            processor_version="p1",
            expected_content_hash=item["content_hash"],
        ),
    )


def test_source_relation_only_update_keeps_valid_artifact(store):
    item = store.upsert(source(), kind="saved", scope_id="s_saved")["item"]
    artifact = store.save_artifact(
        item["id"], "audio", "音频内容", processor_version="p1", expected_content_hash=item["content_hash"]
    )
    result = store.upsert(source(), kind="liked", scope_id="s_likes")["item"]
    assert result["artifacts"]["audio"] == artifact


def test_exclude_blocks_direct_reference_and_can_restore(store):
    ref = store.upsert(source(), kind="saved", scope_id="s_saved")["item"]["id"]
    store.exclude(ref)
    assert_error("not_found", lambda: store.get(ref))
    store.exclude(ref, False)
    assert store.get(ref)["id"] == ref


def test_single_writer_never_reclaims_lock_from_file_alone(store):
    with store.writer():
        assert_error("writer_busy", lambda: store.upsert(source(), kind="saved", scope_id="s_saved"))
    assert store.upsert(source(), kind="saved", scope_id="s_saved")["created"]


def test_orphan_generation_never_visible(store):
    before = store.snapshot()
    store.files.write(".context/提交/c_orphan.json", b'{"items":{"wrong":1}}')
    assert store.snapshot() == before


def test_failed_transaction_retains_committed_generation(store, monkeypatch):
    before = store.snapshot()
    publish = store._publish

    def fail_before_pointer(files, state, *, before_commit=None):
        files.write(".context/提交/c_failed.json", json.dumps(state).encode())
        raise ContextError("storage_unavailable", "模拟提交中断")

    monkeypatch.setattr(store, "_publish", fail_before_pointer)
    assert_error("storage_unavailable", lambda: store.upsert(source(), kind="saved", scope_id="s_saved"))
    assert store.snapshot() == before
    monkeypatch.setattr(store, "_publish", publish)
    assert store.upsert(source(), kind="saved", scope_id="s_saved")["created"]


def test_commit_hash_tampering_detected(store):
    pointer = json.loads(store.files.read(".context/提交/CURRENT.json"))
    path = f".context/提交/{pointer['version']}.json"
    store.files.write(path, b"{}", replace=True)
    assert_error("corrupt_workspace", store.snapshot)


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a//b", "a/./b", "a/../b", "C:/secret", "a\\b"])
def test_no_path_traversal(store, path):
    assert_error("forbidden_path", lambda: store.files.read(path))


def test_symlink_and_hardlink_rejected(store, tmp_path):
    private = tmp_path / "secret"
    private.write_text("不允许读取", encoding="utf-8")
    os.symlink(private, store.files.root / "linked")
    assert_error("forbidden_path", lambda: store.files.read("linked"))
    os.link(private, store.files.root / "hardlinked")
    assert_error("forbidden_path", lambda: store.files.read("hardlinked"))


def test_missing_mount_identity_does_not_create_new_directory(store):
    root = store.files.root
    root.rename(root.with_name("暂时断开的库"))
    root.mkdir()
    assert_error("storage_unavailable", store.snapshot)
    assert not list(root.iterdir())


def test_job_idempotency_and_principal_isolation(store):
    jobs = JobManager(store)
    a = jobs.submit("process", {"item": "i_test"}, idempotency_key="one", max_calls=2)
    assert jobs.submit("process", {"item": "i_test"}, idempotency_key="one", max_calls=2)["id"] == a["id"]
    assert_error(
        "idempotency_conflict",
        lambda: jobs.submit("process", {"item": "i_other"}, idempotency_key="one", max_calls=2),
    )
    assert_error("not_found", lambda: jobs.get(a["id"], principal="other_owner"))


def test_paid_call_budget_and_successful_reuse(store):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="budget", max_calls=1)
    jobs.start(job["id"])
    call = jobs.begin_call(job["id"], stage="audio", input_hash="hash", processor_version="v1")
    assert not call["reused"]
    jobs.finish_call(
        job["id"],
        call["id"],
        outcome="completed",
        actual_model="test-model",
        usage={"prompt_tokens": 10, "total_tokens": 15},
    )
    assert jobs.begin_call(job["id"], stage="audio", input_hash="hash", processor_version="v1")["reused"]
    assert_error(
        "budget_required",
        lambda: jobs.begin_call(job["id"], stage="screen", input_hash="h", processor_version="v1"),
    )
    assert jobs.get(job["id"])["calls"][0]["usage"]["total_tokens"] == 15


def test_request_intent_blocks_unconfirmed_repeat_after_restart(store):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="crash", max_calls=5)
    jobs.start(job["id"])
    jobs.begin_call(job["id"], stage="audio", input_hash="h", processor_version="v1")
    assert_error(
        "upstream_outcome_unknown",
        lambda: jobs.begin_call(job["id"], stage="audio", input_hash="h", processor_version="v1"),
    )
    assert_error("upstream_outcome_unknown", lambda: jobs.finish(job["id"], "succeeded"))
    with ExecutorLease(store.files.root) as lease:
        recovered = JobManager(store).recover_interrupted(lease=lease)
    assert recovered[0]["state"] == "blocked"
    assert recovered[0]["error"]["possibly_charged"]
    assert len(recovered[0]["calls"]) == 1


def test_local_interrupted_job_can_requeue_without_paid_request(store):
    jobs = JobManager(store)
    job = jobs.submit("add", {}, idempotency_key="local")
    jobs.start(job["id"])
    with ExecutorLease(store.files.root) as lease:
        assert jobs.recover_interrupted(lease=lease)[0]["state"] == "queued"


def test_cancel_does_not_promise_refund_or_hide_late_usage(store):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key="cancel", max_calls=1)
    jobs.start(job["id"])
    call = jobs.begin_call(job["id"], stage="audio", input_hash="h", processor_version="v1")
    jobs.cancel(job["id"])
    jobs.finish_call(job["id"], call["id"], outcome="completed")
    assert jobs.get(job["id"])["state"] == "cancelled"
    assert jobs.get(job["id"])["calls"][0]["usage"] is None


@pytest.mark.parametrize("native", ["../123", "１２３", "", 123])
def test_source_identity_is_ascii_platform_id(native):
    assert_error("invalid_source_item", lambda: item_id("douyin", native))


def test_source_url_has_no_share_token_and_publish_is_utc():
    result = validate_source(
        {
            **source(),
            "source_url": "https://example.com/?secret=test",
            "published_at": "2026-10-01T08:00:00+08:00",
        }
    )
    assert result["source_url"] == "https://www.douyin.com/video/123456789"
    assert result["published_at"] == "2026-10-01T00:00:00+00:00"
