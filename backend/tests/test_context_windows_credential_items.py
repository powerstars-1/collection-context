"""Cooperative item protocol with injected DLL/OS doubles, NOT Windows acceptance.

Two facade instances model cooperating processes' separate handles. The existing
native lease double models the kernel lock table; it is NOT proof of real OS
cross-process exclusion, process-death release or actual Credential Manager IO.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest
from test_context_windows_ownership import LeaseDLLs

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_credential_items as module
from collection_context.infrastructure.windows_ownership import WindowsWriterLease

ROOT = "C:\\原创库"
NAMESPACE = "1" * 32
SERVICE = "org.collection-context.credentials." + NAMESPACE
ACCOUNT = "k_" + "2" * 32
VALUE = "synthetic-offline-credential-原创🙂"


class Manager:
    def __init__(self, dlls):
        self.dlls = dlls
        self.items = {}
        self.calls = []
        self.closed = 0
        self.hook = lambda method: None

    def operation(self, method, service, account):
        # Every queried/mutated credential is already within our owned kernel
        # lease; this assertion never treats metadata/PID as ownership proof.
        assert any(self.dlls.held.values())
        self.calls.append((method, service, account))
        self.hook(method)

    def get(self, service, account):
        self.operation("get", service, account)
        if (service, account) not in self.items:
            raise ContextError("credential_missing", VALUE)
        return self.items[service, account]

    def set(self, service, account, value):
        self.operation("set", service, account)
        self.items[service, account] = value
        self.hook("after_set")

    def delete(self, service, account):
        self.operation("delete", service, account)
        del self.items[service, account]
        self.hook("after_delete")

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Credential creation tests cannot access any network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def setup():
    dlls = LeaseDLLs()
    manager = Manager(dlls)
    backend = module.WindowsCredentialItems(ROOT, NAMESPACE, _manager=manager, _native=dlls.native)
    try:
        yield dlls, manager, backend
    finally:
        backend.close()


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code
    assert VALUE not in str(caught.value) and VALUE not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_constructor_is_readonly_no_credential_query_or_lease_bootstrap(setup):
    dlls, manager, backend = setup
    assert not manager.calls and not dlls.writes and not dlls.locks
    assert not any(entry["disposition"] == 2 for entry in dlls.opened)
    assert backend.storage_kind == "windows_credential_manager" and not hasattr(backend, "set")


def test_create_read_delete_and_reference_reopen_under_separate_handles(setup):
    dlls, manager, backend = setup
    backend.create(SERVICE, ACCOUNT, VALUE)
    assert manager.items == {(SERVICE, ACCOUNT): VALUE}
    assert backend.read(SERVICE, ACCOUNT) == VALUE
    with_no_other_backend = module.WindowsCredentialItems(
        ROOT, NAMESPACE, _manager=manager, _native=dlls.native
    )
    try:
        assert with_no_other_backend.read(SERVICE, ACCOUNT) == VALUE
        with_no_other_backend.delete(SERVICE, ACCOUNT)
    finally:
        with_no_other_backend.close()
    failure("credential_missing", lambda: backend.read(SERVICE, ACCOUNT))
    assert not manager.items and not any(dlls.held.values()) and not manager.closed
    for file in dlls.files.values():
        body = file.get("body", b"")
        assert VALUE.encode() not in body
        if body.startswith(b'{"nonce"'):
            assert set(json.loads(body)) == {"nonce", "pid"}


@pytest.mark.parametrize("existing", [VALUE, "different-immutable-value"])
def test_existing_item_is_rejected_even_if_value_matches_and_never_overwritten(setup, existing):
    dlls, manager, backend = setup
    manager.items[SERVICE, ACCOUNT] = existing
    failure("credential_conflict", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert manager.items[SERVICE, ACCOUNT] == existing
    assert [call[0] for call in manager.calls] == ["get"]
    assert not any(dlls.held.values())


@pytest.mark.parametrize("operation", ["create", "read", "delete"])
@pytest.mark.parametrize(
    "service,account",
    [
        (SERVICE.replace(NAMESPACE, "3" * 32), ACCOUNT),
        ("foreign", ACCOUNT),
        (SERVICE, "../key"),
        (SERVICE, ACCOUNT + "\x00"),
        (None, ACCOUNT),
    ],
)
def test_foreign_service_and_invalid_reference_rejected_before_lease_or_sdk(
    setup, operation, service, account
):
    dlls, manager, backend = setup
    args = (service, account, VALUE) if operation == "create" else (service, account)
    failure("invalid_credential_reference", lambda: getattr(backend, operation)(*args))
    assert not manager.calls and not dlls.locks and not dlls.writes


@pytest.mark.parametrize("value", ["", "x\n", "x\x00", "\ud800", "中" * 854])
def test_invalid_creation_value_cannot_bootstrap_or_read_credentials(setup, value):
    dlls, manager, backend = setup
    failure("invalid_credential", lambda: backend.create(SERVICE, ACCOUNT, value))
    assert not manager.calls and not dlls.locks and not dlls.writes


@pytest.mark.parametrize("namespace", ["", "x" * 32, "1" * 31, NAMESPACE + "/key", None])
def test_bad_namespace_never_opens_files_or_manager(namespace):
    dlls = LeaseDLLs()
    manager = Manager(dlls)
    failure(
        "invalid_credential_reference",
        lambda: module.WindowsCredentialItems(ROOT, namespace, _manager=manager, _native=dlls.native),
    )
    assert not dlls.opened and not manager.calls


@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_another_owned_instance_blocks_without_wait_query_or_takeover(setup, operation):
    dlls, manager, backend = setup
    with WindowsWriterLease(ROOT, _native=dlls.native) as first:
        writes = len(dlls.writes)
        args = (SERVICE, ACCOUNT, VALUE) if operation == "create" else (SERVICE, ACCOUNT)
        failure("credential_locked", lambda: getattr(backend, operation)(*args))
        assert not manager.calls and len(dlls.writes) == writes
        first.check()
    assert not any(dlls.held.values())


@pytest.mark.parametrize(
    "code",
    [
        "credential_denied",
        "credential_locked",
        "credential_unavailable",
        "invalid_credential",
        "unknown-private-code",
    ],
)
def test_failed_existence_query_never_becomes_absence_or_write(setup, code):
    dlls, manager, backend = setup

    def hook(method):
        if method == "get":
            raise ContextError(code, VALUE)

    manager.hook = hook
    expected = (
        code
        if code in {"credential_denied", "credential_locked", "invalid_credential"}
        else "credential_unavailable"
    )
    failure(expected, lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert not manager.items and [call[0] for call in manager.calls] == ["get"]
    assert not any(dlls.held.values())


def replace_root(dlls):
    key = (None, "\\device\\harddiskvolume4\\原创库")
    dlls.names[key] = dict(dlls.names[key], id=b"z" * 16)


def test_root_replacement_after_absence_query_blocks_before_dispatch(setup):
    dlls, manager, backend = setup
    manager.hook = lambda method: replace_root(dlls) if method == "get" else None
    failure("credential_unavailable", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert not manager.items and [call[0] for call in manager.calls] == ["get"]
    assert not any(dlls.held.values())


def test_saved_item_then_lost_root_reports_unknown_and_retains_item(setup):
    dlls, manager, backend = setup
    manager.hook = lambda method: replace_root(dlls) if method == "after_set" else None
    failure("credential_outcome_unknown", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert manager.items[SERVICE, ACCOUNT] == VALUE
    assert [call[0] for call in manager.calls] == ["get", "set"]
    assert not any(dlls.held.values())


@pytest.mark.parametrize("interrupt", [False, True])
def test_saved_item_with_exception_is_preserved_without_retry_or_cleanup(setup, interrupt):
    dlls, manager, backend = setup

    def hook(method):
        if method == "after_set":
            if interrupt:
                raise KeyboardInterrupt(VALUE)
            raise RuntimeError(VALUE)

    manager.hook = hook
    failure("credential_outcome_unknown", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert manager.items[SERVICE, ACCOUNT] == VALUE
    assert [call[0] for call in manager.calls] == ["get", "set"]
    assert not any(dlls.held.values())


def test_mismatched_readback_is_unknown_and_never_auto_deletes(setup):
    dlls, manager, backend = setup

    def hook(method):
        if method == "after_set":
            manager.items[SERVICE, ACCOUNT] = "unexpected-external-replacement"

    manager.hook = hook
    failure("credential_outcome_unknown", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert manager.items[SERVICE, ACCOUNT] == "unexpected-external-replacement"
    assert [call[0] for call in manager.calls] == ["get", "set", "get"]
    assert not any(dlls.held.values())


def test_close_failure_after_save_cannot_report_success_or_remove_item(setup, monkeypatch):
    dlls, manager, backend = setup
    original = module.WindowsWriterLease.close

    def failure_after_close(lease):
        original(lease)
        raise RuntimeError(VALUE)

    monkeypatch.setattr(module.WindowsWriterLease, "close", failure_after_close)
    failure("credential_outcome_unknown", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert manager.items[SERVICE, ACCOUNT] == VALUE and not any(dlls.held.values())
    assert not any(call[0] == "delete" for call in manager.calls)


def test_concurrent_instance_cannot_enter_absence_check_while_first_owns_lock(setup):
    dlls, manager, backend = setup
    second = module.WindowsCredentialItems(ROOT, NAMESPACE, _manager=manager, _native=dlls.native)
    entered, resume = threading.Event(), threading.Event()
    errors = []

    def hook(method):
        if method == "get" and not manager.items:
            entered.set()
            assert resume.wait(2)

    def create():
        try:
            backend.create(SERVICE, ACCOUNT, VALUE)
        except BaseException as error:
            errors.append(type(error).__name__)

    manager.hook = hook
    thread = threading.Thread(target=create)
    thread.start()
    try:
        assert entered.wait(2)
        failure("credential_locked", lambda: second.create(SERVICE, ACCOUNT, "other-value"))
        resume.set()
        thread.join(2)
        assert not thread.is_alive() and not errors
        manager.hook = lambda method: None
        failure("credential_conflict", lambda: second.create(SERVICE, ACCOUNT, "other-value"))
        assert manager.items[SERVICE, ACCOUNT] == VALUE
        assert [call[0] for call in manager.calls].count("set") == 1
    finally:
        resume.set()
        thread.join(2)
        second.close()
    assert not any(dlls.held.values())


def test_already_missing_delete_has_no_mutation_and_remains_missing(setup):
    dlls, manager, backend = setup
    failure("credential_missing", lambda: backend.delete(SERVICE, ACCOUNT))
    assert [call[0] for call in manager.calls] == ["get"] and not any(dlls.held.values())


def test_unknown_delete_does_not_retry_or_recreate(setup):
    dlls, manager, backend = setup
    manager.items[SERVICE, ACCOUNT] = VALUE

    def hook(method):
        if method == "after_delete":
            raise RuntimeError(VALUE)

    manager.hook = hook
    failure("credential_outcome_unknown", lambda: backend.delete(SERVICE, ACCOUNT))
    assert not manager.items
    assert [call[0] for call in manager.calls] == ["get", "delete"]
    assert not any(dlls.held.values())


def test_borrowed_manager_not_closed_and_closed_items_do_not_query(setup):
    dlls, manager, backend = setup
    backend.close()
    backend.close()
    failure("credential_unavailable", lambda: backend.create(SERVICE, ACCOUNT, VALUE))
    assert not manager.closed and not manager.calls and not any(dlls.held.values())


def test_owned_manager_is_closed_exactly_once(monkeypatch):
    dlls = LeaseDLLs()
    manager = Manager(dlls)
    monkeypatch.setattr(module, "WindowsCredentialManager", lambda: manager)
    backend = module.WindowsCredentialItems(ROOT, NAMESPACE, _native=dlls.native)
    backend.close()
    backend.close()
    assert manager.closed == 1 and not manager.calls


def test_manager_initialization_failure_closes_root_no_lease_or_credential_query(monkeypatch):
    dlls = LeaseDLLs()

    def unavailable():
        raise RuntimeError(VALUE)

    monkeypatch.setattr(module, "WindowsCredentialManager", unavailable)
    failure(
        "credential_unavailable", lambda: module.WindowsCredentialItems(ROOT, NAMESPACE, _native=dlls.native)
    )
    assert not dlls.writes and not dlls.locks
    assert set(dlls.files) <= set(dlls.closed)


@pytest.mark.parametrize("after_save", [False, True])
def test_lost_actual_lease_before_or_after_dispatch_has_correct_uncertainty(setup, monkeypatch, after_save):
    dlls, manager, backend = setup
    leases = []

    def factory(root, **kwargs):
        lease = WindowsWriterLease(root, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(module, "WindowsWriterLease", factory)

    def hook(method):
        if method == ("after_set" if after_save else "get"):
            leases[-1].lock.close()

    manager.hook = hook
    failure(
        "credential_outcome_unknown" if after_save else "credential_unavailable",
        lambda: backend.create(SERVICE, ACCOUNT, VALUE),
    )
    assert bool(manager.items) is after_save
    assert [call[0] for call in manager.calls] == (["get", "set"] if after_save else ["get"])
    assert all(lease.files.handle.closed for lease in leases) and not any(dlls.held.values())


def test_private_root_failure_closes_handles_before_manager_is_constructed(monkeypatch):
    dlls = LeaseDLLs()
    constructed = []

    def denied(_handle):
        raise ContextError("unsafe_secret_permissions", VALUE)

    monkeypatch.setattr(dlls.native, "require_private_security", denied)
    monkeypatch.setattr(module, "WindowsCredentialManager", lambda: constructed.append(True))
    failure("credential_denied", lambda: module.WindowsCredentialItems(ROOT, NAMESPACE, _native=dlls.native))
    assert not constructed and not dlls.writes and not dlls.locks
    assert set(dlls.files) <= set(dlls.closed)


def test_owner_close_attempts_all_owned_resources_even_on_root_close_failure(monkeypatch):
    dlls = LeaseDLLs()
    manager = Manager(dlls)
    monkeypatch.setattr(module, "WindowsCredentialManager", lambda: manager)
    backend = module.WindowsCredentialItems(ROOT, NAMESPACE, _native=dlls.native)
    original = backend.files.close

    def failure_after_close():
        original()
        raise RuntimeError(VALUE)

    monkeypatch.setattr(backend.files, "close", failure_after_close)
    failure("credential_unavailable", backend.close)
    assert manager.closed == 1 and backend.files.handle.closed
    backend.close()
    assert manager.closed == 1
