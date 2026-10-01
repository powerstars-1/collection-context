"""SDK doubles and Darwin ABI only: never query the actual local Keychain."""

from __future__ import annotations

import ctypes as C
import threading
import traceback
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import macos_credentials as module

SERVICE = "org.collection-context.credentials." + "1" * 32
ACCOUNT = "k_" + "2" * 32
VALUE = "original-test-secret-原创"


class Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeSDK:
    def __init__(self):
        names = (
            "kSecClass",
            "kSecClassGenericPassword",
            "kSecAttrService",
            "kSecAttrAccount",
            "kSecAttrSynchronizable",
            "kSecUseDataProtectionKeychain",
            "kSecUseAuthenticationUI",
            "kSecUseAuthenticationUIFail",
            "kSecValueData",
            "kSecReturnData",
            "kSecMatchLimit",
            "kSecMatchLimitOne",
            "kCFBooleanTrue",
            "kCFBooleanFalse",
        )
        self.constants = {name: index + 1 for index, name in enumerate(names)}
        self.objects = {}
        self.next_id = 100
        self.calls = []
        self.released = []
        self.status = 0
        self.result = VALUE.encode()
        self.result_type = 7
        self.result_length = None
        self.result_null = False
        self.raise_secret = False
        self.buffers = []
        self.created_secret_buffer = None
        self.fail_allocation = None
        self.ui_state = 1
        self.ui_calls = []
        self.get_status = 0
        self.set_status = 0
        self.restore_status = 0
        self.set_count = 0
        self.sec = SimpleNamespace(
            SecItemAdd=Function(lambda q, out: self.operation("create", q, out)),
            SecItemCopyMatching=Function(lambda q, out: self.operation("read", q, out)),
            SecItemDelete=Function(lambda q: self.operation("delete", q)),
            SecKeychainGetUserInteractionAllowed=Function(self.get_ui),
            SecKeychainSetUserInteractionAllowed=Function(self.set_ui),
        )
        self.cf = SimpleNamespace(
            CFStringCreateWithBytes=Function(self.string),
            CFDataCreate=Function(self.data),
            CFDictionaryCreate=Function(self.dictionary),
            CFRelease=Function(self.release),
            CFGetTypeID=Function(lambda ptr: self.result_type),
            CFDataGetTypeID=Function(lambda: 7),
            CFDataGetLength=Function(
                lambda ptr: len(self.result) if self.result_length is None else self.result_length
            ),
            CFDataGetBytePtr=Function(self.bytes),
        )

    def allocate(self, kind, value):
        if self.fail_allocation == kind:
            return None
        self.next_id += 1
        self.objects[self.next_id] = value
        return self.next_id

    def string(self, allocator, raw, length, encoding, external):
        assert allocator is None and encoding == module._UTF8 and external == 0
        return self.allocate("string", C.string_at(raw, length).decode())

    def data(self, allocator, raw, length):
        assert allocator is None
        self.created_secret_buffer = raw
        return self.allocate("data", C.string_at(raw, length))

    def dictionary(self, allocator, keys, values, size, key_callbacks, value_callbacks):
        assert allocator is None and key_callbacks is not None and value_callbacks is not None
        return self.allocate("dictionary", dict(zip(keys[:size], values[:size], strict=True)))

    def release(self, pointer):
        actual = pointer.value if isinstance(pointer, C.c_void_p) else pointer
        self.released.append(actual)

    def operation(self, kind, query, output=None):
        assert self.ui_state == 0
        self.calls.append((kind, self.objects[query]))
        if self.raise_secret:
            raise RuntimeError(VALUE)
        if kind == "read" and not self.result_null:
            pointer = self.allocate("output", self.result)
            C.cast(output, C.POINTER(C.c_void_p))[0] = pointer
        return self.status

    def get_ui(self, output):
        self.ui_calls.append("get")
        self.set_count = 0
        C.cast(output, C.POINTER(C.c_ubyte))[0] = self.ui_state
        return self.get_status

    def set_ui(self, state):
        self.ui_calls.append(state)
        self.set_count += 1
        result = self.set_status if self.set_count == 1 else self.restore_status
        if result == 0:
            self.ui_state = state
        return result

    def bytes(self, pointer):
        buffer = C.create_string_buffer(self.result)
        self.buffers.append(buffer)
        return C.addressof(buffer)


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeSDK()
    monkeypatch.setattr(module, "_load_frameworks", lambda: (fake.sec, fake.cf))
    monkeypatch.setattr(module, "_constant", lambda library, name: fake.constants[name])
    monkeypatch.setattr(module, "_callbacks", lambda cf: (module._KeyCallbacks(), module._ValueCallbacks()))
    monkeypatch.setattr(module, "_UI_GUARD_FAILED", False)
    return fake


def test_constructor_only_binds_and_does_not_query(sdk):
    keychain = module.MacOSKeychain()
    assert sdk.calls == [] and sdk.objects == {} and sdk.ui_calls == []
    assert VALUE not in repr(keychain)
    assert sdk.sec.SecItemAdd.restype is C.c_int32
    assert sdk.cf.CFDataGetLength.restype is C.c_int64
    assert sdk.cf.CFGetTypeID.restype is C.c_uint64
    assert sdk.sec.SecItemCopyMatching.argtypes == [C.c_void_p, C.POINTER(C.c_void_p)]


@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_precise_noninteractive_queries_and_release(sdk, operation):
    keychain = module.MacOSKeychain()
    result = (
        getattr(keychain, operation)(SERVICE, ACCOUNT, VALUE)
        if operation == "create"
        else getattr(keychain, operation)(SERVICE, ACCOUNT)
    )
    assert result == (VALUE if operation == "read" else None)
    assert len(sdk.calls) == 1
    kind, query = sdk.calls[0]
    assert kind == operation
    c = sdk.constants
    expected = {
        c["kSecClass"],
        c["kSecAttrService"],
        c["kSecAttrAccount"],
        c["kSecAttrSynchronizable"],
        c["kSecUseDataProtectionKeychain"],
        c["kSecUseAuthenticationUI"],
    }
    if operation == "create":
        expected.add(c["kSecValueData"])
        assert sdk.objects[query[c["kSecValueData"]]] == VALUE.encode()
        assert C.string_at(sdk.created_secret_buffer, len(VALUE.encode())) == b"\x00" * len(VALUE.encode())
    elif operation == "read":
        expected.update({c["kSecReturnData"], c["kSecMatchLimit"]})
        assert query[c["kSecMatchLimit"]] == c["kSecMatchLimitOne"]
        assert query[c["kSecReturnData"]] == c["kCFBooleanTrue"]
    assert set(query) == expected
    assert sdk.objects[query[c["kSecAttrService"]]] == SERVICE
    assert sdk.objects[query[c["kSecAttrAccount"]]] == ACCOUNT
    assert query[c["kSecClass"]] == c["kSecClassGenericPassword"]
    assert query[c["kSecAttrSynchronizable"]] == c["kCFBooleanFalse"]
    assert query[c["kSecUseDataProtectionKeychain"]] == c["kCFBooleanFalse"]
    assert query[c["kSecUseAuthenticationUI"]] == c["kSecUseAuthenticationUIFail"]
    assert set(sdk.objects) == set(sdk.released)
    assert len(sdk.released) == len(set(sdk.released))
    assert VALUE not in repr(keychain) and VALUE not in repr(vars(keychain))
    assert sdk.ui_calls == ["get", 0, 1] and sdk.ui_state == 1


@pytest.mark.parametrize(
    "service,account",
    [
        ("other-service", ACCOUNT),
        (SERVICE, "default"),
        (SERVICE.upper(), ACCOUNT),
        (SERVICE + "\n", ACCOUNT),
        (SERVICE, ACCOUNT + "\n"),
        ("", ACCOUNT),
        (SERVICE, "k_" + "f" * 31),
        (SERVICE, "k_" + "f" * 33),
        (None, ACCOUNT),
        (SERVICE, None),
        (SERVICE + "\x00", ACCOUNT),
    ],
)
@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_invalid_reference_never_reaches_sdk(sdk, service, account, operation):
    keychain = module.MacOSKeychain()
    with pytest.raises(ContextError) as caught:
        getattr(keychain, operation)(service, account, VALUE) if operation == "create" else getattr(
            keychain, operation
        )(service, account)
    assert caught.value.code == "invalid_credential_reference"
    assert sdk.calls == [] and sdk.objects == {}


@pytest.mark.parametrize("value", [None, 1, b"bytes", "", "bad\x00value", "x" * 16385, "字" * 6000, "\ud800"])
def test_invalid_value_never_reaches_sdk(sdk, value):
    with pytest.raises(ContextError) as caught:
        module.MacOSKeychain().create(SERVICE, ACCOUNT, value)
    assert caught.value.code == "invalid_credential"
    assert sdk.calls == [] and sdk.objects == {}


@pytest.mark.parametrize(
    "status,code",
    [
        (-25299, "credential_conflict"),
        (-25300, "credential_missing"),
        (-25308, "credential_locked"),
        (-25315, "credential_locked"),
        (-25291, "credential_locked"),
        (-25293, "credential_denied"),
        (-25292, "credential_denied"),
        (-34018, "credential_denied"),
        (-34020, "credential_denied"),
        (-128, "credential_denied"),
        (-50, "credential_unavailable"),
        (123456, "credential_unavailable"),
    ],
)
@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_status_fixed_errors_no_fallback_and_cleanup(sdk, status, code, operation):
    sdk.status = status
    with pytest.raises(ContextError) as caught:
        keychain = module.MacOSKeychain()
        getattr(keychain, operation)(SERVICE, ACCOUNT, VALUE) if operation == "create" else getattr(
            keychain, operation
        )(SERVICE, ACCOUNT)
    assert caught.value.code == code
    assert (
        VALUE not in str(caught.value)
        and SERVICE not in str(caught.value)
        and ACCOUNT not in str(caught.value)
    )
    assert len(sdk.calls) == 1
    assert set(sdk.objects) == set(sdk.released)


@pytest.mark.parametrize("kind", ["string", "data", "dictionary"])
def test_allocation_failure_releases_created_objects(sdk, kind):
    sdk.fail_allocation = kind
    with pytest.raises(ContextError) as caught:
        module.MacOSKeychain().create(SERVICE, ACCOUNT, VALUE)
    assert caught.value.code == "credential_unavailable"
    assert sdk.calls == []
    assert set(sdk.objects) == set(sdk.released)


@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_unexpected_sdk_error_has_no_secret_exception_chain(sdk, operation):
    sdk.raise_secret = True
    with pytest.raises(ContextError) as caught:
        keychain = module.MacOSKeychain()
        getattr(keychain, operation)(SERVICE, ACCOUNT, VALUE) if operation == "create" else getattr(
            keychain, operation
        )(SERVICE, ACCOUNT)
    assert caught.value.code == "credential_unavailable"
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert VALUE not in rendered
    assert set(sdk.objects) == set(sdk.released)


@pytest.mark.parametrize(
    "result_type,length,result,null",
    [
        (1, None, b"valid", False),
        (7, -1, b"valid", False),
        (7, 0, b"", False),
        (7, 16385, b"valid", False),
        (7, None, b"\xff", False),
        (7, None, b"has\x00nul", False),
        (7, None, b"valid", True),
    ],
)
def test_malformed_read_output_rejected_and_released(sdk, result_type, length, result, null):
    sdk.result_type, sdk.result_length, sdk.result, sdk.result_null = result_type, length, result, null
    with pytest.raises(ContextError) as caught:
        module.MacOSKeychain().read(SERVICE, ACCOUNT)
    assert caught.value.code == "credential_unavailable"
    assert set(sdk.objects) == set(sdk.released)


def test_exact_value_byte_boundary(sdk):
    value = "x" * 16384
    module.MacOSKeychain().create(SERVICE, ACCOUNT, value)
    sdk.result = value.encode()
    assert module.MacOSKeychain().read(SERVICE, ACCOUNT) == value


def test_64_bit_darwin_callback_layout():
    assert C.sizeof(module._KeyCallbacks) == 48
    assert C.sizeof(module._ValueCallbacks) == 40
    assert module._KeyCallbacks.hash.offset == 40
    assert module._ValueCallbacks.equal.offset == 32
    assert C.sizeof(module._OS_STATUS) == 4
    assert C.sizeof(module._CF_INDEX) == 8


def test_framework_paths_absolute_and_no_query(monkeypatch):
    calls = []
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.C, "CDLL", lambda path: calls.append(path) or object())
    module._load_frameworks()
    assert calls == [module._SECURITY, module._CORE_FOUNDATION]


def test_non_mac_does_not_load_frameworks(monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.C, "CDLL", lambda path: pytest.fail("must not load"))
    with pytest.raises(ContextError, match="64 位 macOS") as caught:
        module.MacOSKeychain()
    assert caught.value.code == "unsupported_platform"


def test_missing_framework_or_symbol_is_redacted(monkeypatch):
    def fail():
        raise OSError(VALUE)

    monkeypatch.setattr(module, "_load_frameworks", fail)
    with pytest.raises(ContextError) as caught:
        module.MacOSKeychain()
    assert caught.value.code == "credential_unavailable"
    assert caught.value.__context__ is None
    assert VALUE not in str(caught.value)


@pytest.mark.parametrize("initial", [0, 1])
@pytest.mark.parametrize("operation", ["create", "read", "delete"])
def test_ui_original_setting_restored_even_sdk_exception(sdk, initial, operation):
    sdk.ui_state = initial
    sdk.raise_secret = True
    with pytest.raises(ContextError) as caught:
        keychain = module.MacOSKeychain()
        getattr(keychain, operation)(SERVICE, ACCOUNT, VALUE) if operation == "create" else getattr(
            keychain, operation
        )(SERVICE, ACCOUNT)
    assert sdk.ui_calls == ["get", 0, initial]
    assert sdk.ui_state == initial
    assert caught.value.__context__ is None
    assert not module._UI_GUARD_FAILED


@pytest.mark.parametrize("get_status,set_status,expected", [(-50, 0, ["get"]), (0, -50, ["get", 0, 1])])
def test_ui_guard_failure_never_calls_item(sdk, get_status, set_status, expected):
    sdk.get_status, sdk.set_status = get_status, set_status
    with pytest.raises(ContextError) as caught:
        module.MacOSKeychain().create(SERVICE, ACCOUNT, VALUE)
    assert caught.value.code == "credential_unavailable"
    assert sdk.calls == [] and sdk.ui_calls == expected
    assert set(sdk.objects) == set(sdk.released)


@pytest.mark.parametrize("sdk_raises", [False, True])
def test_ui_restore_failure_poison_prevents_later_operations(sdk, sdk_raises):
    sdk.restore_status = -50
    sdk.raise_secret = sdk_raises
    keychain = module.MacOSKeychain()
    with pytest.raises(ContextError) as caught:
        keychain.create(SERVICE, ACCOUNT, VALUE)
    assert caught.value.code == "credential_unavailable"
    assert caught.value.__context__ is None
    assert VALUE not in "".join(traceback.format_exception(caught.value))
    assert module._UI_GUARD_FAILED
    sdk.restore_status = 0
    for operation in ("read", "delete"):
        with pytest.raises(ContextError):
            getattr(keychain, operation)(SERVICE, ACCOUNT)
    assert len(sdk.calls) == 1 and sdk.ui_calls == ["get", 0, 1]
    assert set(sdk.objects) == set(sdk.released)


def test_ui_invalid_boolean_fails_without_item(sdk):
    sdk.ui_state = 2
    with pytest.raises(ContextError):
        module.MacOSKeychain().read(SERVICE, ACCOUNT)
    assert sdk.calls == [] and sdk.ui_calls == ["get"]


def test_ui_guard_serializes_instances_across_threads(sdk):
    first_entered = threading.Event()
    allow_first_exit = threading.Event()
    second_started = threading.Event()
    errors = []
    original = sdk.operation

    def paused(kind, query, output=None):
        if not first_entered.is_set():
            first_entered.set()
            assert allow_first_exit.wait(5)
        return original(kind, query, output)

    sdk.sec.SecItemDelete.callback = lambda q: paused("delete", q)
    a, b = module.MacOSKeychain(), module.MacOSKeychain()

    def run(client, start=None):
        try:
            if start is not None:
                start.set()
            client.delete(SERVICE, ACCOUNT)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=run, args=(a,))
    second = threading.Thread(target=run, args=(b, second_started))
    first.start()
    try:
        assert first_entered.wait(5)
        second.start()
        assert second_started.wait(5)
        assert sdk.ui_calls == ["get", 0]
    finally:
        allow_first_exit.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert sdk.ui_calls == ["get", 0, 1, "get", 0, 1]
    assert sdk.ui_state == 1
