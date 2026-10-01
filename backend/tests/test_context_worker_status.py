"""Live kernel status, not stale metadata/PIDs; observation never writes or dispatches."""

import hashlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from test_context_management import managed as managed
from test_context_management import post
from test_context_scheduling import env as env
from test_context_worker import environment as environment

from collection_context.application.management import ManagementService
from collection_context.application.source_management import SourceManagement
from collection_context.infrastructure.ownership import WorkerLease
from collection_context.workflows.worker import BackgroundWorker


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_missing_and_stale_file_are_offline_without_creating_or_rewriting(env):
    store = env[0]
    before = hashes(store.files.root)
    assert WorkerLease.observe(store.files)["online"] is False
    assert hashes(store.files.root) == before
    # No PID reuse, ancient time or valid-looking nonce can make an unlocked file live.
    store.files.write(WorkerLease.path, b'{"pid":1,"nonce":"e_00000000000000000000000000000000"}')
    before = hashes(store.files.root)
    for _ in range(3):
        assert WorkerLease.observe(store.files)["online"] is False
    assert hashes(store.files.root) == before


@pytest.mark.parametrize("capabilities", [(True, False), (False, True), (True, True)])
def test_live_authority_and_original_lease_not_changed(env, capabilities):
    store = env[0]
    with WorkerLease(
        store.files.root, allow_model_calls=capabilities[0], allow_source_sync=capabilities[1]
    ) as lease:
        before = hashes(store.files.root)
        for _ in range(3):
            value = WorkerLease.observe(store.files)
            assert value["online"] is True and value["progress_verified"] is False
            assert value["capabilities"] == dict(
                zip(("model_calls", "source_sync"), capabilities, strict=True)
            )
            assert "pid" not in json.dumps(value) and "nonce" not in json.dumps(value)
            lease.check()
        assert hashes(store.files.root) == before
    assert WorkerLease.observe(store.files)["online"] is False


def test_legacy_live_worker_has_unknown_authority(env):
    with WorkerLease(env[0].files.root):
        value = WorkerLease.observe(env[0].files)
        assert value["online"] is True
        assert value["capabilities"] == {"model_calls": None, "source_sync": None}


def test_actual_worker_reports_authority_while_idle_and_preserves_generation(environment):
    store, _, _, workflow = environment
    events = []
    before = store.snapshot()["generation"]

    def emit(event):
        if event["event"] == "worker_started":
            events.append(WorkerLease.observe(store.files))

    BackgroundWorker(workflow).serve(allow_model_calls=True, once=True, emit=emit)
    assert events[0]["online"] is True and events[0]["capabilities"]["model_calls"] is True
    assert store.snapshot()["generation"] == before
    assert WorkerLease.observe(store.files)["online"] is False


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "oversize"])
def test_unsafe_lock_is_unknown_and_never_read_as_online(env, tmp_path, kind):
    target = env[0].files.root / WorkerLease.path
    if kind in {"symlink", "hardlink"}:
        outside = tmp_path / "not_a_worker"
        outside.write_bytes(b"private-other-content")
        if kind == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"x" * 65_537)
    assert WorkerLease.observe(env[0].files)["online"] is None


@pytest.mark.parametrize(
    "body",
    [b"broken", b"{}", b'{"nonce":1,"pid":1}', b'{"nonce":"e_00000000000000000000000000000000","pid":true}'],
)
def test_busy_invalid_metadata_is_unknown(env, body):
    import fcntl

    target = env[0].files.root / WorkerLease.path
    target.write_bytes(body)
    with target.open("rb") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert WorkerLease.observe(env[0].files)["online"] is None


def test_replaced_lock_or_root_is_unknown_not_old_live_claim(env, monkeypatch):
    store = env[0]
    with WorkerLease(store.files.root):
        original = os.pread

        def changed(fd, length, offset):
            value = original(fd, length, offset)
            path = store.files.root / WorkerLease.path
            path.rename(path.with_suffix(".old"))
            path.write_bytes(b"not-original")
            return value

        monkeypatch.setattr(os, "pread", changed)
        assert WorkerLease.observe(store.files)["online"] is None


def test_readonly_owner_status_does_not_read_secret_or_dispatch_and_other_tokens_denied(
    managed, env, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Status must not execute, request, or resolve secrets")

    monkeypatch.setattr(env[1], "get", forbidden)
    monkeypatch.setattr("collection_context.processing.models.CloudModelClient._send", forbidden)
    before = hashes(env[0].files.root)
    assert post(managed, "overview", {}).json()["data"]["worker"]["online"] is False
    assert post(managed, "sources", {}).json()["data"]["worker_online"] is False
    assert hashes(env[0].files.root) == before
    with WorkerLease(env[0].files.root, allow_model_calls=False, allow_source_sync=True):
        a = ManagementService(env[0]).overview()["worker"]
        b = SourceManagement(env[0]).overview()["worker"]
        assert a["online"] is b["online"] is True
        assert a["capabilities"] == b["capabilities"] == {"model_calls": False, "source_sync": True}
        assert not env[0].snapshot()["jobs"]
    for token in (managed[3]["token"], managed[4]["token"]):
        assert (
            managed[0]
            .post("/v1/management/overview", json={}, headers={"Authorization": "Bearer " + token})
            .status_code
            == 403
        )


def test_real_process_pause_is_live_but_not_progress_and_death_is_offline(env):
    store = env[0]
    code = "from pathlib import Path; import sys; from collection_context.infrastructure.ownership import WorkerLease; w=WorkerLease(Path(sys.argv[1]),allow_model_calls=True,allow_source_sync=False); print('owned',flush=True); sys.stdin.read(1)"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(store.files.root)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline() == b"owned\n"
        value = WorkerLease.observe(store.files)
        assert value["online"] is True and value["progress_verified"] is False
        child.send_signal(__import__("signal").SIGSTOP)
        assert WorkerLease.observe(store.files)["online"] is True
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode == -9
        assert WorkerLease.observe(store.files)["online"] is False
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
