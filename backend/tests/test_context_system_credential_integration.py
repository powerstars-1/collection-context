"""Offline consumer regression for a structural OS credential backend.

The replacement vault is in test-process memory, never the actual Keychain. Its
SafeFiles directory contains no secret bodies. This proves consumer wiring, not
native Keychain encryption, dialogs, ACLs, persistence or any cloud capability.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest
from test_context_execution_runner import wait_for
from test_context_model_registry import prepared, requests

from collection_context.application import execution_runner as execution_module
from collection_context.application import launcher_capabilities as launcher_module
from collection_context.application.contracts import ContextError
from collection_context.application.execution_runner import ExecutionRunner
from collection_context.application.launcher_capabilities import LauncherCapabilities, launcher_resources
from collection_context.application.model_setup import ModelSetup
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow

ORIGINAL_KEYS = {role: "original-test-only-vault-value-" + role for role in ("audio", "vision", "summary")}


class MemorySystemSecrets:
    """Opaque references are scoped to an explicitly supplied private directory."""

    storage_kind = "macos_keychain"
    vaults: dict[Path, dict[str, str]] = {}
    opened: list[MemorySystemSecrets] = []
    reads: list[tuple[Path, str]] = []
    writes: list[tuple[Path, str]] = []
    failure: str | None = None

    def __init__(self, root: Path):
        if self.failure:
            raise ContextError(self.failure, "original offline system vault unavailable")
        self.files = SafeFiles(root)
        self.closed = False
        self.vaults.setdefault(root, {})
        self.opened.append(self)

    @classmethod
    def initialize(cls, root: Path):
        if cls.failure:
            raise ContextError(cls.failure, "original offline system vault unavailable")
        root.mkdir(parents=True, mode=0o700)
        return cls(root)

    @classmethod
    def open(cls, root: Path):
        return cls(root)

    def put(self, value: str) -> str:
        self.files.check_root()
        ref = "k_" + uuid.uuid4().hex
        self.vaults[self.files.root][ref] = value
        self.writes.append((self.files.root, ref))
        return ref

    def get(self, ref: str) -> str:
        self.files.check_root()
        if not isinstance(ref, str) or not ref.startswith("k_") or len(ref) != 34:
            raise ContextError("invalid_credential_reference", "invalid opaque reference")
        self.reads.append((self.files.root, ref))
        try:
            return self.vaults[self.files.root][ref]
        except KeyError:
            raise ContextError("credential_missing", "unknown opaque reference") from None

    def discard_new(self, ref: str) -> None:
        self.files.check_root()
        self.vaults[self.files.root].pop(ref, None)

    def close(self):
        self.files.close()
        self.closed = True

    def __repr__(self):
        return "MemorySystemSecrets(offline fixture)"


@pytest.fixture
def system_env(tmp_path, monkeypatch):
    MemorySystemSecrets.vaults = {}
    MemorySystemSecrets.opened = []
    MemorySystemSecrets.reads = []
    MemorySystemSecrets.writes = []
    MemorySystemSecrets.failure = None
    monkeypatch.setattr(launcher_module, "SystemSecrets", MemorySystemSecrets)
    monkeypatch.setattr(execution_module, "SystemSecrets", MemorySystemSecrets)
    workspace, credentials = tmp_path / "original-library", tmp_path / "original-vault-metadata"
    store = LibraryStore.initialize(workspace)
    try:
        yield store, credentials
    finally:
        for backend in MemorySystemSecrets.opened:
            if not backend.closed:
                backend.close()
        store.close()


def save(setup, role, *, model=None, api_key=None, expected=None):
    return setup.save(
        role=role,
        base_url="https://fixture.invalid/v1",
        model=model or "original-" + role,
        protocol="chat_audio" if role == "audio" else "chat",
        parameters={},
        timeout=30,
        api_key=ORIGINAL_KEYS[role] if api_key is None else api_key,
        expected_profile_id=expected,
        credential_confirmed=True,
    )


def config(store, credentials, **options):
    return LauncherCapabilities(
        store.files.root,
        allow_model_config=True,
        credential_dir=credentials,
        credential_backend="system",
        **options,
    )


def assert_no_keys_on_disk(root):
    for path in root.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert all(key.encode() not in body for key in ORIGINAL_KEYS.values())


def test_protocol_is_structural_and_configuration_three_roles_does_not_get_or_infer(system_env, monkeypatch):
    from collection_context.infrastructure.secrets import CredentialBackend

    store, credentials = system_env
    monkeypatch.setattr(ModelCatalog, "client", lambda *_a, **_k: pytest.fail("configuration cannot infer"))
    with launcher_resources(config(store, credentials)) as resources:
        assert isinstance(resources.model_secrets, CredentialBackend)
        assert resources.credential_storage == "macos_keychain"
        setup = ModelSetup(store, resources.model_secrets)
        for role in ORIGINAL_KEYS:
            result = save(setup, role)
            assert result["roles"][role]["configured"]
            assert result["credential_storage"] == "macos_keychain"
            assert result["model_requests"] == 0
            assert all(key not in json.dumps(result) for key in ORIGINAL_KEYS.values())
        assert len(MemorySystemSecrets.writes) == 3 and not MemorySystemSecrets.reads
        assert not list(credentials.iterdir()) and not store.snapshot()["jobs"]
        assert resources.worker_started is False
    assert MemorySystemSecrets.opened[0].closed
    assert_no_keys_on_disk(store.files.root.parent)


def test_desktop_defaults_system_and_explicit_legacy_config_stays_private_file(tmp_path, monkeypatch):
    root = tmp_path / "product"
    monkeypatch.setattr(launcher_module.diagnostics, "default_workspace", lambda: root / "workspace")
    desktop = launcher_module.default_desktop_capabilities(tmp_path / "library", allow_model_config=True)
    assert desktop.credential_backend == "system"
    assert desktop.credential_dir == root / "credentials-system"
    assert LauncherCapabilities(tmp_path / "library").credential_backend == "private-file"
    for bad in ("", "file", "keychain", 1, None):
        with pytest.raises(ContextError):
            LauncherCapabilities(tmp_path / "library", credential_backend=bad)


def test_system_setup_failure_never_falls_back_to_file(system_env, monkeypatch):
    store, credentials = system_env

    def forbidden(*_a, **_k):
        pytest.fail("system failure must never open private-file fallback")

    monkeypatch.setattr(launcher_module, "FileSecrets", forbidden)
    MemorySystemSecrets.failure = "system_credentials_unavailable"
    with pytest.raises(ContextError):
        with launcher_resources(config(store, credentials)):
            pytest.fail("failed system backend cannot yield handles")
    assert not credentials.exists() and not MemorySystemSecrets.reads


def test_legacy_private_files_still_report_unencrypted(system_env):
    store, credentials = system_env
    configuration = LauncherCapabilities(
        store.files.root, allow_model_config=True, credential_dir=credentials
    )
    with launcher_resources(configuration) as resources:
        assert isinstance(resources.model_secrets, FileSecrets)
        assert resources.credential_storage == "private_service_files_not_encrypted"
        assert (
            ModelSetup(store, resources.model_secrets).settings()["credential_storage"]
            == "private_service_files_not_encrypted"
        )
    assert not MemorySystemSecrets.opened


def test_system_bound_get_and_model_setup_both_reject_library_overlap(system_env):
    store, _ = system_env
    backend = MemorySystemSecrets.initialize(store.files.root / "nested-original-vault")
    for create in (lambda: ModelSetup(store, backend), lambda: ExtractionWorkflow(store, backend.get)):
        with pytest.raises(ContextError) as caught:
            create()
        assert caught.value.code == "secret_directory_overlap"
    assert not MemorySystemSecrets.reads and not MemorySystemSecrets.writes


def register_job(store, credentials):
    with launcher_resources(config(store, credentials)) as resources:
        setup = ModelSetup(store, resources.model_secrets)
        for role in ORIGINAL_KEYS:
            save(setup, role)
        _, identity = prepared(store)
        job = ExtractionWorkflow(store, resources.model_secrets.get).submit(
            identity, idempotency_key="original-pinned-system-job", max_calls=3
        )
    assert not MemorySystemSecrets.reads
    return job


def test_execution_requires_authorization_before_system_open_get_or_transport(system_env, monkeypatch):
    store, credentials = system_env
    register_job(store, credentials)
    sent = requests(monkeypatch)
    opened = len(MemorySystemSecrets.opened)
    runner = ExecutionRunner(store.files.root, credential_dir=credentials, credential_backend="system")
    try:
        with pytest.raises(ContextError):
            runner.start(allow_model_calls=True, execution_confirmed=False)
        assert len(MemorySystemSecrets.opened) == opened
        assert not MemorySystemSecrets.reads and not sent
    finally:
        runner.close()


def test_authorized_execution_reopens_same_system_backend_and_pinned_refs(system_env, monkeypatch):
    store, credentials = system_env
    job = register_job(store, credentials)
    pinned = job["payload"]["extraction"]["model_profiles"]
    pinned_refs = {ModelCatalog(store).get(identity)["credential_ref"] for identity in pinned.values()}
    # Rotate defaults after queuing; replay must retain the old opaque references.
    with launcher_resources(config(store, credentials)) as resources:
        setup = ModelSetup(store, resources.model_secrets)
        for role in ("vision", "summary"):
            old = setup.settings()["roles"][role]["profile_id"]
            save(setup, role, model="original-rotated-" + role, expected=old)
    sent = requests(monkeypatch)
    runner = ExecutionRunner(store.files.root, credential_dir=credentials, credential_backend="system")
    runner.start(allow_model_calls=True, execution_confirmed=True)
    try:
        status = wait_for(runner, lambda value: value["handled"] == 1 or value["state"] == "failed")
        assert status["state"] != "failed"
    finally:
        runner.close()
    assert len(sent) == 3
    assert {body["model"] for body in sent} == {"original-vision", "original-summary"}
    assert {ref for _, ref in MemorySystemSecrets.reads} == pinned_refs
    assert all(root == credentials for root, _ in MemorySystemSecrets.reads)
    assert store.snapshot()["jobs"][job["id"]]["state"] == "succeeded"
    assert all(backend.closed for backend in MemorySystemSecrets.opened)
    assert_no_keys_on_disk(store.files.root.parent)


def test_source_only_execution_never_opens_system_backend(system_env):
    store, credentials = system_env
    entered = threading.Event()

    class Operation:
        def serve(self, **kwargs):
            assert kwargs["allow_source_sync"] and not kwargs["allow_model_calls"]
            entered.set()
            kwargs["stop"].wait(3)
            return {"stopped": True}

    def operation(_store, secrets, source_factory, _runtime):
        assert secrets is None and source_factory is not None
        return Operation()

    runner = ExecutionRunner(
        store.files.root,
        credential_dir=credentials,
        browser_dir=credentials.parent / "original-profile",
        credential_backend="system",
        _operation_factory=operation,
    )
    runner.start(allow_source_sync=True, execution_confirmed=True)
    try:
        assert entered.wait(2)
    finally:
        runner.close()
    assert not MemorySystemSecrets.opened and not MemorySystemSecrets.reads
    assert not credentials.exists()


def test_system_execution_failure_never_falls_back_to_private_file(system_env, monkeypatch):
    store, credentials = system_env
    register_job(store, credentials)

    def forbidden(*_a, **_k):
        pytest.fail("system execution cannot fall back to private files")

    monkeypatch.setattr(execution_module, "FileSecrets", forbidden)
    sent = requests(monkeypatch)
    MemorySystemSecrets.failure = "system_credentials_unavailable"
    runner = ExecutionRunner(store.files.root, credential_dir=credentials, credential_backend="system")
    runner.start(allow_model_calls=True, execution_confirmed=True)
    try:
        status = wait_for(runner, lambda value: value["state"] == "failed")
        assert not status["active"]
    finally:
        runner.close()
    assert not sent and not MemorySystemSecrets.reads


@pytest.mark.parametrize("ref", ["/absolute/account", "../account", "k_unknown", "k_" + "a" * 32])
def test_unknown_opaque_reference_rejected_by_system_consumer_without_transport(system_env, monkeypatch, ref):
    store, credentials = system_env
    with launcher_resources(config(store, credentials)) as resources:
        backend = resources.model_secrets
        with pytest.raises(ContextError):
            backend.get(ref)
        _, identity = prepared(store)
        unknown = "k_" + "a" * 32
        for role in ("vision", "summary"):
            ModelCatalog(store).configure(
                role=role,
                base_url="https://fixture.invalid/v1",
                model="original-" + role,
                credential_ref=unknown,
            )
        job = ExtractionWorkflow(store, backend.get).submit(
            identity, idempotency_key="unknown-original-reference", max_calls=3
        )
        before = len(MemorySystemSecrets.reads)
        sent = requests(monkeypatch)
        with pytest.raises(ContextError) as caught:
            ExtractionWorkflow(store, backend.get).run(job["id"])
        assert caught.value.code == "credential_missing"
        assert len(MemorySystemSecrets.reads) == before + 1 and not sent
    assert_no_keys_on_disk(store.files.root.parent)


def test_unknown_reference_from_other_system_directory_not_resolved(system_env):
    store, credentials = system_env
    first = MemorySystemSecrets.initialize(credentials)
    foreign = MemorySystemSecrets.initialize(credentials.parent / "other-original-vault")
    reference = foreign.put(ORIGINAL_KEYS["vision"])
    with pytest.raises(ContextError) as caught:
        first.get(reference)
    assert caught.value.code == "credential_missing"
    assert_no_keys_on_disk(store.files.root.parent)
