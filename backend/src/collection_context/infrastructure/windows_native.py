"""Unconnected Windows x64 primitives, NOT an accepted SafeFiles/lease backend.

Only existing local-drive objects can be opened. No writes, installation,
arbitrary access masks, or fallback path IO are provided. DLLs load lazily on
real Windows x64; ``_dlls`` injection is for call/layout tests, never acceptance.
Private ACL validation remains explicitly unsupported.

Native contracts (original implementation; no copied external implementation):
https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile
https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes
https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55

The caller owns sequencing: handles are not transferable between adapters or
to subprocesses. This draft does not implement a complete race-safe path
facade, root reattachment checks, private ACL checks, read/write, or recovery.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from collection_context.application.contracts import ContextError

# Windows LLP64, including UTF-16 WCHAR, regardless of the test host's C ABI.
U16 = ctypes.c_uint16
U32 = ctypes.c_uint32
U64 = ctypes.c_uint64
I32 = ctypes.c_int32
I64 = ctypes.c_int64
HANDLE = ctypes.c_void_p


class UnicodeString(ctypes.Structure):
    _fields_ = [("Length", U16), ("MaximumLength", U16), ("Buffer", ctypes.POINTER(U16))]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", U32),
        ("RootDirectory", HANDLE),
        ("ObjectName", ctypes.POINTER(UnicodeString)),
        ("Attributes", U32),
        ("SecurityDescriptor", HANDLE),
        ("SecurityQualityOfService", HANDLE),
    ]


class _StatusUnion(ctypes.Union):
    _fields_ = [("Status", I32), ("Pointer", HANDLE)]


class IoStatusBlock(ctypes.Structure):
    _fields_ = [("Result", _StatusUnion), ("Information", U64)]


class FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", U64), ("FileId", ctypes.c_ubyte * 16)]


class FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", I64),
        ("EndOfFile", I64),
        ("NumberOfLinks", U32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", I64),
        ("LastAccessTime", I64),
        ("LastWriteTime", I64),
        ("ChangeTime", I64),
        ("FileAttributes", U32),
    ]


class FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", U32), ("ReparseTag", U32)]


class _Offsets(ctypes.Structure):
    _fields_ = [("Offset", U32), ("OffsetHigh", U32)]


class _OffsetUnion(ctypes.Union):
    _fields_ = [("Offsets", _Offsets), ("Pointer", HANDLE)]


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", U64), ("InternalHigh", U64), ("Position", _OffsetUnion), ("hEvent", HANDLE)]


Role = Literal["directory", "read_file", "lease_file", "lease_observer"]
_ROLES = frozenset({"directory", "read_file", "lease_file", "lease_observer"})
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_DIRECTORY_FILE = 0x1
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_READ_ATTRIBUTES = 0x80
_READ_CONTROL = 0x00020000
_SYNCHRONIZE = 0x00100000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_INVALID_HANDLE = (1 << 64) - 1
# Existing metadata is bounded to 65,536 bytes. A lock outside EOF lets a
# read-only observer inspect metadata without reading an exclusive region.
LEASE_LOCK_OFFSET = 1 << 20
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


@dataclass(frozen=True)
class FileIdentity:
    volume_serial: int
    file_id: bytes


@dataclass(frozen=True)
class FileInformation:
    identity: FileIdentity
    size: int
    allocation_size: int
    links: int
    directory: bool
    delete_pending: bool
    attributes: int
    reparse_tag: int
    creation_time: int
    last_write_time: int
    change_time: int


@dataclass(frozen=True)
class _DLLs:
    kernel32: Any
    ntdll: Any
    last_error: Callable[[], int]


def _unsupported() -> ContextError:
    return ContextError("unsupported_platform", "Windows 原生适配尚未具备此安全能力；不会降级。")


def validate_component(component: str) -> str:
    """Accept one ordinary Windows filename, never a path/stream/device alias."""
    if (
        not isinstance(component, str)
        or not component
        or component in {".", ".."}
        or component[-1] in " ."
        or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in component)
        or component.split(".", 1)[0].rstrip(" ").upper() in _RESERVED
    ):
        raise ContextError("forbidden_path", "文件名称不是允许的单一目录组件。")
    try:
        encoded = component.encode("utf-16-le", errors="strict")
    except UnicodeError:
        raise ContextError("forbidden_path", "文件名称不是有效的 UTF-16。") from None
    if len(encoded) > 510:
        raise ContextError("forbidden_path", "文件名称超过受控长度。")
    return component


def _root_name(path: str) -> str:
    # Deliberately no UNC, device paths, drive-relative paths, normalization,
    # environment expansion, or arbitrary NT object-manager namespaces.
    if not isinstance(path, str) or len(path) > 1024 or not re.match(r"^[A-Za-z]:\\", path):
        raise ContextError("forbidden_path", "这里只接受绝对本地盘目录路径。")
    tail = path[3:]
    if tail:
        for component in tail.split("\\"):
            validate_component(component)
    return "\\??\\" + path


def _utf16_name(value: str) -> tuple[Any, UnicodeString]:
    raw = value.encode("utf-16-le", errors="strict")
    if len(raw) > 65532:
        raise ContextError("forbidden_path", "原生文件名称超过受控长度。")
    buffer = (U16 * (len(raw) // 2 + 1)).from_buffer_copy(raw + b"\x00\x00")
    return buffer, UnicodeString(len(raw), len(raw) + 2, buffer)


def _check_layout() -> None:
    expected = {
        UnicodeString: 16,
        ObjectAttributes: 48,
        IoStatusBlock: 16,
        FileIdInfo: 24,
        FileStandardInfo: 24,
        FileBasicInfo: 40,
        FileAttributeTagInfo: 8,
        Overlapped: 32,
    }
    offsets = (
        UnicodeString.Buffer.offset == 8,
        ObjectAttributes.RootDirectory.offset == 8,
        ObjectAttributes.ObjectName.offset == 16,
        ObjectAttributes.Attributes.offset == 24,
        ObjectAttributes.SecurityDescriptor.offset == 32,
        ObjectAttributes.SecurityQualityOfService.offset == 40,
        IoStatusBlock.Information.offset == 8,
        FileIdInfo.FileId.offset == 8,
        FileStandardInfo.NumberOfLinks.offset == 16,
        FileBasicInfo.FileAttributes.offset == 32,
        Overlapped.Position.offset == 16,
        Overlapped.hEvent.offset == 24,
    )
    if (
        ctypes.sizeof(HANDLE) != 8
        or any(ctypes.sizeof(kind) != size for kind, size in expected.items())
        or not all(offsets)
    ):
        raise _unsupported()


class NativeHandle:
    """Owned non-inheritable HANDLE. Do not expose/transfer it to subprocesses."""

    def __init__(self, owner: WindowsNative, value: int, role: Role):
        self._owner = owner
        self._value = value
        self._role = role
        self._identity: FileIdentity | None = None
        self._locks = 0

    @property
    def closed(self) -> bool:
        return self._value == 0

    @property
    def role(self) -> Role:
        return self._role

    @property
    def identity(self) -> FileIdentity:
        self._owner._value(self)
        if self._identity is None:
            raise ContextError("storage_unavailable", "文件身份尚未建立。")
        return self._identity

    def close(self) -> None:
        if self.closed:
            return
        value, self._value = self._value, 0
        if not self._owner._libraries().kernel32.CloseHandle(HANDLE(value)):
            raise ContextError("storage_unavailable", "原生文件句柄关闭失败。")

    def __enter__(self) -> NativeHandle:
        self._owner._value(self)
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class NativeLock:
    """One nonblocking kernel range lock; does not own the file HANDLE."""

    def __init__(self, handle: NativeHandle, overlap: Overlapped, identity: FileIdentity):
        self._handle = handle
        self._overlap = overlap
        self.identity = identity
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        # CloseHandle already releases the kernel lock; never unlock a reused HANDLE.
        if not self._handle.closed:
            owner = self._handle._owner
            if not owner._libraries().kernel32.UnlockFileEx(
                HANDLE(owner._value(self._handle)), 0, 1, 0, ctypes.byref(self._overlap)
            ):
                raise ContextError("storage_unavailable", "原生所有权锁释放失败。")
        self.closed = True
        self._handle._locks = 0

    def __enter__(self) -> NativeLock:
        if self.closed:
            raise ContextError("lock_changed", "原生所有权锁已经关闭。")
        self._handle._owner._value(self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WindowsNative:
    """Low-level x64 draft, intentionally not selected by platform_safety."""

    def __init__(self, *, _dlls: _DLLs | None = None):
        self._dlls = _dlls
        self._bound = False

    def _libraries(self) -> _DLLs:
        _check_layout()
        if self._dlls is None:
            if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
                raise _unsupported()
            try:
                loader = getattr(ctypes, "WinDLL")
                # Search only the system DLL directory, never CWD/PATH/user DLLs.
                self._dlls = _DLLs(
                    loader("kernel32.dll", use_last_error=True, winmode=0x800),
                    loader("ntdll.dll", use_last_error=True, winmode=0x800),
                    getattr(ctypes, "get_last_error"),
                )
            except (AttributeError, OSError):
                raise _unsupported() from None
        if not self._bound:
            self._bind(self._dlls)
            self._bound = True
        return self._dlls

    @staticmethod
    def _bind(dlls: _DLLs) -> None:
        signatures = [
            (
                dlls.ntdll,
                "NtCreateFile",
                I32,
                [
                    ctypes.POINTER(HANDLE),
                    U32,
                    ctypes.POINTER(ObjectAttributes),
                    ctypes.POINTER(IoStatusBlock),
                    ctypes.POINTER(I64),
                    U32,
                    U32,
                    U32,
                    U32,
                    HANDLE,
                    U32,
                ],
            ),
            (dlls.kernel32, "SetHandleInformation", I32, [HANDLE, U32, U32]),
            (dlls.kernel32, "CloseHandle", I32, [HANDLE]),
            (dlls.kernel32, "GetFileType", U32, [HANDLE]),
            (dlls.kernel32, "GetDriveTypeW", U32, [ctypes.POINTER(U16)]),
            (dlls.kernel32, "GetFileInformationByHandleEx", I32, [HANDLE, U32, HANDLE, U32]),
            (dlls.kernel32, "LockFileEx", I32, [HANDLE, U32, U32, U32, U32, ctypes.POINTER(Overlapped)]),
            (dlls.kernel32, "UnlockFileEx", I32, [HANDLE, U32, U32, U32, ctypes.POINTER(Overlapped)]),
        ]
        try:
            for dll, name, result, arguments in signatures:
                function = getattr(dll, name)
                function.argtypes = arguments
                function.restype = result
        except AttributeError:
            raise _unsupported() from None

    def _value(self, handle: NativeHandle) -> int:
        if not isinstance(handle, NativeHandle) or handle._owner is not self or handle.closed:
            raise ContextError("storage_unavailable", "文件句柄无效、已关闭或不属于此适配器。")
        return handle._value

    @staticmethod
    def _nt_error(status: int) -> ContextError:
        unsigned = int(status) & 0xFFFFFFFF
        if unsigned in {0xC0000034, 0xC000003A, 0xC000000F}:
            return ContextError("not_found", "受控文件不存在。")
        if unsigned in {0xC000050B, 0xC0000279, 0x8000002D, 0xC0000022}:
            return ContextError("forbidden_path", "原生文件边界或访问检查拒绝。")
        if unsigned == 0xC0000043:
            return ContextError("version_changed", "文件正由其他操作修改或替换。", retryable=True)
        return ContextError("storage_unavailable", "原生文件打开失败；不会回退普通路径。")

    def open_root_directory(self, path: str) -> NativeHandle:
        name = _root_name(path)
        drive_buffer, drive_name = _utf16_name(path[:3])
        # Drive-letter spelling alone does not exclude mapped network drives.
        # Unknown/unavailable/remote/optical/RAM drive types are not claimed.
        drive_type = self._libraries().kernel32.GetDriveTypeW(drive_name.Buffer)
        del drive_buffer
        if drive_type not in {2, 3}:  # DRIVE_REMOVABLE, DRIVE_FIXED
            raise ContextError("storage_unavailable", "这里只接受可用的本地固定或可移动磁盘。")
        return self._open(name, None, "directory")

    def open_relative(self, parent: NativeHandle, component: str, *, role: Role) -> NativeHandle:
        validate_component(component)
        self._value(parent)
        if parent.role != "directory" or not isinstance(role, str) or role not in _ROLES:
            raise ContextError("forbidden_path", "目录句柄或固定访问角色不匹配。")
        self.information(parent)
        return self._open(component, parent, role)

    def _open(self, name: str, parent: NativeHandle | None, role: Role) -> NativeHandle:
        dlls = self._libraries()
        buffer, string = _utf16_name(name)
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            self._value(parent) if parent is not None else None,
            ctypes.pointer(string),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            None,
            None,
        )
        access = _SYNCHRONIZE | _READ_CONTROL | _FILE_READ_ATTRIBUTES
        access |= 0x20 if role == "directory" else 0x1  # TRAVERSE / READ_DATA
        if role == "lease_file":
            access |= 0x2  # WRITE_DATA, never arbitrary access from a caller
        shares = {"directory": 3, "read_file": 1, "lease_file": 7, "lease_observer": 7}[role]
        options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
        options |= _FILE_DIRECTORY_FILE if role == "directory" else _FILE_NON_DIRECTORY_FILE
        output, io = HANDLE(), IoStatusBlock()
        status = dlls.ntdll.NtCreateFile(
            ctypes.byref(output),
            access,
            ctypes.byref(attributes),
            ctypes.byref(io),
            None,
            0,
            shares,
            1,
            options,
            None,
            0,  # FILE_OPEN only; never create/truncate
        )
        # Keep the explicit UTF-16 backing buffer alive through the native call.
        del buffer
        value = output.value
        if status != 0:
            if value not in (None, 0, _INVALID_HANDLE):
                dlls.kernel32.CloseHandle(output)
            raise self._nt_error(status)
        if value in (None, 0, _INVALID_HANDLE):
            raise ContextError("storage_unavailable", "原生打开没有返回有效句柄。")
        handle = NativeHandle(self, value, role)
        try:
            if not dlls.kernel32.SetHandleInformation(output, 1, 0):
                raise ContextError("storage_unavailable", "无法清除文件句柄继承标志。")
            handle._identity = self.information(handle).identity
            return handle
        except BaseException:
            handle.close()
            raise

    def information(self, handle: NativeHandle) -> FileInformation:
        value = HANDLE(self._value(handle))
        dlls = self._libraries()
        if dlls.kernel32.GetFileType(value) != 1:  # FILE_TYPE_DISK only
            raise ContextError("forbidden_path", "对象不是磁盘文件。")
        values: list[Any] = []
        for kind, structure in (
            (18, FileIdInfo),
            (1, FileStandardInfo),
            (0, FileBasicInfo),
            (9, FileAttributeTagInfo),
        ):
            record = structure()
            if not dlls.kernel32.GetFileInformationByHandleEx(
                value, kind, ctypes.byref(record), ctypes.sizeof(record)
            ):
                raise ContextError("storage_unavailable", "原生文件安全信息不可用。")
            values.append(record)
        identity, standard, basic, tags = values
        directory = bool(standard.Directory)
        if (
            (tags.FileAttributes | basic.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT
            or tags.ReparseTag
            or directory != (handle.role == "directory")
            or standard.DeletePending
            or standard.EndOfFile < 0
            or standard.AllocationSize < 0
            or (not directory and standard.NumberOfLinks != 1)
            or not any(identity.FileId)
        ):
            raise ContextError("forbidden_path", "对象类型、重解析点或硬链接检查拒绝。")
        result = FileInformation(
            FileIdentity(int(identity.VolumeSerialNumber), bytes(identity.FileId)),
            int(standard.EndOfFile),
            int(standard.AllocationSize),
            int(standard.NumberOfLinks),
            directory,
            bool(standard.DeletePending),
            int(tags.FileAttributes),
            int(tags.ReparseTag),
            int(basic.CreationTime),
            int(basic.LastWriteTime),
            int(basic.ChangeTime),
        )
        if handle._identity is not None and result.identity != handle._identity:
            raise ContextError("lock_changed", "原生文件句柄身份发生变化。")
        return result

    def try_lock(self, handle: NativeHandle, *, exclusive: bool) -> NativeLock:
        self._value(handle)
        if type(exclusive) is not bool or handle.role not in {"lease_file", "lease_observer"}:
            raise ContextError("forbidden_path", "只有固定所有权角色可以获取文件锁。")
        if (handle.role == "lease_observer" and exclusive) or handle._locks:
            raise ContextError("forbidden_path", "只读观察者不能排他锁定，且同一句柄只允许一个锁。")
        before = self.information(handle)
        if before.size > 65536:
            raise ContextError("forbidden_path", "所有权元数据超过限制。")
        overlap = Overlapped()
        overlap.Position.Offsets.Offset = LEASE_LOCK_OFFSET
        flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY, optionally EXCLUSIVE
        dlls = self._libraries()
        if not dlls.kernel32.LockFileEx(HANDLE(self._value(handle)), flags, 0, 1, 0, ctypes.byref(overlap)):
            if dlls.last_error() in {32, 33}:
                raise ContextError("lock_busy", "已有执行者持有原生文件锁。", retryable=True)
            raise ContextError("storage_unavailable", "原生文件锁获取失败。")
        handle._locks = 1
        lock = NativeLock(handle, overlap, before.identity)
        try:
            if self.information(handle).identity != before.identity:
                raise ContextError("lock_changed", "获取锁时文件身份发生变化。")
            return lock
        except BaseException:
            lock.close()
            raise

    def require_private_security(self, handle: NativeHandle) -> None:
        self._value(handle)
        # READ_CONTROL access does not establish safe ownership/DACL. The next
        # adapter batch must implement and natively verify those checks.
        raise _unsupported()
