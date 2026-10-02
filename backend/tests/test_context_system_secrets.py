"""Exact offline system-item fixtures; never access the real macOS keychain."""

from __future__ import annotations

import json
import os
import types
import uuid

import pytest

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.infrastructure.system_secrets import MANIFEST, SystemSecrets

PRIVATE = "中文 私密模型凭据不回显"


class Items:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail = {}

    def create(self, service, account, value):
        self.calls.append(("create", service, account))
        if "create" in self.fail:
            raise self.fail["create"]
        if (service, account) in self.values:
            raise ContextError("credential_conflict", PRIVATE)
        self.values[service, account] = value

    def read(self, service, account):
        self.calls.append(("read", service, account))
        if "read" in self.fail:
            raise self.fail["read"]
        if (service, account) not in self.values:
            raise ContextError("credential_missing", PRIVATE)
        return self.values[service, account]

    def delete(self, service, account):
        self.calls.append(("delete", service, account))
        if "delete" in self.fail:
            raise self.fail["delete"]
        if (service, account) not in self.values:
            raise ContextError("credential_missing", PRIVATE)
        del self.values[service, account]


@pytest.fixture
def setup(tmp_path):
    items = Items()
    backend = SystemSecrets.initialize(tmp_path / "系统凭据 元数据", _backend=items)
    try:
        yield backend, items
    finally:
        backend.close()


def no_private(error):
    assert PRIVATE not in str(error) and PRIVATE not in json.dumps(error.as_dict(), ensure_ascii=False)


def test_protocol_preserves_existing_file_behavior_and_explicit_storage_kind(tmp_path):
    backend = FileSecrets.initialize(tmp_path / "service")
    try:
        assert isinstance(backend, CredentialBackend)
        assert backend.storage_kind == "private_service_files_not_encrypted"
        ref = backend.put(PRIVATE)
        assert backend.get(ref) == PRIVATE
        backend.discard_new(ref)
        with pytest.raises(ContextError) as caught:
            backend.get(ref)
        assert caught.value.code == "credential_missing"
    finally:
        backend.close()


def test_initialization_and_constructor_query_no_system_items_and_metadata_is_private(setup):
    backend, items = setup
    root = backend.files.root
    assert not items.calls and isinstance(backend, CredentialBackend)
    assert backend.storage_kind == "macos_keychain"
    assert root.stat().st_mode & 0o077 == 0
    assert (root / MANIFEST).stat().st_mode & 0o077 == 0
    manifest = json.loads((root / MANIFEST).read_text())
    assert set(manifest) == {"schema_version", "backend", "namespace"}
    assert manifest["schema_version"] == 1 and manifest["backend"] == "macos_keychain"
    assert len(manifest["namespace"]) == 32
    other = SystemSecrets(root, _backend=items)
    other.close()
    assert not items.calls


@pytest.mark.parametrize("manifest_kind", ["valid", "corrupt", "broken-link", "directory", "hardlink"])
def test_file_backend_refuses_system_identity_even_if_manifest_cannot_be_read(setup, tmp_path, manifest_kind):
    backend, items = setup
    ref = backend.put(PRIVATE)
    path = backend.files.root / MANIFEST
    if manifest_kind == "corrupt":
        backend.files.write(MANIFEST, b"invalid", replace=True)
    elif manifest_kind == "broken-link":
        path.unlink()
        path.symlink_to(tmp_path / "missing-target")
    elif manifest_kind == "directory":
        path.unlink()
        path.mkdir()
    elif manifest_kind == "hardlink":
        os.link(path, tmp_path / "other-name")
    with pytest.raises(ContextError) as caught:
        FileSecrets(backend.files.root)
    assert caught.value.code == "credential_backend_mismatch"
    assert len(items.calls) == len(items.values) == 1
    assert (backend.files.root / ref).exists()


def test_already_open_file_backend_rechecks_identity_before_reading_or_writing(tmp_path):
    backend = FileSecrets.initialize(tmp_path / "ordinary")
    try:
        ref = backend.put(PRIVATE)
        backend.files.write(MANIFEST, b"invalid-system-marker")
        for operation in (
            lambda: backend.get(ref),
            lambda: backend.put(PRIVATE),
            lambda: backend.discard_new(ref),
        ):
            with pytest.raises(ContextError) as caught:
                operation()
            assert caught.value.code == "credential_backend_mismatch"
        assert (backend.files.root / ref).read_text() == PRIVATE
    finally:
        backend.close()


def test_chinese_key_exact_namespace_and_ref_cross_instance_read_no_disk_key(setup, capsys):
    backend, items = setup
    ref = backend.put(PRIVATE)
    assert len(ref) == 34 and ref.startswith("k_")
    metadata = json.loads(backend.files.read(ref, private=True))
    assert metadata == {**json.loads(backend.files.read(MANIFEST)), "ref": ref, "confirmed": True}
    service = "org.collection-context.credentials." + metadata["namespace"]
    assert items.calls == [("create", service, ref)]
    assert PRIVATE not in repr(backend)
    for path in backend.files.root.iterdir():
        assert PRIVATE.encode() not in path.read_bytes()
    other = SystemSecrets(backend.files.root, _backend=items)
    try:
        assert other.get(ref) == PRIVATE
        assert items.calls[-1] == ("read", service, ref)
        with pytest.raises(ContextError) as caught:
            other.discard_new(ref)
        assert caught.value.code == "system_secret_rollback_forbidden"
    finally:
        other.close()
    assert len(items.values) == 1 and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_unsupported_platform_does_not_fallback_or_create_directory(tmp_path, monkeypatch, platform):
    monkeypatch.setattr("collection_context.infrastructure.system_secrets.sys.platform", platform)
    for operation in (SystemSecrets, SystemSecrets.initialize):
        with pytest.raises(ContextError) as caught:
            operation(tmp_path / "no-fallback")
        assert caught.value.code == "system_secret_unsupported"
    assert not list(tmp_path.iterdir())


def test_default_mac_constructor_selects_sdk_but_never_queries(tmp_path, monkeypatch):
    items = Items()
    backend = SystemSecrets.initialize(tmp_path / "metadata", _backend=items)
    backend.close()
    monkeypatch.setattr("collection_context.infrastructure.system_secrets.sys.platform", "darwin")
    monkeypatch.setitem(
        __import__("sys").modules,
        "collection_context.infrastructure.macos_credentials",
        types.SimpleNamespace(MacOSKeychain=lambda: items),
    )
    other = SystemSecrets(tmp_path / "metadata")
    other.close()
    assert not items.calls


def test_owned_factory_runs_after_identity_validation_and_closes_once(tmp_path):
    class Owned(Items):
        closed = 0

        def close(self):
            self.closed += 1

    items, calls = Owned(), []

    def factory(root, namespace):
        manifest = json.loads((root / MANIFEST).read_bytes())
        assert manifest["namespace"] == namespace and manifest["backend"] == "macos_keychain"
        calls.append((root, namespace))
        assert not items.calls
        return items

    backend = SystemSecrets.initialize(tmp_path / "owned", _items_factory=factory)
    assert len(calls) == 1 and not items.calls
    backend.close()
    backend.close()
    assert items.closed == 1


def test_borrowed_backend_is_not_closed(tmp_path):
    class Borrowed(Items):
        def close(self):
            pytest.fail("Borrowed items must not be closed")

    backend = SystemSecrets.initialize(tmp_path / "borrowed", _backend=Borrowed())
    backend.close()


def test_invalid_manifest_does_not_instantiate_owned_factory(setup):
    backend, _ = setup
    backend.files.write(MANIFEST, canonical_bytes({"schema_version": 1}), replace=True)

    def forbidden(root, namespace):
        pytest.fail("Invalid identity cannot bind an item service")

    with pytest.raises(ContextError):
        SystemSecrets(backend.files.root, _items_factory=forbidden)


def test_owned_factory_failure_is_sanitized_and_preserves_initialized_root(tmp_path):
    def fail(root, namespace):
        raise RuntimeError(PRIVATE)

    root = tmp_path / "owned-failure"
    with pytest.raises(ContextError) as caught:
        SystemSecrets.initialize(root, _items_factory=fail)
    assert caught.value.code == "credential_unavailable"
    no_private(caught.value)
    assert [path.name for path in root.iterdir()] == [MANIFEST]


def test_owned_items_closed_if_manifest_changes_during_factory(tmp_path):
    class Owned(Items):
        closed = 0

        def close(self):
            self.closed += 1

    items = Owned()

    def factory(root, namespace):
        from collection_context.infrastructure.files import SafeFiles

        with SafeFiles(root) as files:
            files.write(MANIFEST, canonical_bytes({"schema_version": 1}), replace=True)
        return items

    with pytest.raises(ContextError) as caught:
        SystemSecrets.initialize(tmp_path / "changed", _items_factory=factory)
    assert caught.value.code == "credential_unavailable" and items.closed == 1 and not items.calls


def test_owned_close_failure_still_closes_files_once_and_does_not_echo(tmp_path, monkeypatch):
    class Owned(Items):
        closed = 0

        def close(self):
            self.closed += 1
            raise RuntimeError(PRIVATE)

    items, closed = Owned(), []
    backend = SystemSecrets.initialize(
        tmp_path / "failed-close", _items_factory=lambda root, namespace: items
    )
    original = backend.files.close

    def close_files():
        closed.append(True)
        original()

    monkeypatch.setattr(backend.files, "close", close_files)
    with pytest.raises(ContextError) as caught:
        backend.close()
    no_private(caught.value)
    assert caught.value.code == "credential_unavailable"
    backend.close()
    assert items.closed == 1 and closed == [True]


def test_invalid_factory_result_cannot_claim_initialization_success(tmp_path):
    with pytest.raises(ContextError) as caught:
        SystemSecrets.initialize(tmp_path / "invalid-factory", _items_factory=lambda root, namespace: None)
    assert caught.value.code == "credential_unavailable"
    assert [path.name for path in (tmp_path / "invalid-factory").iterdir()] == [MANIFEST]


@pytest.mark.parametrize("kind", ["file-secrets", "unknown"])
def test_existing_old_or_unknown_directories_are_not_imported_or_migrated(tmp_path, kind):
    root, items = tmp_path / "existing", Items()
    if kind == "file-secrets":
        old = FileSecrets.initialize(root)
        old.put(PRIVATE)
        old.close()
    else:
        root.mkdir(mode=0o700)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(ContextError) as caught:
        SystemSecrets(root, _backend=items)
    assert caught.value.code == "system_secret_directory_invalid"
    with pytest.raises(ContextError) as caught:
        SystemSecrets.initialize(root, _backend=items)
    assert caught.value.code == "secret_directory_exists"
    assert before == {path.name: path.read_bytes() for path in root.iterdir()} and not items.calls


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"schema_version": True, "backend": "macos_keychain", "namespace": "a" * 32},
        {"schema_version": 2, "backend": "macos_keychain", "namespace": "a" * 32},
        {"schema_version": 1, "backend": "files", "namespace": "a" * 32},
        {"schema_version": 1, "backend": "macos_keychain", "namespace": "../outside"},
        {"schema_version": 1, "backend": "macos_keychain", "namespace": "A" * 32},
        {"schema_version": 1, "backend": "macos_keychain", "namespace": "a" * 32, "key": PRIVATE},
    ],
)
def test_invalid_manifest_refuses_before_system_query(tmp_path, manifest):
    root, items = tmp_path / "invalid", Items()
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    from collection_context.infrastructure.files import SafeFiles

    with SafeFiles(root) as files:
        files.write(MANIFEST, canonical_bytes(manifest))
    with pytest.raises(ContextError) as caught:
        SystemSecrets(root, _backend=items)
    no_private(caught.value)
    assert not items.calls


@pytest.mark.parametrize(
    "mutation", ["namespace", "schema-bool", "duplicate", "corrupt", "hardlink", "symlink"]
)
def test_manifest_corruption_or_replacement_never_selects_another_system_namespace(setup, tmp_path, mutation):
    backend, items = setup
    ref = backend.put(PRIVATE)
    before = list(items.calls)
    path = backend.files.root / MANIFEST
    original = json.loads(path.read_text())
    if mutation == "namespace":
        backend.files.write(MANIFEST, canonical_bytes({**original, "namespace": "a" * 32}), replace=True)
    elif mutation == "schema-bool":
        backend.files.write(MANIFEST, canonical_bytes({**original, "schema_version": True}), replace=True)
    elif mutation == "duplicate":
        backend.files.write(
            MANIFEST, b'{"backend":"macos_keychain","backend":"macos_keychain"}', replace=True
        )
    elif mutation == "corrupt":
        backend.files.write(MANIFEST, PRIVATE.encode(), replace=True)
    elif mutation == "hardlink":
        os.link(path, tmp_path / "linked-manifest")
    else:
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    for operation in (
        lambda: backend.get(ref),
        lambda: backend.put(PRIVATE),
        lambda: backend.discard_new(ref),
    ):
        with pytest.raises(ContextError) as caught:
            operation()
        no_private(caught.value)
    assert items.calls == before and len(items.values) == 1


@pytest.mark.parametrize(
    "mutation", ["namespace", "ref", "bool-int", "extra", "corrupt", "hardlink", "symlink"]
)
def test_reference_confirmation_must_match_before_reading_or_deleting_system_item(setup, tmp_path, mutation):
    backend, items = setup
    ref = backend.put(PRIVATE)
    path = backend.files.root / ref
    data = json.loads(path.read_text())
    before = list(items.calls)
    changes = {
        "namespace": {"namespace": "a" * 32},
        "ref": {"ref": "k_" + "b" * 32},
        "bool-int": {"confirmed": 1},
        "extra": {"value": PRIVATE},
    }
    if mutation in changes:
        backend.files.write(ref, canonical_bytes({**data, **changes[mutation]}), replace=True)
    elif mutation == "corrupt":
        backend.files.write(ref, PRIVATE.encode(), replace=True)
    elif mutation == "hardlink":
        os.link(path, tmp_path / "linked-ref")
    else:
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    for operation in (lambda: backend.get(ref), lambda: backend.discard_new(ref)):
        with pytest.raises(ContextError) as caught:
            operation()
        no_private(caught.value)
    assert items.calls == before and len(items.values) == 1


def test_directory_identity_swap_and_permissions_change_stop_before_system_query(setup, tmp_path):
    backend, items = setup
    ref = backend.put(PRIVATE)
    before = list(items.calls)
    root = backend.files.root
    os.chmod(root, 0o750)
    with pytest.raises(ContextError) as caught:
        backend.get(ref)
    assert caught.value.code == "unsafe_secret_permissions"
    os.chmod(root, 0o700)
    root.rename(tmp_path / "preserved-original")
    root.mkdir(mode=0o700)
    with pytest.raises(ContextError) as caught:
        backend.get(ref)
    assert caught.value.code == "storage_unavailable" and items.calls == before


@pytest.mark.parametrize(
    "ref", ["../outside", "/outside", "k_" + "a" * 31, "k_" + "A" * 32, "k_" + "a" * 32 + "/x", None, 1]
)
def test_untrusted_ref_never_becomes_system_account_or_path(setup, ref):
    backend, items = setup
    for operation in (backend.get, backend.discard_new):
        with pytest.raises(ContextError) as caught:
            operation(ref)
        assert caught.value.code == "invalid_credential_reference"
    assert not items.calls


@pytest.mark.parametrize("value", ["", "line\nkey", "nul\x00key", "a" * 4097, None, 1])
def test_invalid_key_is_rejected_without_system_request(setup, value):
    backend, items = setup
    with pytest.raises(ContextError) as caught:
        backend.put(value)
    assert caught.value.code == "invalid_credential" and not items.calls


@pytest.mark.parametrize(
    "code", ["credential_conflict", "credential_locked", "credential_denied", "invalid_credential"]
)
def test_explicit_create_failure_does_not_publish_or_overwrite_and_is_sanitized(setup, code, capsys):
    backend, items = setup
    items.fail["create"] = ContextError(code, PRIVATE)
    with pytest.raises(ContextError) as caught:
        backend.put(PRIVATE)
    assert caught.value.code == code
    no_private(caught.value)
    assert list(backend.files.root.iterdir()) == [backend.files.root / MANIFEST]
    assert len(items.calls) == 1 and not items.values and not backend._new
    assert capsys.readouterr() == ("", "")


def test_create_only_collision_preserves_existing_system_value_and_metadata(setup, monkeypatch):
    backend, items = setup
    ref = backend.put("existing-value")
    before = backend.files.read(ref, private=True)
    identity = uuid.UUID(hex=ref[2:])
    monkeypatch.setattr("collection_context.infrastructure.system_secrets.uuid.uuid4", lambda: identity)
    with pytest.raises(ContextError) as caught:
        backend.put(PRIVATE)
    assert caught.value.code == "credential_conflict"
    assert backend.files.read(ref, private=True) == before
    assert len(items.values) == 1 and backend.get(ref) == "existing-value"


@pytest.mark.parametrize("data", [b'{"confirmed":NaN}', b'{"ref":"\\ud800"}'])
def test_noncanonical_json_is_fixed_error_before_system_query(setup, data):
    backend, items = setup
    ref = backend.put(PRIVATE)
    backend.files.write(ref, data, replace=True)
    with pytest.raises(ContextError) as caught:
        backend.get(ref)
    assert caught.value.code == "system_secret_metadata_invalid" and len(items.calls) == 1


@pytest.mark.parametrize(
    "failure", [RuntimeError(PRIVATE), SystemExit(PRIVATE), ContextError("credential_unavailable", PRIVATE)]
)
def test_unknown_system_create_outcome_never_retries_or_deletes(setup, failure):
    backend, items = setup

    def ambiguous(service, ref, value):
        items.calls.append(("create", service, ref))
        items.values[service, ref] = value
        raise failure

    items.create = ambiguous
    with pytest.raises(ContextError) as caught:
        backend.put(PRIVATE)
    assert caught.value.code == "system_secret_outcome_unknown"
    no_private(caught.value)
    assert len(items.calls) == len(items.values) == 1 and not backend._new
    assert list(backend.files.root.iterdir()) == [backend.files.root / MANIFEST]


@pytest.mark.parametrize("committed", [False, True])
def test_metadata_publication_failure_preserves_item_without_unsafe_rollback(setup, monkeypatch, committed):
    backend, items = setup
    write = backend.files.write

    def fail(name, data, **kwargs):
        if committed:
            write(name, data, **kwargs)
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(backend.files, "write", fail)
    with pytest.raises(ContextError) as caught:
        backend.put(PRIVATE)
    assert caught.value.code == "system_secret_outcome_unknown"
    no_private(caught.value)
    assert len(items.values) == len(items.calls) == 1 and items.calls[0][0] == "create"
    ref = items.calls[0][2]
    assert not backend._new
    with pytest.raises(ContextError) as caught:
        backend.discard_new(ref)
    assert caught.value.code == "system_secret_rollback_forbidden"
    assert (backend.files.root / ref).exists() is committed


def test_only_this_instance_new_unread_reference_can_be_rolled_back(setup):
    backend, items = setup
    old, fresh = backend.put("old"), backend.put(PRIVATE)
    assert backend.get(old) == "old"
    backend.discard_new(fresh)
    assert len(items.values) == 1 and (backend.files.root / old).exists()
    assert not (backend.files.root / fresh).exists()
    with pytest.raises(ContextError) as caught:
        backend.discard_new(old)
    assert caught.value.code == "system_secret_rollback_forbidden"
    with pytest.raises(ContextError) as caught:
        backend.discard_new(fresh)
    assert caught.value.code == "system_secret_rollback_forbidden"
    assert sum(call[0] == "delete" for call in items.calls) == 1


@pytest.mark.parametrize("code", ["credential_locked", "credential_denied", "credential_missing"])
def test_get_system_error_is_fixed_without_fallback(setup, code):
    backend, items = setup
    ref = backend.put(PRIVATE)
    items.fail["read"] = ContextError(code, PRIVATE)
    with pytest.raises(ContextError) as caught:
        backend.get(ref)
    assert caught.value.code == code
    no_private(caught.value)
    assert len(items.values) == 1 and len(items.calls) == 2


@pytest.mark.parametrize("deleted", [False, True])
def test_ambiguous_delete_preserves_metadata_and_forbids_retry(setup, deleted):
    backend, items = setup
    ref = backend.put(PRIVATE)

    def ambiguous(service, account):
        items.calls.append(("delete", service, account))
        if deleted:
            del items.values[service, account]
        raise RuntimeError(PRIVATE)

    items.delete = ambiguous
    with pytest.raises(ContextError) as caught:
        backend.discard_new(ref)
    assert caught.value.code == "system_secret_outcome_unknown"
    no_private(caught.value)
    assert (backend.files.root / ref).exists()
    with pytest.raises(ContextError) as caught:
        backend.discard_new(ref)
    assert caught.value.code == "system_secret_rollback_forbidden"
    assert sum(call[0] == "delete" for call in items.calls) == 1


def test_missing_metadata_prevents_even_exact_existing_item_read(setup):
    backend, items = setup
    ref = backend.put(PRIVATE)
    backend.files.unlink(ref)
    with pytest.raises(ContextError) as caught:
        backend.get(ref)
    assert caught.value.code == "credential_missing"
    assert len(items.calls) == len(items.values) == 1


def test_close_never_deletes_saved_items_and_closed_instance_cannot_operate(setup):
    backend, items = setup
    ref = backend.put(PRIVATE)
    root = backend.files.root
    backend.close()
    backend.close()
    for operation in (
        lambda: backend.get(ref),
        lambda: backend.put(PRIVATE),
        lambda: backend.discard_new(ref),
    ):
        with pytest.raises(ContextError) as caught:
            operation()
        assert caught.value.code == "system_secret_closed"
    assert len(items.calls) == len(items.values) == 1
    other = SystemSecrets(root, _backend=items)
    try:
        assert other.get(ref) == PRIVATE
    finally:
        other.close()
