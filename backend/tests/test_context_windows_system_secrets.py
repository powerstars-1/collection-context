"""Injected Win32 credential contracts on Mac; NOT Windows OS acceptance."""

from __future__ import annotations

import ctypes as C
import socket
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import windows_system_secrets as module

SERVICE = "org.collection-context.credentials." + "1" * 32
ACCOUNT = "k_" + "2" * 32
VALUE = "original-offline-test-secret-原创🙂"
TARGET = SERVICE + "/" + ACCOUNT


class Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def wide_at(pointer):
    parts = []
    for offset in range(0, 1024, 2):
        part = C.string_at(pointer + offset, 2)
        if part == b"\x00\x00":
            return b"".join(parts).decode("utf-16-le")
        parts.append(part)
    raise AssertionError("Test target did not terminate within fixture budget")


class SDK:
    def __init__(self):
        self.calls = []
        self.freed = []
        self.blocks = []
        self.last_error = 0
        self.fail = None
        self.raise_at = None
        self.raise_context = False
        self.raise_base = False
        self.raise_after_allocation = False
        self.body = VALUE.encode()
        self.target = TARGET
        self.account = ACCOUNT
        self.mutate = lambda credential, base: None
        self.null_result = False
        self.blob_input_address = None
        self.blob_input_size = None
        self.dll = SimpleNamespace(
            CredWriteW=Function(self.write),
            CredReadW=Function(self.read),
            CredDeleteW=Function(self.delete),
            CredFree=Function(self.free),
        )

    def exception(self, name):
        if self.raise_at == name:
            if self.raise_context:
                raise ContextError("credential_denied", VALUE)
            if self.raise_base:
                raise KeyboardInterrupt(VALUE)
            raise RuntimeError(VALUE)

    def write(self, pointer, flags):
        self.exception("set")
        credential = C.cast(pointer, C.POINTER(module.CredentialW)).contents
        self.calls.append(
            (
                "set",
                wide_at(credential.TargetName),
                credential.Type,
                flags,
                wide_at(credential.UserName),
                credential.Persist,
                C.string_at(credential.CredentialBlob, credential.CredentialBlobSize),
            )
        )
        assert credential.Flags == 0 and not credential.Comment and not credential.TargetAlias
        assert credential.AttributeCount == 0 and not credential.Attributes
        self.blob_input_address = credential.CredentialBlob
        self.blob_input_size = credential.CredentialBlobSize
        if self.fail == "set":
            return 0
        return 1

    def read(self, name, kind, flags, output):
        self.exception("get")
        self.calls.append(("get", wide_at(name), kind, flags))
        if self.fail == "get":
            return 0
        if self.null_result:
            return 1
        block = C.create_string_buffer(8192)
        self.blocks.append(block)
        base = C.addressof(block)
        credential = module.CredentialW.from_buffer(block)
        target = self.target.encode("utf-16-le") + b"\x00\x00"
        account = self.account.encode("utf-16-le") + b"\x00\x00"
        C.memmove(base + 80, target, len(target))
        C.memmove(base + 1024, account, len(account))
        C.memmove(base + 2048, self.body, len(self.body))
        credential.Type, credential.Persist = 1, 2
        credential.TargetName, credential.UserName = base + 80, base + 1024
        credential.CredentialBlob, credential.CredentialBlobSize = base + 2048, len(self.body)
        self.mutate(credential, base)
        C.cast(output, C.POINTER(C.POINTER(module.CredentialW)))[0] = C.cast(
            base, C.POINTER(module.CredentialW)
        )
        if self.raise_after_allocation:
            raise RuntimeError(VALUE)
        return 1

    def delete(self, name, kind, flags):
        self.exception("delete")
        self.calls.append(("delete", wide_at(name), kind, flags))
        return 0 if self.fail == "delete" else 1

    def free(self, pointer):
        self.freed.append(pointer.value)
        self.exception("free")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Credential SDK tests must never contact any network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def sdk():
    api = SDK()
    manager = module.WindowsCredentialManager(_api=module._NativeAPI(api.dll, lambda: api.last_error))
    try:
        yield api, manager
    finally:
        manager.close()


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code
    assert VALUE not in str(caught.value)
    assert caught.value.__cause__ is None


def test_fixed_llp64_abi_and_constructor_no_queries(sdk):
    api, manager = sdk
    assert C.sizeof(module.CredentialW) == 80
    assert module.CredentialW.CredentialBlobSize.offset == 32
    assert module.CredentialW.CredentialBlob.offset == 40
    assert module.CredentialW.UserName.offset == 72
    assert api.dll.CredReadW.argtypes == [
        module.PTR,
        module.U32,
        module.U32,
        C.POINTER(C.POINTER(module.CredentialW)),
    ]
    assert api.dll.CredWriteW.argtypes == [C.POINTER(module.CredentialW), module.U32]
    assert api.dll.CredDeleteW.argtypes == [module.PTR, module.U32, module.U32]
    assert api.dll.CredFree.argtypes == [module.PTR] and api.dll.CredFree.restype is None
    assert api.dll.CredReadW.restype is module.BOOL
    assert manager.storage_kind == "windows_credential_manager"
    assert not hasattr(manager, "create")  # Upsert cannot masquerade as create-only.
    assert not api.calls and not api.freed


def test_exact_os_only_set_read_delete_utf8_and_no_enumeration(sdk, tmp_path):
    api, manager = sdk
    before = list(tmp_path.iterdir())
    manager.set(SERVICE, ACCOUNT, VALUE)
    assert api.calls == [("set", TARGET, 1, 0, ACCOUNT, 2, VALUE.encode())]
    assert manager.get(SERVICE, ACCOUNT) == VALUE
    assert manager.read(SERVICE, ACCOUNT) == VALUE
    manager.delete(SERVICE, ACCOUNT)
    assert api.calls[1:] == [("get", TARGET, 1, 0), ("get", TARGET, 1, 0), ("delete", TARGET, 1, 0)]
    assert api.freed == [C.addressof(block) for block in api.blocks]
    assert list(tmp_path.iterdir()) == before and VALUE not in repr(manager)


@pytest.mark.parametrize("operation", ["set", "get", "delete"])
@pytest.mark.parametrize(
    "service,account",
    [
        ("foreign", ACCOUNT),
        (SERVICE + "/escape", ACCOUNT),
        (SERVICE, "../key"),
        (SERVICE, "k_" + "2" * 31),
        (SERVICE.upper(), ACCOUNT),
        (SERVICE, ACCOUNT + "\x00"),
        (None, ACCOUNT),
        (SERVICE, 1),
    ],
)
def test_reference_rejected_before_native_requests(sdk, operation, service, account):
    api, manager = sdk
    arguments = (service, account, VALUE) if operation == "set" else (service, account)
    failure("invalid_credential_reference", lambda: getattr(manager, operation)(*arguments))
    assert not api.calls and not api.freed


@pytest.mark.parametrize("value", ["", None, 1, "x\r", "x\n", "x\x00", "\ud800", "x" * 2561, "中" * 854])
def test_invalid_values_and_byte_not_character_limit_before_native(sdk, value):
    api, manager = sdk
    failure("invalid_credential", lambda: manager.set(SERVICE, ACCOUNT, value))
    assert not api.calls


@pytest.mark.parametrize("value", ["x" * 2560, "中" * 853 + "x"])
def test_exact_blob_byte_limit_and_explicit_upsert(sdk, value):
    api, manager = sdk
    manager.set(SERVICE, ACCOUNT, value)
    manager.set(SERVICE, ACCOUNT, value)
    assert [call[0] for call in api.calls] == ["set", "set"]
    assert all(len(call[-1]) == 2560 for call in api.calls)


@pytest.mark.parametrize("operation", ["set", "get", "delete"])
@pytest.mark.parametrize(
    "status,code",
    [
        (1168, "credential_missing"),
        (1312, "credential_locked"),
        (5, "credential_denied"),
        (87, "credential_unavailable"),
        (0, "credential_unavailable"),
    ],
)
def test_native_false_maps_fixed_errors_without_retry(sdk, operation, status, code):
    api, manager = sdk
    api.fail, api.last_error = operation, status
    args = (SERVICE, ACCOUNT, VALUE) if operation == "set" else (SERVICE, ACCOUNT)
    failure(code, lambda: getattr(manager, operation)(*args))
    assert len(api.calls) == 1 and not api.freed


@pytest.mark.parametrize("operation", ["set", "get", "delete", "free"])
@pytest.mark.parametrize("context", [False, True])
def test_native_exceptions_are_redacted_and_never_retried(sdk, operation, context):
    api, manager = sdk
    api.raise_at, api.raise_context = operation, context
    code = (
        "credential_denied"
        if context and operation != "free"
        else ("credential_outcome_unknown" if operation in {"set", "delete"} else "credential_unavailable")
    )
    actual = "get" if operation == "free" else operation
    args = (SERVICE, ACCOUNT, VALUE) if actual == "set" else (SERVICE, ACCOUNT)
    failure(code, lambda: getattr(manager, actual)(*args))
    assert len(api.freed) == (1 if operation == "free" else 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Flags", 1),
        ("Type", 2),
        ("Persist", 3),
        ("AttributeCount", 1),
        ("Attributes", 1),
        ("Comment", 1),
        ("TargetAlias", 1),
        ("CredentialBlobSize", 0),
        ("CredentialBlobSize", 2561),
        ("CredentialBlob", None),
        ("TargetName", None),
        ("UserName", None),
    ],
)
def test_malformed_structures_free_once_without_arbitrary_dereference(sdk, field, value):
    api, manager = sdk
    api.mutate = lambda credential, base: setattr(credential, field, value)
    failure("credential_unavailable", lambda: manager.get(SERVICE, ACCOUNT))
    assert api.freed == [C.addressof(api.blocks[0])]


@pytest.mark.parametrize("field", ["CredentialBlob", "TargetName", "UserName"])
@pytest.mark.parametrize("offset", [-1, 0, 79, 8192, 9000])
def test_pointer_span_bounds_reject_before_reading_foreign_memory(sdk, field, offset):
    api, manager = sdk
    api.mutate = lambda credential, base: setattr(credential, field, base + offset)
    failure("credential_unavailable", lambda: manager.get(SERVICE, ACCOUNT))
    assert len(api.freed) == 1


@pytest.mark.parametrize("field", ["target", "account"])
def test_exact_returned_target_and_account_must_match(sdk, field):
    api, manager = sdk
    setattr(api, field, "foreign-credential")
    failure("credential_unavailable", lambda: manager.get(SERVICE, ACCOUNT))
    assert len(api.freed) == 1


@pytest.mark.parametrize("body", [b"\xff", b"x\x00", b"x\r", b"x\n", b"\xed\xa0\x80"])
def test_bad_utf8_or_invalid_secret_cannot_escape_and_always_free(sdk, body):
    api, manager = sdk
    api.body = body
    failure("invalid_credential", lambda: manager.get(SERVICE, ACCOUNT))
    assert len(api.freed) == 1


def test_success_with_null_output_is_rejected_without_freeing_null(sdk):
    api, manager = sdk
    api.null_result = True
    failure("credential_unavailable", lambda: manager.get(SERVICE, ACCOUNT))
    assert not api.freed


@pytest.mark.parametrize("operation", ["set", "get", "delete"])
def test_closed_instance_cannot_dispatch(sdk, operation):
    api, manager = sdk
    manager.close()
    args = (SERVICE, ACCOUNT, VALUE) if operation == "set" else (SERVICE, ACCOUNT)
    failure("credential_unavailable", lambda: getattr(manager, operation)(*args))
    assert not api.calls


def test_default_mac_runtime_refuses_before_dll_loading(monkeypatch):
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    failure("unsupported_platform", module.WindowsCredentialManager)


@pytest.mark.parametrize("machine", ["ARM64", "aarch64", "x86", "i386"])
def test_native_loader_rejects_non_x64_before_dll_lookup(monkeypatch, machine):
    # Call loader directly; no pathlib/pytest OS emulation and no actual DLL.
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    failure("unsupported_platform", module._load_api)


def test_missing_symbols_fail_constructor_without_queries():
    api = SDK()
    del api.dll.CredFree
    failure(
        "credential_unavailable",
        lambda: module.WindowsCredentialManager(_api=module._NativeAPI(api.dll, lambda: 0)),
    )
    assert not api.calls


def test_native_exception_after_returning_owned_block_still_frees_once(sdk):
    api, manager = sdk
    api.raise_after_allocation = True
    failure("credential_unavailable", lambda: manager.get(SERVICE, ACCOUNT))
    assert api.freed == [C.addressof(api.blocks[0])]


@pytest.mark.parametrize("operation", ["set", "get", "delete", "free"])
def test_interrupt_after_native_entry_is_redacted_and_mutation_unknown(sdk, operation):
    api, manager = sdk
    api.raise_at, api.raise_base = operation, True
    actual = "get" if operation == "free" else operation
    args = (SERVICE, ACCOUNT, VALUE) if actual == "set" else (SERVICE, ACCOUNT)
    code = "credential_outcome_unknown" if operation in {"set", "delete"} else "credential_unavailable"
    failure(code, lambda: getattr(manager, actual)(*args))
    assert len(api.freed) == (1 if operation == "free" else 0)


@pytest.mark.parametrize("outcome", ["success", "false", "exception"])
def test_mutable_input_blob_is_zeroed_before_scope_exit(sdk, monkeypatch, outcome):
    api, manager = sdk
    zeroed = []
    original = C.memset

    def observe(address, fill, count):
        original(address, fill, count)
        zeroed.append(C.string_at(address, count))  # Borrowed buffer still lives here.

    monkeypatch.setattr(module.C, "memset", observe)
    if outcome == "false":
        api.fail, api.last_error = "set", 5
        failure("credential_denied", lambda: manager.set(SERVICE, ACCOUNT, VALUE))
    elif outcome == "exception":
        api.raise_at = "set"
        failure("credential_outcome_unknown", lambda: manager.set(SERVICE, ACCOUNT, VALUE))
    else:
        manager.set(SERVICE, ACCOUNT, VALUE)
    assert zeroed == [b"\x00" * len(VALUE.encode())]


def test_loading_fixed_system32_dll_does_not_query_credentials(monkeypatch):
    api, loaded = SDK(), []
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")

    def loader(name, **kwargs):
        loaded.append((name, kwargs))
        return api.dll

    monkeypatch.setattr(module.C, "WinDLL", loader, raising=False)
    monkeypatch.setattr(module.C, "get_last_error", lambda: 0, raising=False)
    manager = module.WindowsCredentialManager()
    manager.close()
    assert loaded == [("advapi32.dll", {"use_last_error": True, "winmode": 0x800})]
    assert not api.calls


def test_system_secrets_windows_gate_remains_closed_without_sdk_queries(monkeypatch):
    from collection_context.infrastructure import system_secrets

    # Inject only selector input; do not emulate the OS or load any real SDK.
    monkeypatch.setattr(system_secrets, "sys", SimpleNamespace(platform="win32"))
    failure("system_secret_unsupported", lambda: system_secrets._backend_or_default(None))
