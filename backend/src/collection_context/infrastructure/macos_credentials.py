"""Exact, non-interactive macOS credential operations; no shell or automatic fallback.

Original ctypes binding based on Apple's public SecItem.h / CoreFoundation APIs:
https://github.com/apple-oss-distributions/Security/blob/main/keychain/headers/SecItem.h
https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains
https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/SecKeychain.cpp
Legacy keychains can display UI despite per-query no-authentication options.
This adapter explicitly uses the legacy backend with a process-interaction guard.
All product calls share a lock; unrelated code must not change the process-wide
interaction setting concurrently. No lock state or ACL is changed. Construction
binds symbols only: it never queries, creates, or unlocks a keychain.
"""

from __future__ import annotations

import ctypes as C
import platform
import re
import threading
from typing import Any

from collection_context.application.contracts import ContextError

_SECURITY = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_SERVICE = re.compile(r"org\.collection-context\.credentials\.[0-9a-f]{32}\Z")
_ACCOUNT = re.compile(r"k_[0-9a-f]{32}\Z")
_MAX_VALUE_BYTES = 16_384
_CF_INDEX = C.c_int64
_CF_TYPE_ID = C.c_uint64
_OS_STATUS = C.c_int32
_PTR = C.c_void_p
_UTF8 = 0x08000100
_UI_LOCK = threading.RLock()
_UI_GUARD_FAILED = False


class _KeyCallbacks(C.Structure):
    _fields_ = [("version", _CF_INDEX)] + [
        (name, _PTR) for name in ("retain", "release", "copyDescription", "equal", "hash")
    ]


class _ValueCallbacks(C.Structure):
    _fields_ = [("version", _CF_INDEX)] + [
        (name, _PTR) for name in ("retain", "release", "copyDescription", "equal")
    ]


def _load_frameworks() -> tuple[Any, Any]:
    if platform.system() != "Darwin" or C.sizeof(_PTR) != 8:
        raise ContextError("unsupported_platform", "系统凭据库仅支持 64 位 macOS。")
    return C.CDLL(_SECURITY), C.CDLL(_CORE_FOUNDATION)


def _constant(library: Any, name: str) -> int:
    pointer = _PTR.in_dll(library, name).value
    if not pointer:
        raise ContextError("credential_unavailable", "系统凭据库暂不可用。")
    return pointer


def _callbacks(cf: Any) -> tuple[Any, Any]:
    return (
        _KeyCallbacks.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"),
        _ValueCallbacks.in_dll(cf, "kCFTypeDictionaryValueCallBacks"),
    )


def _bind(library: Any, name: str, arguments: list[Any], result: Any) -> Any:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _status_error(status: int) -> ContextError:
    if status == -25299:
        return ContextError("credential_conflict", "此凭据已存在，不能覆盖。")
    if status == -25300:
        return ContextError("credential_missing", "对应系统凭据不存在。")
    if status in {-25308, -25315, -25291}:
        return ContextError("credential_locked", "系统凭据库未就绪或需要用户在系统中解锁。")
    if status in {-25293, -25292, -34018, -34020, -128}:
        return ContextError("credential_denied", "系统拒绝凭据操作，请检查应用权限。")
    return ContextError("credential_unavailable", "系统凭据库操作未完成。")


class MacOSKeychain:
    """Create-only opaque product slots; instance representation contains no secrets."""

    def __init__(self) -> None:
        failure = None
        try:
            self._sec, self._cf = _load_frameworks()
            self._keys, self._values = _callbacks(self._cf)
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
            )
            self._constants = {name: _constant(self._sec, name) for name in names}
            self._true = _constant(self._cf, "kCFBooleanTrue")
            self._false = _constant(self._cf, "kCFBooleanFalse")
            self._string = _bind(
                self._cf, "CFStringCreateWithBytes", [_PTR, _PTR, _CF_INDEX, C.c_uint32, C.c_ubyte], _PTR
            )
            self._data = _bind(self._cf, "CFDataCreate", [_PTR, _PTR, _CF_INDEX], _PTR)
            self._dictionary = _bind(
                self._cf,
                "CFDictionaryCreate",
                [_PTR, C.POINTER(_PTR), C.POINTER(_PTR), _CF_INDEX, _PTR, _PTR],
                _PTR,
            )
            self._release = _bind(self._cf, "CFRelease", [_PTR], None)
            self._type = _bind(self._cf, "CFGetTypeID", [_PTR], _CF_TYPE_ID)
            self._data_type = _bind(self._cf, "CFDataGetTypeID", [], _CF_TYPE_ID)
            self._length = _bind(self._cf, "CFDataGetLength", [_PTR], _CF_INDEX)
            self._bytes = _bind(self._cf, "CFDataGetBytePtr", [_PTR], _PTR)
            self._add = _bind(self._sec, "SecItemAdd", [_PTR, C.POINTER(_PTR)], _OS_STATUS)
            self._copy = _bind(self._sec, "SecItemCopyMatching", [_PTR, C.POINTER(_PTR)], _OS_STATUS)
            self._delete = _bind(self._sec, "SecItemDelete", [_PTR], _OS_STATUS)
            self._get_ui = _bind(
                self._sec, "SecKeychainGetUserInteractionAllowed", [C.POINTER(C.c_ubyte)], _OS_STATUS
            )
            self._set_ui = _bind(self._sec, "SecKeychainSetUserInteractionAllowed", [C.c_ubyte], _OS_STATUS)
        except ContextError as error:
            failure = ContextError(error.code, error.message)
        except Exception:
            failure = ContextError("credential_unavailable", "系统凭据库暂不可用。")
        if failure is not None:
            raise failure

    def create(self, service: str, account: str, value: str) -> None:
        self._operation("create", service, account, value)

    def read(self, service: str, account: str) -> str:
        result = self._operation("read", service, account)
        if result is None:
            raise ContextError("credential_unavailable", "系统凭据库未返回有效内容。")
        return result

    def delete(self, service: str, account: str) -> None:
        self._operation("delete", service, account)

    def _operation(self, operation: str, service: str, account: str, value: str | None = None) -> str | None:
        failure = None
        result = None
        try:
            if (
                type(service) is not str
                or type(account) is not str
                or not _SERVICE.fullmatch(service)
                or not _ACCOUNT.fullmatch(account)
            ):
                raise ContextError("invalid_credential_reference", "只接受产品的固定凭据引用。")
            encoded = None
            if operation == "create":
                if type(value) is not str or not value or len(value) > _MAX_VALUE_BYTES or "\x00" in value:
                    raise ContextError("invalid_credential", "凭据内容无效或过长。")
                try:
                    encoded = value.encode("utf-8")
                except UnicodeError:
                    encoded = None
                if encoded is None:
                    raise ContextError("invalid_credential", "凭据内容编码无效。")
                if len(encoded) > _MAX_VALUE_BYTES:
                    raise ContextError("invalid_credential", "凭据内容无效或过长。")
            result = self._execute(operation, service, account, encoded)
        except ContextError as error:
            failure = ContextError(error.code, error.message)
        except Exception:
            failure = ContextError("credential_unavailable", "系统凭据库操作未完成。")
        if failure is not None:
            raise failure
        return result

    def _execute(self, operation: str, service: str, account: str, encoded: bytes | None) -> str | None:
        owned: list[int] = []
        output = _PTR()
        buffer = None
        try:

            def string(text: str) -> int:
                raw = text.encode("utf-8")
                pointer = self._string(None, raw, len(raw), _UTF8, 0)
                if not pointer:
                    raise ContextError("credential_unavailable", "系统凭据库暂不可用。")
                owned.append(pointer)
                return int(pointer)

            constants = self._constants
            pairs = [
                (constants["kSecClass"], constants["kSecClassGenericPassword"]),
                (constants["kSecAttrService"], string(service)),
                (constants["kSecAttrAccount"], string(account)),
                (constants["kSecAttrSynchronizable"], self._false),
                (constants["kSecUseDataProtectionKeychain"], self._false),
                (constants["kSecUseAuthenticationUI"], constants["kSecUseAuthenticationUIFail"]),
            ]
            if encoded is not None:
                buffer = C.create_string_buffer(encoded, len(encoded))
                pointer = self._data(None, buffer, len(encoded))
                if not pointer:
                    raise ContextError("credential_unavailable", "系统凭据库暂不可用。")
                owned.append(pointer)
                pairs.append((constants["kSecValueData"], pointer))
            elif operation == "read":
                pairs.extend(
                    [
                        (constants["kSecReturnData"], self._true),
                        (constants["kSecMatchLimit"], constants["kSecMatchLimitOne"]),
                    ]
                )
            keys = (_PTR * len(pairs))(*(key for key, _ in pairs))
            values = (_PTR * len(pairs))(*(value for _, value in pairs))
            query = self._dictionary(
                None, keys, values, len(pairs), C.byref(self._keys), C.byref(self._values)
            )
            if not query:
                raise ContextError("credential_unavailable", "系统凭据库暂不可用。")
            owned.append(query)
            status = self._guarded_item(operation, query, output)
            if status != 0:
                raise _status_error(status)
            if operation != "read":
                return None
            if not output.value or self._type(output) != self._data_type():
                raise ContextError("credential_unavailable", "系统凭据库未返回有效内容。")
            length = int(self._length(output))
            if not 0 < length <= _MAX_VALUE_BYTES:
                raise ContextError("credential_unavailable", "系统凭据库未返回有效内容。")
            pointer = self._bytes(output)
            if not pointer:
                raise ContextError("credential_unavailable", "系统凭据库未返回有效内容。")
            value = C.string_at(pointer, length).decode("utf-8")
            if "\x00" in value:
                raise ContextError("credential_unavailable", "系统凭据库未返回有效内容。")
            return value
        finally:
            if output.value:
                self._release(output)
            for pointer in reversed(owned):
                self._release(pointer)
            if buffer is not None:
                C.memset(C.addressof(buffer), 0, C.sizeof(buffer))

    def _guarded_item(self, operation: str, query: int, output: Any) -> int:
        """Serialize the process-wide legacy UI switch; never unlock or authorize.

        A failed restore poisons further calls for this process. Retrying a create
        after any unknown result is unsafe: it may already have added the item.
        """
        global _UI_GUARD_FAILED
        with _UI_LOCK:
            if _UI_GUARD_FAILED:
                raise ContextError("credential_unavailable", "系统凭据交互状态未确认，请重启应用后再检查。")
            original = C.c_ubyte(0)
            status = int(self._get_ui(C.byref(original)))
            if status != 0:
                raise _status_error(status)
            if original.value not in (0, 1):
                raise ContextError("credential_unavailable", "系统凭据交互状态无效。")
            try:
                status = int(self._set_ui(0))
                if status != 0:
                    raise _status_error(status)
                if operation == "create":
                    return int(self._add(query, None))
                if operation == "read":
                    return int(self._copy(query, C.byref(output)))
                return int(self._delete(query))
            finally:
                try:
                    restored = int(self._set_ui(original.value)) == 0
                except Exception:
                    restored = False
                if not restored:
                    _UI_GUARD_FAILED = True
                    raise ContextError(
                        "credential_unavailable", "系统凭据交互状态未恢复，请重启应用后再检查。"
                    ) from None
