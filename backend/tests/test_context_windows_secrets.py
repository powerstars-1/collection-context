"""Real unified metadata/item/SDK composition with injected DLLs, NOT Windows acceptance."""

from __future__ import annotations

import ctypes
import json
import socket
from pathlib import Path

import pytest
from test_context_storage import StorageDLLs
from test_context_windows_deletion import DeletionDLLs
from test_context_windows_system_secrets import SDK, wide_at

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure import windows_system_secrets as primitive
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.infrastructure.storage import PosixStorage, WindowsFileAccess, WindowsStorage
from collection_context.infrastructure.system_secrets import MANIFEST, SystemSecrets
from collection_context.infrastructure.windows_credential_items import WindowsCredentialItems
from collection_context.infrastructure.windows_native import ObjectAttributes
from collection_context.infrastructure.windows_secrets import WindowsSystemSecrets

ROOT = Path("C:\\原创凭据\\系统元数据")
VALUE = "original-offline-windows-credential-原创🙂"


class InitializingDLLs(StorageDLLs, DeletionDLLs):
    """Bind fixture relative-created nodes to absolute reopen of the same path.

    Existing DLL fixtures model only relative or only absolute namespaces. This
    mapping supplies OS-like identity continuity without altering product guards.
    """

    def __init__(self):
        self.paths, self.absolute_keys = {}, {}
        super().__init__()

    def name_key(self, parent, name):
        if parent is None:
            return self.absolute_keys.get(name.casefold().rstrip("\\"), super().name_key(parent, name))
        return super().name_key(parent, name)

    def open(self, output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size):
        request = ctypes.cast(attributes, ctypes.POINTER(ObjectAttributes)).contents
        name = ctypes.string_at(
            request.ObjectName.contents.Buffer, request.ObjectName.contents.Length
        ).decode("utf-16-le")
        key = self.name_key(request.RootDirectory, name)
        path = (
            name
            if request.RootDirectory is None
            else self.paths[self.files[request.RootDirectory]["id"]].rstrip("\\") + "\\" + name
        )
        result = super().open(
            output, access, attributes, io, allocation, attrs, shares, disposition, options, ea, size
        )
        if result == 0:
            value = ctypes.cast(output, ctypes.POINTER(primitive.PTR)).contents.value
            self.paths[self.files[value]["id"]] = path
            self.absolute_keys[path.casefold().rstrip("\\")] = key
        return result


class StatefulSDK(SDK):
    """Original OS table fixture; real consumer/item/ctypes code still runs."""

    def __init__(self, dlls):
        super().__init__()
        self.dlls, self.items = dlls, {}

    def write(self, pointer, flags):
        assert any(self.dlls.held.values())
        result = super().write(pointer, flags)
        if result:
            entry = ctypes.cast(pointer, ctypes.POINTER(primitive.CredentialW)).contents
            target = wide_at(entry.TargetName)
            self.items[target] = (
                wide_at(entry.UserName),
                ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize),
            )
        return result

    def read(self, name, kind, flags, output):
        assert any(self.dlls.held.values())
        self.target = wide_at(name)
        if self.target not in self.items:
            self.calls.append(("get", self.target, kind, flags))
            self.last_error = 1168
            return 0
        self.account, self.body = self.items[self.target]
        return super().read(name, kind, flags, output)

    def delete(self, name, kind, flags):
        assert any(self.dlls.held.values())
        result = super().delete(name, kind, flags)
        if result:
            del self.items[wide_at(name)]
        return result


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Windows credential composition cannot contact network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def setup(monkeypatch):
    dlls = InitializingDLLs()
    storage = WindowsStorage(_native=dlls.native)
    sdk = StatefulSDK(dlls)
    manager = primitive.WindowsCredentialManager(_api=primitive._NativeAPI(sdk.dll, lambda: sdk.last_error))
    created = []

    def factory(root, namespace, *, _native):
        assert _native is dlls.native
        with storage.open_files(Path(root)) as files:
            manifest = json.loads(files.read(MANIFEST))
            assert manifest["namespace"] == namespace
            assert manifest["backend"] == "windows_credential_manager"
        item = WindowsCredentialItems(root, namespace, _manager=manager, _native=dlls.native)
        created.append(item)
        return item

    monkeypatch.setattr(
        "collection_context.infrastructure.windows_credential_items.WindowsCredentialItems", factory
    )
    backend = WindowsSystemSecrets.initialize(ROOT, _storage=storage)
    try:
        yield backend, storage, dlls, sdk, created
    finally:
        backend.close()
        manager.close()
        assert not any(dlls.held.values())
        assert all(handle in dlls.closed for handle in dlls.files)


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code and VALUE not in str(caught.value)
    assert caught.value.__cause__ is None


def test_initialization_is_handle_private_key_free_and_no_credential_queries(setup):
    backend, storage, dlls, sdk, created = setup
    assert isinstance(backend, CredentialBackend) and isinstance(backend.files, WindowsFileAccess)
    assert backend.storage is storage and backend.storage_kind == "windows_credential_manager"
    assert not sdk.calls and not dlls.locks and len(created) == 1
    manifest = json.loads(backend.files.read(MANIFEST, private=True))
    assert manifest == {
        "schema_version": 1,
        "backend": "windows_credential_manager",
        "namespace": manifest["namespace"],
    }
    assert len(manifest["namespace"]) == 32
    assert all(entry["security"] for entry in dlls.opened if entry["disposition"] == 2)
    assert VALUE not in repr(backend)


def test_end_to_end_put_reopen_get_and_rollback_use_same_metadata_protocol(setup):
    backend, storage, dlls, sdk, created = setup
    ref = backend.put(VALUE)
    manifest = json.loads(backend.files.read(MANIFEST))
    metadata = json.loads(backend.files.read(ref, private=True))
    assert metadata == {**manifest, "confirmed": True, "ref": ref}
    target = "org.collection-context.credentials." + manifest["namespace"] + "/" + ref
    assert sdk.items == {target: (ref, VALUE.encode())}
    reopened = WindowsSystemSecrets(ROOT, _storage=storage)
    try:
        assert reopened.get(ref) == VALUE
        failure("system_secret_rollback_forbidden", lambda: reopened.discard_new(ref))
    finally:
        reopened.close()
    assert created[-1]._closed
    backend.discard_new(ref)
    assert not sdk.items and not backend.files.entry_exists(ref)
    assert len(sdk.blocks) == len(sdk.freed)
    for file in dlls.files.values():
        assert VALUE.encode() not in file.get("body", b"")


def test_get_consumes_fresh_rollback_authority(setup):
    backend, _, _, sdk, _ = setup
    ref = backend.put(VALUE)
    assert backend.get(ref) == VALUE
    failure("system_secret_rollback_forbidden", lambda: backend.discard_new(ref))
    assert len(sdk.items) == 1


@pytest.mark.parametrize("identity", ["macos_keychain", "private_service_files_not_encrypted", "other"])
def test_mismatched_manifest_refuses_before_item_factory_or_credential_access(setup, identity):
    backend, storage, _, sdk, created = setup
    manifest = json.loads(backend.files.read(MANIFEST))
    backend.files.write(MANIFEST, canonical_bytes({**manifest, "backend": identity}), replace=True)
    failure("system_secret_directory_invalid", lambda: WindowsSystemSecrets(ROOT, _storage=storage))
    assert len(created) == 1 and not sdk.calls


def test_windows_system_directory_cannot_be_opened_by_file_or_mac_backend(setup):
    backend, storage, _, sdk, _ = setup
    failure("credential_backend_mismatch", lambda: FileSecrets(ROOT, _storage=storage))

    def forbidden(root, namespace):
        pytest.fail("Backend mismatch must reject before item factory")

    failure(
        "system_secret_directory_invalid",
        lambda: SystemSecrets(ROOT, _storage=storage, _items_factory=forbidden),
    )
    assert not sdk.calls and backend.files.entry_exists(MANIFEST)


def test_changed_namespace_and_bad_reference_block_before_cred_query(setup):
    backend, _, _, sdk, _ = setup
    ref = backend.put(VALUE)
    calls = len(sdk.calls)
    data = json.loads(backend.files.read(ref))
    backend.files.write(ref, canonical_bytes({**data, "namespace": "f" * 32}), replace=True)
    failure("system_secret_metadata_invalid", lambda: backend.get(ref))
    failure("system_secret_metadata_invalid", lambda: backend.discard_new(ref))
    backend.files.write(MANIFEST, canonical_bytes({**data, "namespace": "e" * 32}), replace=True)
    failure("system_secret_directory_changed", lambda: backend.get(ref))
    assert len(sdk.calls) == calls and len(sdk.items) == 1


def test_default_gate_and_wrong_storage_do_not_create_or_query(tmp_path, monkeypatch):
    monkeypatch.setattr("collection_context.infrastructure.windows_secrets.sys.platform", "darwin")
    for method in (WindowsSystemSecrets, WindowsSystemSecrets.initialize):
        failure("system_secret_unsupported", lambda: method(tmp_path / "not-created"))
        failure(
            "system_secret_unsupported", lambda: method(tmp_path / "not-created", _storage=PosixStorage())
        )
    assert not list(tmp_path.iterdir())


def test_initialize_does_not_use_pathname_mkdir_or_adopt_existing_root(setup, monkeypatch):
    _, storage, _, sdk, _ = setup

    def forbidden(*args, **kwargs):
        pytest.fail("Windows private root must use native anchored creation")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    fresh = WindowsSystemSecrets.initialize(Path("C:\\原创凭据\\额外测试元数据"), _storage=storage)
    fresh.close()
    failure("secret_directory_exists", lambda: WindowsSystemSecrets.initialize(ROOT, _storage=storage))
    assert not sdk.calls


def test_uncertain_system_create_never_publishes_ref_or_removes_possible_item(setup):
    backend, _, _, sdk, _ = setup
    sdk.raise_at = "get"
    sdk.raise_after_allocation = False
    failure("system_secret_outcome_unknown", lambda: backend.put(VALUE))
    assert len(sdk.items) == 1 and not backend._new
    assert not any(call[0] == "delete" for call in sdk.calls)


def test_metadata_publication_failure_preserves_system_item_without_rollback(setup, monkeypatch):
    backend, _, _, sdk, _ = setup
    original = backend.files.write

    def fail(name, payload, **kwargs):
        if name.startswith("k_"):
            raise RuntimeError(VALUE)
        return original(name, payload, **kwargs)

    monkeypatch.setattr(backend.files, "write", fail)
    failure("system_secret_outcome_unknown", lambda: backend.put(VALUE))
    assert len(sdk.items) == 1 and not backend._new
    assert not any(call[0] == "delete" for call in sdk.calls)


def test_factory_attaching_changed_manifest_releases_new_owned_item_handles(setup):
    backend, storage, _, sdk, _ = setup
    owned = []

    def factory(root, namespace):
        item = WindowsCredentialItems(str(root), namespace, _manager=object(), _native=storage._native)
        owned.append(item)
        backend.files.write(MANIFEST, canonical_bytes({"changed": True}), replace=True)
        return item

    failure(
        "credential_unavailable",
        lambda: WindowsSystemSecrets(ROOT, _storage=storage, _items_factory=factory),
    )
    assert len(owned) == 1 and owned[0]._closed and not sdk.calls
