"""Business integration over injected Windows DLLs, NOT Windows kernel acceptance.

LibraryStore, FileIndex, ReadGateway and JobManager are the real implementations.
Only the NT/DLL boundary is a deterministic original-data fixture; no model,
platform, private vault, or alternate in-memory business store is involved.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_context_windows_directory import DirectoryDLLs
from test_context_windows_native import code
from test_context_windows_ownership import LeaseDLLs

from collection_context.application.contracts import SCHEMA_VERSION, ContextError, canonical_bytes
from collection_context.application.gateway import ReadGateway
from collection_context.application.service import ContextService
from collection_context.infrastructure import storage
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.storage import PosixStorage, WindowsFileAccess, WindowsStorage
from collection_context.infrastructure.windows_native import FileIdentity
from collection_context.library.index import FileIndex
from collection_context.library.store import LEGACY_GUARD, WRITER_PROTOCOL, LibraryStore
from collection_context.workflows.jobs import JobManager


class StorageDLLs(LeaseDLLs, DirectoryDLLs):
    """Compose existing rooted publication, directory record and kernel-lock fixtures."""

    def __init__(self):
        super().__init__()
        # The shared security fixture reserves token HANDLE 777. Business flows
        # open many more files than individual SDK tests; never collide with it.
        self.next_handle = 10_000


class NewRootDLLs(StorageDLLs):
    """Resolve an absolute reopen to the same fixture node created relatively.

    PublicationDLLs normally auto-seeds arbitrary absolute roots. This one
    fixed new-root scenario instead reports its absence until native relative
    creation installs it; the production initializer and attachment checks run.
    """

    selected_root = Path("C:\\新父目录\\原创 新库")

    def name_key(self, parent, name):
        if parent is None:
            drive = self.device_target + "\\"
            current = self.names.get((None, drive.casefold()))
            if current is not None and name.startswith(drive) and name != drive:
                for component in name[len(drive) :].split("\\"):
                    key = int.from_bytes(current["id"], "little"), component.casefold()
                    current = self.names.get(key)
                    if current is None:
                        break
                else:
                    return key
        return super().name_key(parent, name)

    def open(self, output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size):
        request = ctypes.cast(attributes, ctypes.POINTER(native.ObjectAttributes)).contents
        string = request.ObjectName.contents
        name = ctypes.string_at(string.Buffer, string.Length).decode("utf-16-le")
        if (
            request.RootDirectory is None
            and name == self.device_target + str(self.selected_root)[2:]
            and self.name_key(None, name) not in self.names
        ):
            return -1073741772  # STATUS_OBJECT_NAME_NOT_FOUND, no synthetic root creation.
        return super().open(
            output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size
        )


ROOT = Path("C:\\原创 测试库")


def initial_store(backend, root=ROOT):
    """Seed a recognized fixture using production publication, not private user files."""
    workspace = "w_" + "a" * 32
    with backend.open_files(root) as files:
        files.write(LEGACY_GUARD, LibraryStore._guard_body(workspace))
        files.write(
            "context-workspace.json",
            canonical_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "workspace_id": workspace,
                    "vault_dir": "content-vault",
                    "created_at": "2026-10-02T00:00:00Z",
                    "writer_protocol": WRITER_PROTOCOL,
                }
            ),
        )
        LibraryStore._publish(
            files,
            {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": workspace,
                "generation": 0,
                "items": {},
                "jobs": {},
                "idempotency": {},
                "scopes": {},
                "settings": {"auto_sync": False, "auto_process": False},
            },
        )
    return LibraryStore(root, _storage=backend)


@pytest.fixture
def windows_store():
    dlls = StorageDLLs()
    backend = WindowsStorage(_native=dlls.native)
    library = initial_store(backend)
    try:
        yield library, backend, dlls
    finally:
        library.close()
        assert not any(dlls.held.values())
        assert {
            handle: dlls.closed.count(handle) for handle in dlls.files if dlls.closed.count(handle) != 1
        } == {}


def original(native="123456789"):
    return {
        "native_id": native,
        "title": "原创 页面排版教程",
        "body": "先确定布局，再核对留白。合成资料只用于本地测试。",
        "author": "原创夹具作者",
        "media_type": "video",
    }


def test_windows_store_roundtrip_transaction_and_read_gateway(windows_store):
    library, backend, dlls = windows_store
    assert library.storage is backend
    assert isinstance(library.files, WindowsFileAccess)
    assert isinstance(library.files.root, Path) and library.files.root == ROOT
    assert isinstance(library.files.identity, FileIdentity)
    assert not hasattr(library.files, "fd")
    item = library.upsert(original(), kind="saved", scope_id="s_saved")["item"]
    library.upsert(original(), kind="liked", scope_id="s_liked")
    snapshot = library.snapshot()
    assert snapshot["generation"] == 2 and len(snapshot["items"]) == 1
    assert len(snapshot["items"][item["id"]]["relations"]) == 2
    assert len(dlls.renames) > 2  # Both library and index genuinely published through NT rename.

    reopened = LibraryStore(ROOT, _storage=backend)
    try:
        assert reopened.snapshot() == snapshot
        before = len(dlls.writes)
        gateway = ReadGateway(ContextService(reopened))
        search = gateway.dispatch("search_collections", {"query": "排版"})
        assert search["ok"] and search["data"]["items"][0]["material_ref"] == item["id"]
        read = gateway.dispatch("read_collection", {"material_ref": item["id"], "artifact": "original"})
        assert read["ok"] and "先确定布局" in read["data"]["text"]
        status = gateway.dispatch("collection_status", {"material_ref": item["id"]})
        assert status["ok"] and status["data"]["artifacts"]["audio"]["state"] == "missing"
        assert len(dlls.writes) == before  # Reading never repairs/rebuilds or creates tasks.
        assert b"\xe5\x8e\x9f\xe5\x88\x9b" in reopened.files.read(FileIndex.readable_path(item["id"]))
    finally:
        reopened.close()


def test_windows_rejected_mutation_and_empty_recovery_do_not_publish(windows_store):
    library, backend, dlls = windows_store
    before = library.snapshot()

    def reject(state):
        state["settings"]["auto_sync"] = True
        raise ContextError("invalid_argument", "原创拒绝夹具")

    assert code(lambda: library.transact(reject)) == "invalid_argument"
    assert library.snapshot() == before
    with backend.executor(ROOT) as executor:
        publications = len(dlls.renames)
        assert JobManager(library).recover_interrupted(lease=executor) == []
        assert len(dlls.renames) == publications
    assert library.snapshot() == before


def test_windows_business_never_falls_back_to_pathname_io(windows_store, monkeypatch):
    library, _, _ = windows_store

    def forbidden(*args, **kwargs):
        pytest.fail("Windows business integration must stay behind owned native handles")

    with monkeypatch.context() as patch:
        for name in ("open", "mkdir", "read_bytes", "write_bytes", "unlink"):
            patch.setattr(Path, name, forbidden)
        item = library.upsert(original(), kind="saved", scope_id="s_nofallback")["item"]
        assert ContextService(library).read(item["id"])["state"] == "ready"


def test_windows_corrupted_commit_does_not_silently_rebuild(windows_store):
    library, _, dlls = windows_store
    library.files.write(".context/提交/CURRENT.json", b'{"version":"c_missing","sha256":"bad"}', replace=True)
    writes = len(dlls.writes)
    assert code(library.snapshot) == "not_found"
    assert len(dlls.writes) == writes


def test_windows_job_submission_is_idempotent_and_principal_scoped(windows_store):
    library, backend, _ = windows_store
    jobs = JobManager(library)
    job = jobs.submit("process", {"original_fixture": True}, idempotency_key="same", max_calls=1)
    assert (
        jobs.submit("process", {"original_fixture": True}, idempotency_key="same", max_calls=1)["id"]
        == job["id"]
    )
    assert code(lambda: jobs.submit("process", {}, idempotency_key="same")) == "idempotency_conflict"
    assert code(lambda: jobs.get(job["id"], principal="another_owner")) == "not_found"
    reopened = LibraryStore(ROOT, _storage=backend)
    try:
        assert JobManager(reopened).get(job["id"])["state"] == "queued"
        assert len(reopened.snapshot()["jobs"]) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("uncertain", [False, True], ids=["local-stage", "unresolved-intent"])
def test_windows_interrupted_stage_recovery_uses_real_job_logic(windows_store, uncertain):
    library, backend, dlls = windows_store
    jobs = JobManager(library)
    job = jobs.submit("process", {}, idempotency_key="interrupted", max_calls=1)
    jobs.start(job["id"])
    if uncertain:
        jobs.begin_call(
            job["id"], stage="audio", input_hash="original-hash", processor_version="v1", start_stage=True
        )
    else:
        jobs.set_stage(job["id"], "prepare", state_name="running", input_hash="original-hash")
    reopened = LibraryStore(ROOT, _storage=backend)
    try:
        recovered_jobs = JobManager(reopened)
        with backend.executor(ROOT) as executor:
            assert executor.files.root == reopened.files.root
            assert executor.files.identity == reopened.files.identity
            result = recovered_jobs.recover_interrupted(lease=executor)
        assert len(result) == 1
        recovered = recovered_jobs.get(job["id"])
        assert recovered["state"] == ("blocked" if uncertain else "queued")
        stage = recovered["stages"]["audio" if uncertain else "prepare"]
        assert stage["state"] == ("blocked" if uncertain else "interrupted")
        if uncertain:
            assert recovered["error"]["code"] == "upstream_outcome_unknown"
            assert recovered["error"]["possibly_charged"] is True
            assert recovered["calls"][0]["state"] == "intent"
            assert code(lambda: recovered_jobs.start(job["id"])) == "job_not_queued"
        else:
            assert recovered["calls"] == []
        with backend.executor(ROOT) as executor:
            publications = len(dlls.renames)
            assert recovered_jobs.recover_interrupted(lease=executor) == []
            assert len(dlls.renames) == publications
    finally:
        reopened.close()


def test_windows_completed_call_reuse_and_budget_remain_durable(windows_store):
    library, _, _ = windows_store
    jobs = JobManager(library)
    job = jobs.submit("process", {}, idempotency_key="confirmed", max_calls=1)
    jobs.start(job["id"])
    call = jobs.begin_call(job["id"], stage="audio", input_hash="hash", processor_version="v1")
    jobs.finish_call(
        job["id"], call["id"], outcome="completed", actual_model="original-fixture", usage={"total_tokens": 3}
    )
    same = jobs.begin_call(job["id"], stage="audio", input_hash="hash", processor_version="v1")
    assert same["id"] == call["id"] and same["reused"] is True
    assert (
        code(lambda: jobs.begin_call(job["id"], stage="screen", input_hash="hash", processor_version="v1"))
        == "budget_required"
    )
    assert len(jobs.get(job["id"])["calls"]) == 1


@pytest.mark.parametrize("role", ["writer", "executor"])
def test_windows_same_role_exclusion_and_path_identity(windows_store, role):
    library, backend, dlls = windows_store
    acquire = getattr(backend, role)
    with acquire(ROOT) as first:
        assert first.files.root == library.files.root
        assert first.files.identity == library.files.identity
        writes = len(dlls.writes)
        assert code(lambda: acquire(ROOT)) == role + "_busy"
        assert len(dlls.writes) == writes
        first.check()
    assert code(first.check) == role + "_not_owned"
    with acquire(ROOT) as second:
        second.check()


def test_windows_worker_snapshot_does_not_create_or_write_and_excludes_competitor(windows_store):
    library, backend, dlls = windows_store
    writes = len(dlls.writes)
    assert backend.observe_worker(library.files)["online"] is False
    assert len(dlls.writes) == writes
    with backend.worker(ROOT, model_calls=False, source_sync=True) as worker:
        writes = len(dlls.writes)
        result = backend.observe_worker(library.files)
        assert result["online"] is True and result["progress_verified"] is False
        assert result["capabilities"] == {"model_calls": False, "source_sync": True}
        assert len(dlls.writes) == writes
        assert code(lambda: backend.worker(ROOT, model_calls=True, source_sync=True)) == "worker_busy"
        worker.check()
    assert backend.observe_worker(library.files)["online"] is False


def test_windows_recovery_rejects_wrong_library_and_closed_executor(windows_store):
    library, backend, _ = windows_store
    before = library.snapshot()
    with backend.executor(Path("C:\\另一个原创库")) as other:
        assert code(lambda: JobManager(library).recover_interrupted(lease=other)) == "executor_not_owned"
    executor = backend.executor(ROOT)
    executor.close()
    assert code(lambda: JobManager(library).recover_interrupted(lease=executor)) == "executor_not_owned"
    assert library.snapshot() == before


def test_windows_stream_contract_retains_hash_size_and_closes(windows_store):
    library, _, _ = windows_store
    body = ("原创流式数据 \n" * 9000).encode()
    digest = hashlib.sha256(body).hexdigest()
    library.files.write_chunks(
        "附件/原创.txt",
        (body[offset : offset + 65_536] for offset in range(0, len(body), 65_536)),
        expected_size=len(body),
        expected_sha256=digest,
    )
    with library.files.read_chunks("附件/原创.txt", max_bytes=len(body), private=True) as chunks:
        assert b"".join(chunks) == body
    assert library.files.file_size("附件/原创.txt") == len(body)
    assert library.files.list_directory("附件") == [{"name": "原创.txt", "kind": "file"}]


def test_windows_business_failure_after_root_replacement_has_no_new_write(windows_store):
    library, _, dlls = windows_store
    root_key = dlls.name_key(None, dlls.device_target + str(ROOT)[2:])
    dlls.names[root_key] = dict(dlls.names[root_key], id=b"r" * 16)
    writes = len(dlls.writes)
    assert code(library.snapshot) == "storage_unavailable"
    assert (
        code(lambda: JobManager(library).submit("process", {}, idempotency_key="no-write"))
        == "storage_unavailable"
    )
    assert len(dlls.writes) == writes


def test_windows_root_replaced_between_store_check_and_lease_has_no_metadata_write(
    windows_store, monkeypatch
):
    library, backend, dlls = windows_store
    acquire = backend.writer
    identity = library.files.identity
    calls = []

    def replace_before_acquire(root, *, expected_identity=None):
        calls.append(expected_identity)
        assert expected_identity == identity
        root_key = dlls.name_key(None, dlls.device_target + str(root)[2:])
        dlls.names[root_key] = dict(dlls.names[root_key], id=b"r" * 16)
        return acquire(root, expected_identity=expected_identity)

    monkeypatch.setattr(backend, "writer", replace_before_acquire)
    writes, publications = len(dlls.writes), len(dlls.renames)
    assert (
        code(lambda: JobManager(library).submit("process", {}, idempotency_key="race"))
        == "storage_unavailable"
    )
    assert calls == [identity]
    assert len(dlls.writes) == writes and len(dlls.renames) == publications


def test_windows_existing_empty_initializer_and_populated_refusal():
    dlls = StorageDLLs()
    backend = WindowsStorage(_native=dlls.native)
    library = LibraryStore.initialize(ROOT, _storage=backend)
    try:
        assert library.snapshot()["generation"] == 0
        assert ContextService(library).search("原创")["items"] == []
        before = library.snapshot()
        writes = len(dlls.writes)
        assert code(lambda: LibraryStore.initialize(ROOT, _storage=backend)) == "workspace_not_empty"
        assert len(dlls.writes) == writes and library.snapshot() == before
    finally:
        library.close()
    assert all(dlls.closed.count(handle) == 1 for handle in dlls.files)


def test_windows_new_root_initialization_reopens_same_native_identity(monkeypatch):
    dlls = NewRootDLLs()
    backend = WindowsStorage(_native=dlls.native)

    def no_path_creation(*args, **kwargs):
        pytest.fail("New Windows roots require handle-relative creation and private DACL")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", no_path_creation)
        patch.setattr(os, "mkdir", no_path_creation)
        library = LibraryStore.initialize(dlls.selected_root, _storage=backend)
        try:
            assert library.files.root == dlls.selected_root
            assert library.snapshot()["generation"] == 0
            item = library.upsert(original(), kind="saved", scope_id="s_initialized")["item"]
            assert ContextService(library).search("排版")["items"][0]["material_ref"] == item["id"]
            with backend.writer(dlls.selected_root) as writer:
                assert writer.files.identity == library.files.identity
            created = [request for request in dlls.opened if request["disposition"] == 2]
            assert [request["name"] for request in created[:2]] == ["新父目录", "原创 新库"]
            assert all(request["security"] for request in created)
        finally:
            library.close()
    assert not any(dlls.held.values())
    assert all(dlls.closed.count(handle) == 1 for handle in dlls.files)


def test_backend_mismatch_rejected_before_observation(windows_store, tmp_path):
    library, backend, dlls = windows_store
    other = StorageDLLs()
    with WindowsStorage(_native=other.native).open_files(ROOT) as foreign:
        writes = len(dlls.writes)
        assert code(lambda: backend.observe_worker(foreign)) == "invalid_workspace"
        assert len(dlls.writes) == writes
    assert code(lambda: PosixStorage().observe_worker(library.files)) == "invalid_workspace"
    if os.name == "posix":
        with SafeFiles(tmp_path) as real:
            assert code(lambda: backend.observe_worker(real)) == "invalid_workspace"
    assert all(other.closed.count(handle) == 1 for handle in other.files)


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX path, not a Windows acceptance claim")
def test_default_real_posix_store_index_jobs_and_worker(tmp_path):
    root = tmp_path / "原创 空格库"
    library = LibraryStore.initialize(root)
    try:
        assert isinstance(library.storage, PosixStorage) and isinstance(library.files, SafeFiles)
        item = library.upsert(original(), kind="saved", scope_id="s_test")["item"]
        assert ContextService(library).search("排版")["items"][0]["material_ref"] == item["id"]
        with library.storage.worker(root, model_calls=False, source_sync=False):
            assert library.storage.observe_worker(library.files)["online"] is True
        jobs = JobManager(library)
        job = jobs.submit("process", {}, idempotency_key="posix")
        jobs.start(job["id"])
        with library.storage.executor(root) as executor:
            assert jobs.recover_interrupted(lease=executor)[0]["state"] == "queued"
        assert library.storage.observe_worker(library.files)["online"] is False
    finally:
        library.close()
    assert (root / "context-workspace.json").is_file()


@pytest.mark.parametrize("backend_name", [None, "windows_native_unaccepted", "unrecognized"])
def test_default_selector_refuses_unaccepted_backend_without_creating_root(
    monkeypatch, tmp_path, backend_name
):
    monkeypatch.setattr(
        storage, "require_safe_files_runtime", lambda: SimpleNamespace(safe_files_backend=backend_name)
    )
    root = tmp_path / "must-not-exist"
    assert code(lambda: LibraryStore.initialize(root)) == "unsupported_platform"
    assert not root.exists()


def test_default_selector_preserves_fail_closed_probe(monkeypatch, tmp_path):
    def unavailable():
        raise ContextError("unsupported_platform", "原创未支持平台夹具")

    monkeypatch.setattr(storage, "require_safe_files_runtime", unavailable)
    assert code(lambda: LibraryStore(tmp_path / "absent")) == "unsupported_platform"
    assert list(tmp_path.iterdir()) == []
