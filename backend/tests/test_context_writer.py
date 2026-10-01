from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.cli import main
from collection_context.infrastructure.ownership import ExecutorLease, WriterLease
from collection_context.library.store import LEGACY_GUARD, WRITER_PROTOCOL, LibraryStore


@pytest.fixture
def store(tmp_path):
    library = LibraryStore.initialize(tmp_path / "中文 有空格的库")
    yield library
    library.close()


def source():
    return {"native_id": "123456789", "title": "原创样例", "body": "仅用于恢复验证", "media_type": "video"}


def upsert(store):
    return store.upsert(source(), kind="saved", scope_id="s_saved")


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def legacy_workspace(store):
    config = json.loads(store.files.read("context-workspace.json"))
    config.pop("writer_protocol")
    store.files.write("context-workspace.json", canonical_bytes(config), replace=True)
    store.files.unlink(LEGACY_GUARD)  # Explicit fixture only; never repair a live lock this way.


def test_new_workspace_and_guard_remain_after_writes(store):
    assert json.loads(store.files.read("context-workspace.json"))["writer_protocol"] == WRITER_PROTOCOL
    guard = store.files.read(LEGACY_GUARD)
    with store.writer() as owner:
        owner.check()
        failure("writer_busy", lambda: upsert(store))
    assert upsert(store)["created"]
    assert store.files.read(LEGACY_GUARD) == guard
    assert (store.files.root / WriterLease.path).is_file()
    # An older O_EXCL-only version is deliberately prevented from entering.
    failure("write_conflict", lambda: store.files.write(LEGACY_GUARD, b"old owner"))


def test_executor_and_transaction_ownership_are_separate(store):
    with ExecutorLease(store.files.root) as executor:
        assert upsert(store)["created"]
        executor.check()
        failure("executor_busy", lambda: ExecutorLease(store.files.root))


def test_other_store_cannot_write_until_actual_owner_releases(store):
    other = LibraryStore(store.files.root)
    try:
        with store.writer():
            failure("writer_busy", lambda: upsert(other))
        assert upsert(other)["created"]
    finally:
        other.close()


@pytest.mark.parametrize("replacement", [b"{}", None])
def test_guard_loss_or_change_blocks_writes_without_repair(store, replacement):
    before = store.snapshot()
    if replacement is None:
        store.files.unlink(LEGACY_GUARD)
    else:
        store.files.write(LEGACY_GUARD, replacement, replace=True)
    failure("lock_changed", lambda: upsert(store))
    failure("lock_changed", store.upgrade_writer)
    assert store.snapshot() == before


def test_owner_replacement_during_mutation_prevents_publication(store):
    before = store.snapshot()

    def mutate(state):
        state["settings"]["auto_sync"] = True
        store.files.write(WriterLease.path, b"{}", replace=True)

    failure("lock_changed", lambda: store.transact(mutate))
    assert store.snapshot() == before


def test_ownership_file_deleted_during_mutation_prevents_publication(store):
    before = store.snapshot()

    def mutate(state):
        store.files.unlink(WriterLease.path)

    failure("lock_changed", lambda: store.transact(mutate))
    assert store.snapshot() == before


def test_owner_loss_after_generation_write_prevents_current_switch(store, monkeypatch):
    before = store.snapshot()
    write = store.files.write

    def replace_owner(relative, body, *, replace=False):
        write(relative, body, replace=replace)
        if relative.startswith(".context/提交/c_"):
            write(WriterLease.path, b"{}", replace=True)

    monkeypatch.setattr(store.files, "write", replace_owner)
    failure("lock_changed", lambda: upsert(store))
    assert store.snapshot() == before


def test_os_file_metadata_is_not_a_liveness_claim(store):
    store.files.write(WriterLease.path, b'{"pid":1,"nonce":"old"}')
    assert upsert(store)["created"]  # Acquire kernel lock, then replace its diagnostic metadata.


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversize", "directory"])
def test_ownership_path_is_not_an_unrestricted_file(store, tmp_path, kind):
    path = store.files.root / WriterLease.path
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "oversize":
        path.write_bytes(b"x" * 65_537)
    else:
        path.mkdir()
    with pytest.raises(ContextError):
        upsert(store)
    assert outside.read_bytes() == b"untouched"


def test_old_workspace_remains_readable_but_requires_explicit_upgrade(store):
    item = upsert(store)["item"]
    before = store.snapshot()
    legacy_workspace(store)
    assert store.get(item["id"]) == before["items"][item["id"]]
    failure("writer_upgrade_required", lambda: upsert(store))
    assert store.upgrade_writer() == {"upgraded": True, "resumed": False, "writer_protocol": WRITER_PROTOCOL}
    assert store.snapshot() == before
    assert not upsert(store)["created"]
    assert store.upgrade_writer() == {"upgraded": False, "writer_protocol": WRITER_PROTOCOL}


@pytest.mark.parametrize("old_body", [b"", b"broken", b'{"pid":99999999,"created_at":"1900-01-01"}'])
def test_legacy_lock_never_reclaimed_from_age_pid_or_invalid_body(store, old_body):
    legacy_workspace(store)
    store.files.write(LEGACY_GUARD, old_body)
    before = store.snapshot()
    failure("writer_busy", store.upgrade_writer)
    assert store.files.read(LEGACY_GUARD) == old_body
    assert store.snapshot() == before
    assert "writer_protocol" not in json.loads(store.files.read("context-workspace.json"))


def test_configuration_failure_leaves_known_guard_and_upgrade_can_resume(store, monkeypatch):
    legacy_workspace(store)
    before = store.snapshot()
    write = store.files.write

    def fail_config(relative, body, *, replace=False):
        if relative == "context-workspace.json":
            raise ContextError("storage_unavailable", "合成磁盘故障")
        write(relative, body, replace=replace)

    monkeypatch.setattr(store.files, "write", fail_config)
    failure("storage_unavailable", store.upgrade_writer)
    assert store.files.read(LEGACY_GUARD) == store._guard_body(store.workspace_id)
    failure("writer_upgrade_required", lambda: upsert(store))
    monkeypatch.setattr(store.files, "write", write)
    assert store.upgrade_writer()["resumed"]
    assert store.snapshot() == before
    assert upsert(store)["created"]


def test_unknown_protocol_not_silently_downgraded(store):
    config = json.loads(store.files.read("context-workspace.json"))
    config["writer_protocol"] = "future_version"
    store.files.write("context-workspace.json", canonical_bytes(config), replace=True)
    failure("invalid_workspace", store.upgrade_writer)
    assert json.loads(store.files.read("context-workspace.json"))["writer_protocol"] == "future_version"


def v1_workspace(store):
    config = json.loads(store.files.read("context-workspace.json"))
    config["writer_protocol"] = "os_writer_v1"
    store.files.write("context-workspace.json", canonical_bytes(config), replace=True)
    store.files.write(
        LEGACY_GUARD,
        canonical_bytes(
            {
                "protocol": "os_writer_v1",
                "workspace_id": store.workspace_id,
                "purpose": "legacy_write_guard",
            }
        ),
        replace=True,
    )


def test_v1_queue_library_readable_and_explicit_upgrade_preserves_contents(store):
    upsert(store)
    before = store.snapshot()
    v1_workspace(store)
    assert store.snapshot() == before
    failure("writer_upgrade_required", lambda: upsert(store))
    assert store.upgrade_writer() == {"upgraded": True, "resumed": False, "writer_protocol": WRITER_PROTOCOL}
    assert store.snapshot() == before
    assert store.files.read(LEGACY_GUARD) == store._guard_body(store.workspace_id)


def test_v1_upgrade_rejects_active_executor_and_unknown_guard(store):
    v1_workspace(store)
    before = store.files.read("context-workspace.json")
    with ExecutorLease(store.files.root):
        failure("executor_busy", store.upgrade_writer)
    assert store.files.read("context-workspace.json") == before
    store.files.write(LEGACY_GUARD, b"unknown", replace=True)
    failure("lock_changed", store.upgrade_writer)


def test_v1_guard_first_upgrade_configuration_failure_is_restartable(store, monkeypatch):
    v1_workspace(store)
    before = store.snapshot()
    real = store.files.write

    def fail_config(relative, body, *, replace=False):
        if relative == "context-workspace.json":
            raise ContextError("storage_unavailable", "fixture")
        real(relative, body, replace=replace)

    monkeypatch.setattr(store.files, "write", fail_config)
    failure("storage_unavailable", store.upgrade_writer)
    assert store.files.read(LEGACY_GUARD) == store._guard_body(store.workspace_id)
    assert json.loads(store.files.read("context-workspace.json"))["writer_protocol"] == "os_writer_v1"
    monkeypatch.setattr(store.files, "write", real)
    assert store.upgrade_writer()["resumed"]
    assert store.snapshot() == before


def test_workspace_identity_change_is_rejected_before_mutation(store):
    config = json.loads(store.files.read("context-workspace.json"))
    config["workspace_id"] = "w_other"
    store.files.write("context-workspace.json", canonical_bytes(config), replace=True)
    failure("invalid_workspace", lambda: upsert(store))


def test_cli_explicit_upgrade_returns_safe_result_and_does_not_touch_items(store, capsys):
    before = store.snapshot()
    legacy_workspace(store)
    assert main(["--workspace", str(store.files.root), "upgrade-writer"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["upgraded"] and result["data"]["writer_protocol"] == WRITER_PROTOCOL
    assert store.snapshot() == before


def test_actual_legacy_writer_is_not_taken_over_by_upgrade(store):
    legacy_workspace(store)
    code = """
import sys
from pathlib import Path
from collection_context.infrastructure.files import SafeFiles
files = SafeFiles(Path(sys.argv[1]))
files.write('.context/写锁.json', b'{"nonce":"actual_legacy_owner"}')
print('legacy-owned', flush=True)
sys.stdin.read(1)
files.unlink('.context/写锁.json')
files.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(store.files.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        assert process.stdout is not None and select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "legacy-owned"
        failure("writer_busy", store.upgrade_writer)
        assert store.files.read(LEGACY_GUARD) == b'{"nonce":"actual_legacy_owner"}'
        assert process.stdin is not None
        process.stdin.write("x")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
        assert store.upgrade_writer()["upgraded"]
        assert upsert(store)["created"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


CHILD = """
import sys
from pathlib import Path
from collection_context.library.store import LibraryStore
store = LibraryStore(Path(sys.argv[1]))
target = sys.argv[2]
write = store.files.write
def intercept(relative, body, *, replace=False):
    write(relative, body, replace=replace)
    selected = relative.startswith('.context/提交/c_') if target == 'before' else relative == '.context/提交/CURRENT.json'
    if selected:
        print('paused', flush=True)
        sys.stdin.read(1)
store.files.write = intercept
store.upsert({'native_id':'123456789','title':'原创样例','body':'仅用于恢复验证','media_type':'video'}, kind='saved', scope_id='s_saved')
"""


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_real_process_death_preserves_commit_boundary_and_releases_writer(store, boundary):
    before = store.snapshot()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(store.files.root), boundary],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "paused"
        failure("writer_busy", lambda: upsert(store))
        process.kill()
        assert process.wait(timeout=5) < 0
        with LibraryStore(store.files.root).files as files:
            assert files.read(LEGACY_GUARD) == store._guard_body(store.workspace_id)
        reopened = LibraryStore(store.files.root)
        try:
            snapshot = reopened.snapshot()
            if boundary == "before":
                assert snapshot == before
            else:
                assert snapshot["generation"] == before["generation"] + 1
                assert len(snapshot["items"]) == 1
            assert upsert(reopened)["created"] == (boundary == "before")
            assert len(reopened.snapshot()["items"]) == 1
        finally:
            reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
