"""No-op recovery elision retains locks, authorization and real durable recovery."""

from __future__ import annotations

import json
import socket
from contextlib import contextmanager

import pytest

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.ownership import ExecutorLease, WriterLease
from collection_context.library.index import FileIndex
from collection_context.library.store import LEGACY_GUARD, LibraryStore
from collection_context.workflows.jobs import JobManager


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("No-op recovery tests cannot contact a platform or model")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def store(tmp_path):
    library = LibraryStore.initialize(tmp_path / "原创 空恢复库")
    try:
        yield library
    finally:
        library.close()


def fail(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def start_job(store, key, *, principal="local_owner", paid=False):
    jobs = JobManager(store)
    job = jobs.submit("process", {}, idempotency_key=key, max_calls=1, principal=principal)
    jobs.start(job["id"], principal=principal)
    if paid:
        jobs.begin_call(
            job["id"],
            stage="vision",
            input_hash="original-input",
            processor_version="p1",
            principal=principal,
            start_stage=True,
        )
    return job["id"]


def test_empty_recovery_does_not_publish_manifest_or_index(store, monkeypatch):
    before = store.snapshot()
    current = store.files.read(".context/提交/CURRENT.json")
    index = store.files.read(".context/索引/CURRENT.json")

    def no_write(*_args, **_kwargs):
        pytest.fail("Empty recovery must not publish any manifest, index or artifact")

    monkeypatch.setattr(store.files, "write", no_write)
    monkeypatch.setattr(FileIndex, "rebuild_committed", no_write)
    with ExecutorLease(store.files.root) as owner:
        assert JobManager(store).recover_interrupted(lease=owner, principal="local_owner") == []
        owner.check()
    assert store.snapshot() == before
    assert store.files.read(".context/提交/CURRENT.json") == current
    assert store.files.read(".context/索引/CURRENT.json") == index
    assert (store.files.root / WriterLease.path).is_file()


def test_general_transaction_default_still_publishes_noop(store):
    before = store.snapshot()
    pointer = store.files.read(".context/提交/CURRENT.json")
    called = []
    assert store.transact(lambda _state: [], before_commit=lambda: called.append(True)) == []
    assert called == [True]
    assert store.snapshot()["generation"] == before["generation"] + 1
    assert store.files.read(".context/提交/CURRENT.json") != pointer


def test_opt_in_uses_canonical_state_not_result_and_preserves_real_changes(store):
    before = store.snapshot()

    def changed(state):
        state["settings"]["auto_process"] = True
        return []

    assert store.transact(changed, skip_unchanged=True) == []
    after = store.snapshot()
    assert after["settings"]["auto_process"] is True
    assert after["generation"] == before["generation"] + 1


def test_noop_result_is_detached_and_commit_callback_runs(store):
    before = store.snapshot()
    called = []
    result = store.transact(
        lambda state: state, skip_unchanged=True, before_commit=lambda: called.append(True)
    )
    result["settings"]["auto_sync"] = True
    assert called == [True] and store.snapshot() == before


@pytest.mark.parametrize("bad", [1, None, "true"])
def test_invalid_internal_flag_cannot_relax_default_checks(store, bad):
    before = store.snapshot()
    fail("invalid_argument", lambda: store.transact(lambda _state: None, skip_unchanged=bad))
    assert store.snapshot() == before


@pytest.mark.parametrize("paid", [False, True])
def test_actual_recovery_is_durable_and_does_not_rebuild_content_index(store, monkeypatch, paid):
    ref = start_job(store, "original-uncertain" if paid else "original-local", paid=paid)
    if not paid:
        JobManager(store).set_stage(ref, "audio", state_name="running", input_hash="original-input")
    before = store.snapshot()
    index = store.files.read(".context/索引/CURRENT.json")

    def no_index(*_args, **_kwargs):
        pytest.fail("Job-only recovery must not rebuild the content index")

    monkeypatch.setattr(FileIndex, "rebuild_committed", no_index)
    with ExecutorLease(store.files.root) as owner:
        recovered = JobManager(store).recover_interrupted(lease=owner, principal="local_owner")
    assert [job["id"] for job in recovered] == [ref]
    reopened = LibraryStore(store.files.root)
    try:
        after = reopened.snapshot()
        job = after["jobs"][ref]
        assert after["generation"] == before["generation"] + 1
        assert job["state"] == ("blocked" if paid else "queued")
        if paid:
            assert job["error"]["code"] == "upstream_outcome_unknown"
            assert job["calls"][0]["state"] == "intent" and len(job["calls"]) == 1
            assert job["stages"]["vision"]["state"] == "blocked"
        else:
            assert job["stages"]["audio"]["state"] == "interrupted"
        assert reopened.files.read(".context/索引/CURRENT.json") == index
    finally:
        reopened.close()


@pytest.mark.parametrize("scope", ["principal", "ids", "both", "none"])
def test_recovery_preserves_principal_and_job_id_scope(store, scope):
    own = start_job(store, "own")
    own_other = start_job(store, "own-other")
    foreign = start_job(store, "foreign", principal="other_owner")
    principal = "local_owner" if scope in {"principal", "both"} else None
    job_ids = {own, foreign} if scope in {"ids", "both"} else None
    expected = {
        "principal": {own, own_other},
        "ids": {own, foreign},
        "both": {own},
        "none": {own, own_other, foreign},
    }[scope]
    with ExecutorLease(store.files.root) as owner:
        recovered = JobManager(store).recover_interrupted(lease=owner, principal=principal, job_ids=job_ids)
    assert {job["id"] for job in recovered} == expected
    assert all(
        job["state"] == ("queued" if ref in expected else "running")
        for ref, job in store.snapshot()["jobs"].items()
    )


def test_scoped_empty_recovery_cannot_touch_foreign_running_job(store):
    ref = start_job(store, "foreign-only", principal="other_owner")
    before = store.snapshot()
    with ExecutorLease(store.files.root) as owner:
        assert (
            JobManager(store).recover_interrupted(lease=owner, principal="local_owner", job_ids={ref}) == []
        )
    assert store.snapshot() == before


def test_empty_recovery_still_requires_actual_executor_and_writer_ownership(store, tmp_path):
    other = LibraryStore.initialize(tmp_path / "other-library")
    try:
        with ExecutorLease(other.files.root) as owner:
            fail("executor_not_owned", lambda: JobManager(store).recover_interrupted(lease=owner))
        with ExecutorLease(store.files.root) as owner:
            with store.writer():
                fail("writer_busy", lambda: JobManager(store).recover_interrupted(lease=owner))
        fail("executor_not_owned", lambda: JobManager(store).recover_interrupted(lease=owner))
    finally:
        other.close()


@pytest.mark.parametrize(
    "boundary,code",
    [
        ("writer", "lock_changed"),
        ("guard", "lock_changed"),
        ("config", "writer_upgrade_required"),
        ("root", "storage_unavailable"),
    ],
)
def test_noop_mutation_cannot_hide_replaced_safety_boundary(store, boundary, code):
    before = store.snapshot()
    pointer = store.files.read(".context/提交/CURRENT.json")
    root = store.files.root

    def changed_boundary(_state):
        if boundary == "writer":
            store.files.write(WriterLease.path, b"{}", replace=True)
        elif boundary == "guard":
            store.files.write(LEGACY_GUARD, b"{}", replace=True)
        elif boundary == "config":
            config = json.loads(store.files.read("context-workspace.json"))
            config.pop("writer_protocol")
            store.files.write("context-workspace.json", canonical_bytes(config), replace=True)
        else:
            root.rename(root.with_name("original-displaced-library"))
            root.mkdir(mode=0o700)

    fail(code, lambda: store.transact(changed_boundary, skip_unchanged=True))
    if boundary == "root":
        old = LibraryStore(root.with_name("original-displaced-library"))
        try:
            assert old.snapshot() == before
            assert old.files.read(".context/提交/CURRENT.json") == pointer
        finally:
            old.close()
    else:
        assert store.snapshot() == before and store.files.read(".context/提交/CURRENT.json") == pointer


def test_noop_before_commit_denial_is_not_reported_success(store):
    before = store.snapshot()

    def denied():
        raise ContextError("scope_revoked", "Original isolated authorization was revoked")

    fail(
        "scope_revoked", lambda: store.transact(lambda _state: [], skip_unchanged=True, before_commit=denied)
    )
    assert store.snapshot() == before


def test_noop_checks_permission_again_after_before_commit_revocation(store):
    before = store.snapshot()
    revoked = False

    def guard():
        if revoked:
            raise ContextError("scope_revoked", "Original isolated authorization was revoked")

    def revoke():
        nonlocal revoked
        revoked = True

    store._mutation_guard = guard
    fail(
        "scope_revoked", lambda: store.transact(lambda _state: [], skip_unchanged=True, before_commit=revoke)
    )
    assert store.snapshot() == before


def test_noop_preserves_writer_exit_authorization_check(store, monkeypatch):
    before = store.snapshot()
    revoked = False
    original_writer = store.writer

    def guard():
        if revoked:
            raise ContextError("scope_revoked", "Original isolated authorization was revoked")

    @contextmanager
    def checked_exit():
        nonlocal revoked
        with original_writer() as owner:
            yield owner
            revoked = True

    store._mutation_guard = guard
    monkeypatch.setattr(store, "writer", checked_exit)
    fail("scope_revoked", lambda: store.transact(lambda _state: [], skip_unchanged=True))
    assert store.snapshot() == before
