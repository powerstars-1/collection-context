"""Unconnected Windows x64 primitives, NOT an accepted SafeFiles/lease backend.

Existing local-drive objects can be opened and bounded file contents read.
Existing lease metadata can be written under its own exclusive range lock; this
is not atomic library publication. A separate draft publication adapter creates
private staging files and publishes verified contents. No installation, arbitrary
access masks, or fallback path IO are provided. DLLs load lazily on
real Windows x64; ``_dlls`` injection is for call/layout tests, never acceptance.
Private ACL checks are a separate lazy adapter; neither draft selects Windows
as an accepted public backend.

Native contracts (original implementation; no copied external implementation):
https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile
https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes
https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex
https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_extd_dir_info
https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ne-minwinbase-file_info_by_handle_class
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/36172f0b-8dce-435a-8748-859978d632f8
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-readfile
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfilepointerex
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setendoffile
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-querydosdevicew
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55

The caller owns sequencing: handles are not transferable between adapters or
to subprocesses. This draft does not implement a complete race-safe path
facade, root reattachment checks, atomic writes, or recovery.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import re
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from collection_context.application.contracts import ContextError

if TYPE_CHECKING:
    from collection_context.infrastructure.windows_security import PrivateCreation, WindowsPrivateSecurity

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


class FileIdExtdDirectoryHeader(ctypes.Structure):
    """Fixed prefix only; FileName follows at byte 88 without a terminator."""

    _fields_ = [
        ("NextEntryOffset", U32),
        ("FileIndex", U32),
        ("CreationTime", I64),
        ("LastAccessTime", I64),
        ("LastWriteTime", I64),
        ("ChangeTime", I64),
        ("EndOfFile", I64),
        ("AllocationSize", I64),
        ("FileAttributes", U32),
        ("FileNameLength", U32),
        ("EaSize", U32),
        ("ReparsePointTag", U32),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class _Offsets(ctypes.Structure):
    _fields_ = [("Offset", U32), ("OffsetHigh", U32)]


class _OffsetUnion(ctypes.Union):
    _fields_ = [("Offsets", _Offsets), ("Pointer", HANDLE)]


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", U64), ("InternalHigh", U64), ("Position", _OffsetUnion), ("hEvent", HANDLE)]


Role = Literal[
    "directory",
    "directory_listing",
    "read_file",
    "lease_file",
    "lease_observer",
    "publication_file",
    "metadata",
    "delete_file",
]
# Publication handles can only originate in exclusive private creation. They
# cannot be requested by open_relative for an arbitrary existing file.
_ROLES = frozenset(
    {"directory", "directory_listing", "read_file", "lease_file", "lease_observer", "metadata", "delete_file"}
)
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
MAX_NATIVE_READ = 16_000_000
_READ_CHUNK = 65_536
_DIRECTORY_BUFFER = 65_536
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
        FileIdExtdDirectoryHeader: 88,
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
        FileIdExtdDirectoryHeader.FileAttributes.offset == 56,
        FileIdExtdDirectoryHeader.FileNameLength.offset == 60,
        FileIdExtdDirectoryHeader.FileId.offset == 72,
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
        self._active_lock: NativeLock | None = None
        self._io_lock = threading.RLock()

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
        with self._io_lock:
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

    def __init__(self, handle: NativeHandle, overlap: Overlapped, identity: FileIdentity, *, exclusive: bool):
        self._handle = handle
        self._overlap = overlap
        self.identity = identity
        self._exclusive = exclusive
        self.closed = False

    @property
    def exclusive(self) -> bool:
        return self._exclusive

    def close(self) -> None:
        with self._handle._io_lock:
            if self.closed:
                return
            # Invalidate local write authority even when the unlock outcome is unknown.
            self._handle._active_lock = None
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
        self._security: WindowsPrivateSecurity | None = None

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
            (dlls.kernel32, "QueryDosDeviceW", U32, [ctypes.POINTER(U16), ctypes.POINTER(U16), U32]),
            (dlls.kernel32, "GetFileInformationByHandleEx", I32, [HANDLE, U32, HANDLE, U32]),
            (dlls.kernel32, "SetFilePointerEx", I32, [HANDLE, I64, ctypes.POINTER(I64), U32]),
            (dlls.kernel32, "ReadFile", I32, [HANDLE, HANDLE, U32, ctypes.POINTER(U32), HANDLE]),
            (dlls.kernel32, "WriteFile", I32, [HANDLE, HANDLE, U32, ctypes.POINTER(U32), HANDLE]),
            (dlls.kernel32, "SetEndOfFile", I32, [HANDLE]),
            (dlls.kernel32, "FlushFileBuffers", I32, [HANDLE]),
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
        if unsigned == 0xC0000035:  # STATUS_OBJECT_NAME_COLLISION
            return ContextError("write_conflict", "目标已存在，未覆盖原文件。")
        if unsigned in {0xC000050B, 0xC0000279, 0x8000002D, 0xC0000022}:
            return ContextError("forbidden_path", "原生文件边界或访问检查拒绝。")
        if unsigned == 0xC0000043:
            return ContextError("version_changed", "文件正由其他操作修改或替换。", retryable=True)
        return ContextError("storage_unavailable", "原生文件打开失败；不会回退普通路径。")

    def _drive_target(self, path: str) -> str:
        drive_buffer, drive_name = _utf16_name(path[:3])
        # Drive-letter spelling alone does not exclude mapped network drives.
        # Unknown/unavailable/remote/optical/RAM drive types are not claimed.
        drive_type = self._libraries().kernel32.GetDriveTypeW(drive_name.Buffer)
        del drive_buffer
        if drive_type not in {2, 3}:  # DRIVE_REMOVABLE, DRIVE_FIXED
            raise ContextError("storage_unavailable", "这里只接受可用的本地固定或可移动磁盘。")
        device_buffer, device_name = _utf16_name(path[:2])
        target = (U16 * 1024)()
        count = self._libraries().kernel32.QueryDosDeviceW(device_name.Buffer, target, len(target))
        del device_buffer
        if not 2 <= count <= len(target):
            raise ContextError("storage_unavailable", "盘符映射不能在受控范围确认。")
        try:
            text = bytes(target)[: count * 2].decode("utf-16-le", errors="strict")
        except UnicodeError:
            raise ContextError("storage_unavailable", "盘符映射不是有效的本地设备。") from None
        if not text.endswith("\x00\x00") or not all(text[:-2].split("\x00")):
            raise ContextError("storage_unavailable", "盘符映射结构不完整。")
        current = text.split("\x00", 1)[0]
        # Query only the requested drive. Never enumerate or resolve SUBST/UNC,
        # arbitrary DOS aliases, object-manager links, or previous mappings.
        if not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]{1,20}", current, flags=re.IGNORECASE):
            raise ContextError("forbidden_path", "盘符不是允许的直接本地卷映射。")
        return current

    def open_root_directory(self, path: str) -> NativeHandle:
        return self._open_drive_directory(path, "directory")

    def open_root_listing(self, path: str) -> NativeHandle:
        """Separate bounded-listing authority for an explicitly selected root."""
        return self._open_drive_directory(path, "directory_listing")

    def _open_drive_directory(
        self, path: str, role: Literal["directory", "directory_listing"]
    ) -> NativeHandle:
        _root_name(path)  # Validate the caller spelling, do not open DOS aliases.
        target = self._drive_target(path)
        # DOS drive letters are namespace junctions. Resolve the bounded current
        # mapping first; preserve OBJ_DONT_REPARSE for the actual device path.
        handle = self._open(target + path[2:], None, role)
        try:
            if self._drive_target(path) != target:
                raise ContextError("storage_unavailable", "打开期间盘符映射变化；未继续操作。")
            self.information(handle)
            return handle
        except BaseException:
            handle.close()
            raise

    def create_directory(self, parent: NativeHandle, component: str) -> NativeHandle:
        """Exclusive child creation with explicit protected private inheritance.

        Parent spelling/attachment is the facade's responsibility; this method
        only operates through the owned directory HANDLE. Existing collisions
        never open, repair or replace an object implicitly.
        """
        validate_component(component)
        self._value(parent)
        if parent.role != "directory":
            raise ContextError("forbidden_path", "目录创建需要本适配器拥有的目录句柄。")
        with parent._io_lock:
            self.information(parent)
            security = self.private_security()
            descriptor = security.creation_descriptor(directory=True)
            self.information(parent)
            handle = self._open(component, parent, "directory", _creation=descriptor)
            try:
                if security.current_user() != descriptor.user:
                    raise ContextError("storage_unavailable", "创建目录时运行身份变化。")
                self.require_private_security(handle)
                self.information(parent)
                return handle
            except BaseException:
                handle.close()
                raise

    def create_lease_file(self, parent: NativeHandle, component: str) -> NativeHandle:
        """Exclusive private bootstrap only; not ownership or metadata write authority."""
        validate_component(component)
        self._value(parent)
        if parent.role != "directory":
            raise ContextError("forbidden_path", "所有权文件创建需要受控目录句柄。")
        with parent._io_lock:
            self.information(parent)
            self.require_private_security(parent)
            security = self.private_security()
            descriptor = security.creation_descriptor(directory=False)
            handle = self._open(component, parent, "lease_file", _creation=descriptor)
            try:
                if security.current_user() != descriptor.user:
                    raise ContextError("storage_unavailable", "创建所有权文件时运行身份变化。")
                self.require_private_security(handle)
                if self.information(handle).size != 0:
                    raise ContextError("storage_unavailable", "新所有权文件不是空文件。")
                self.information(parent)
                return handle
            except BaseException:
                handle.close()
                raise

    def open_relative(self, parent: NativeHandle, component: str, *, role: Role) -> NativeHandle:
        validate_component(component)
        self._value(parent)
        if (
            parent.role not in {"directory", "directory_listing"}
            or not isinstance(role, str)
            or role not in _ROLES
        ):
            raise ContextError("forbidden_path", "目录句柄或固定访问角色不匹配。")
        self.information(parent)
        return self._open(component, parent, role)

    def _open(
        self,
        name: str,
        parent: NativeHandle | None,
        role: Role,
        *,
        _creation: PrivateCreation | None = None,
    ) -> NativeHandle:
        dlls = self._libraries()
        buffer, string = _utf16_name(name)
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            self._value(parent) if parent is not None else None,
            ctypes.pointer(string),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            _creation.pointer if _creation is not None else None,
            None,
        )
        access = _SYNCHRONIZE | _READ_CONTROL | _FILE_READ_ATTRIBUTES
        if role not in {"metadata", "delete_file"}:
            access |= 0x20 if role in {"directory", "directory_listing"} else 0x1  # TRAVERSE / READ_DATA
        if role == "directory_listing":
            access |= 0x1  # FILE_LIST_DIRECTORY; ordinary traversal handles keep their existing rights.
        if role in {"lease_file", "publication_file"}:
            # SetEndOfFile/FlushFileBuffers require GENERIC_WRITE, not just
            # WRITE_DATA. Neither role requests WRITE_DAC; lease has no DELETE.
            access |= 0x40000000
        if role == "lease_observer":
            # LockFileEx requires GENERIC_READ or GENERIC_WRITE. Observers
            # obtain only GENERIC_READ, never metadata-write/delete authority.
            access |= 0x80000000
        if role == "delete_file":
            access |= 0x10000  # DELETE plus metadata only; no body read/write.
        if role == "publication_file":
            if _creation is None or parent is None:
                raise ContextError("forbidden_path", "发布句柄只能由私有排他创建获得。")
            access |= 0x10000  # DELETE for handle-relative rename, never WRITE_DAC.
        elif _creation is not None and (role not in {"directory", "lease_file"} or parent is None):
            raise ContextError("forbidden_path", "创建权限与固定文件角色不匹配。")
        shares = {
            "directory": 3,
            "directory_listing": 3,
            "read_file": 1,
            "lease_file": 3,
            "lease_observer": 3,
            "publication_file": 0,
            "metadata": 3,
            "delete_file": 0,
        }[role]
        options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
        if role != "metadata":
            options |= (
                _FILE_DIRECTORY_FILE
                if role in {"directory", "directory_listing"}
                else _FILE_NON_DIRECTORY_FILE
            )
        output, io = HANDLE(), IoStatusBlock()
        status = dlls.ntdll.NtCreateFile(
            ctypes.byref(output),
            access,
            ctypes.byref(attributes),
            ctypes.byref(io),
            None,
            0,
            shares,
            2 if _creation is not None else 1,
            options,
            None,
            0,  # FILE_CREATE is exclusive; never OPEN_IF/OVERWRITE/SUPERSEDE.
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
            if _creation is not None and (io.Information != 2 or io.Result.Status != 0):
                raise ContextError("storage_unavailable", "原生排他创建结果不能确认。")
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
            or (
                handle.role != "metadata" and directory != (handle.role in {"directory", "directory_listing"})
            )
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

    def list_directory(self, handle: NativeHandle, *, max_entries: int = 10_000) -> list[dict[str, str]]:
        """Bounded enumeration through an owned listing HANDLE, with no path fallback.

        The OS cursor belongs to this handle and is serialized through its IO
        lock. A fixed aligned buffer is restarted once and then continued until
        ERROR_NO_MORE_FILES. Errors never return a truncated success. This is a
        checked observation, not an atomic directory snapshot or an IO deadline.
        """
        if type(max_entries) is not int or not 1 <= max_entries <= 100_000:
            raise ContextError("invalid_argument", "目录条目上限须为1至100000的整数。")
        self._value(handle)
        if handle.role != "directory_listing":
            raise ContextError("forbidden_path", "目录枚举需要专用只读枚举句柄。")
        with handle._io_lock:
            before = self.information(handle)
            dlls = self._libraries()
            restart = True
            seen: set[str] = set()
            result: list[dict[str, str]] = []
            while True:
                # U64 allocation establishes the required 8-byte alignment.
                buffer = (U64 * (_DIRECTORY_BUFFER // 8))()
                if not dlls.kernel32.GetFileInformationByHandleEx(
                    HANDLE(self._value(handle)), 20 if restart else 19, buffer, ctypes.sizeof(buffer)
                ):
                    error = dlls.last_error()
                    if error == 18:  # ERROR_NO_MORE_FILES, never generic EOF or another failure.
                        break
                    if error in {1, 50, 87}:  # Unsupported class/driver: no different enumeration API.
                        raise _unsupported()
                    raise ContextError("storage_unavailable", "目录枚举失败；未返回部分条目。")
                restart = False
                body, offset = bytes(buffer), 0
                while True:
                    size = ctypes.sizeof(FileIdExtdDirectoryHeader)
                    if offset + size > len(body):
                        raise ContextError("storage_unavailable", "目录枚举记录边界无效。")
                    record = FileIdExtdDirectoryHeader.from_buffer_copy(body, offset)
                    length, following = int(record.FileNameLength), int(record.NextEntryOffset)
                    end = offset + size + length
                    if (
                        length < 2
                        or length % 2
                        or length > 510
                        or end > len(body)
                        or following
                        and (
                            following % 8
                            or following < size + length
                            or offset + following + size > len(body)
                        )
                    ):
                        raise ContextError("storage_unavailable", "目录枚举名称或后续记录边界无效。")
                    try:
                        name = body[offset + size : end].decode("utf-16-le", errors="strict")
                    except UnicodeError:
                        raise ContextError("storage_unavailable", "目录枚举名称不是有效UTF-16。") from None
                    if name in seen:
                        raise ContextError("version_changed", "目录枚举返回重复条目；未合并不稳定结果。")
                    seen.add(name)
                    if name not in {".", ".."}:
                        if len(result) >= max_entries:
                            raise ContextError("scan_limit", "目录条目超过本次上限；未返回部分列表。")
                        kind = "unsafe"
                        try:
                            validate_component(name)
                        except ContextError:
                            pass  # An ambiguous native spelling is reported but never opened.
                        else:
                            # FILE_ID_EXTD_DIR_INFO.ReparsePointTag is undefined
                            # unless FileAttributes has REPARSE_POINT. Ignore
                            # garbage in that field for ordinary entries; the
                            # relative metadata handle still checks real tags.
                            if not record.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                                if not any(record.FileId):
                                    raise _unsupported()  # Do not identify a file by an unchecked name.
                                try:
                                    with self.open_relative(handle, name, role="metadata") as entry:
                                        info = self.information(entry)
                                        if (
                                            info.identity.volume_serial != before.identity.volume_serial
                                            or info.identity.file_id != bytes(record.FileId)
                                            or info.directory != bool(record.FileAttributes & 0x10)
                                        ):
                                            raise ContextError("version_changed", "枚举条目身份已改变。")
                                        kind = "directory" if info.directory else "file"
                                except ContextError as error:
                                    if error.code == "not_found":
                                        raise ContextError("version_changed", "枚举条目已消失。") from None
                                    if error.code != "forbidden_path":
                                        raise
                        result.append({"name": name, "kind": kind})
                    if not following:
                        break
                    offset += following
                if self.information(handle) != before:
                    raise ContextError("version_changed", "目录在枚举期间发生变化。")
            if self.information(handle) != before:
                raise ContextError("version_changed", "目录在枚举期间发生变化。")
            return sorted(result, key=lambda entry: entry["name"])

    def read_file(
        self, handle: NativeHandle, *, max_bytes: int = MAX_NATIVE_READ, private: bool = False
    ) -> bytes:
        """Bounded synchronous read from an owned file HANDLE, never path fallback.

        Serialize rewind/read/close on this handle. Do not promise an IO deadline
        or cancellation: a native synchronous read can wait for the OS/device.
        Private reads explicitly require the ACL adapter both before and after
        reading. An unchecked read is never inferred to be a private-file proof.
        """
        body = bytearray()
        self._consume_file(handle, max_bytes=max_bytes, private=private, consume=body.extend)
        return bytes(body)

    def file_digest(
        self, handle: NativeHandle, *, expected_size: int, check_cancel: Callable[[], None] = lambda: None
    ) -> str:
        """Private archive readback; keep only a SHA state, not the entire file.

        Cancellation is cooperative between synchronous native reads. It cannot
        interrupt a blocking device read. Lease/observer handles are not archive
        consumers and do not acquire this new capability.
        """
        from collection_context.infrastructure.file_stream import MAX_STREAM_BYTES

        if type(expected_size) is not int or not 0 <= expected_size <= MAX_STREAM_BYTES:
            raise ContextError("invalid_argument", "分块回读大小无效。")
        self._value(handle)
        if handle.role not in {"read_file", "publication_file"}:
            raise ContextError("forbidden_path", "分块回读只允许归档文件角色。")
        digest = hashlib.sha256()
        count = self._consume_file(
            handle, max_bytes=expected_size, private=True, consume=digest.update, check_cancel=check_cancel
        )
        if count != expected_size:
            raise ContextError("file_stream_integrity", "分块回读未达到固定大小。")
        return digest.hexdigest()

    def _consume_file(
        self,
        handle: NativeHandle,
        *,
        max_bytes: int,
        private: bool,
        consume: Callable[[bytes], object],
        check_cancel: Callable[[], None] = lambda: None,
    ) -> int:
        total = 0
        with self.read_chunks(
            handle, max_bytes=max_bytes, private=private, check_cancel=check_cancel
        ) as chunks:
            for chunk in chunks:
                consume(chunk)
                total += len(chunk)
        return total

    @contextmanager
    def read_chunks(
        self,
        handle: NativeHandle,
        *,
        max_bytes: int = MAX_NATIVE_READ,
        private: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> Iterator[Iterator[bytes]]:
        """Single-thread owned stream shared by buffered reads and SHA readback.

        Only exhaustion performs the final version/ACL checks. Early exit closes
        the generator and releases this scope's IO lock, not the caller's HANDLE.
        The facade still owns directory/attachment and HANDLE cleanup.
        """
        if type(max_bytes) is not int or not 0 <= max_bytes <= (1 << 63) - 2:
            raise ContextError("invalid_argument", "原生读取大小上限无效。")
        if type(private) is not bool:
            raise ContextError("invalid_argument", "原生读取权限标志无效。")
        if not callable(check_cancel):
            raise ContextError("invalid_argument", "分块回读取消检查无效。")
        self._value(handle)
        if handle.role not in {"read_file", "lease_file", "lease_observer", "publication_file"}:
            raise ContextError("forbidden_path", "只允许读取固定文件角色的句柄。")
        limit = min(max_bytes, 65_536) if handle.role in {"lease_file", "lease_observer"} else max_bytes
        with handle._io_lock:
            owner = threading.get_ident()
            check_cancel()
            before = self.information(handle)
            if before.size > limit:
                raise ContextError("forbidden_path", "文件超过受控读取大小。")
            if private:
                self.require_private_security(handle)
            dlls = self._libraries()
            value, position = HANDLE(self._value(handle)), I64(-1)
            if not dlls.kernel32.SetFilePointerEx(value, I64(0), ctypes.byref(position), 0):
                raise ContextError("storage_unavailable", "原生文件位置无法确认。")
            if position.value != 0:
                raise ContextError("storage_unavailable", "原生文件位置不符合读取边界。")

            def consume() -> Generator[bytes, None, None]:
                total = 0
                while True:
                    if threading.get_ident() != owner:
                        raise ContextError("invalid_argument", "原生分块读取不能跨线程转移。")
                    check_cancel()
                    capacity = min(_READ_CHUNK, before.size + 1 - total)
                    buffer, count = ctypes.create_string_buffer(capacity), U32()
                    success = dlls.kernel32.ReadFile(
                        HANDLE(self._value(handle)), buffer, capacity, ctypes.byref(count), None
                    )
                    if not success:
                        if dlls.last_error() == 38 and count.value == 0:  # ERROR_HANDLE_EOF
                            break
                        raise ContextError("storage_unavailable", "原生文件读取失败，未返回完整内容。")
                    if count.value > capacity:
                        raise ContextError("storage_unavailable", "原生读取长度不符合缓冲区边界。")
                    if count.value == 0:
                        break
                    total += count.value
                    if total > before.size:
                        raise ContextError("version_changed", "读取时文件超过原大小范围。", retryable=True)
                    check_cancel()
                    self._value(handle)
                    yield buffer.raw[: count.value]
                after = self.information(handle)
                if before != after or total != before.size:
                    raise ContextError("version_changed", "读取时文件版本或长度发生变化。", retryable=True)
                if private:
                    self.require_private_security(handle)
                check_cancel()

            chunks = consume()
            try:
                yield chunks
            finally:
                chunks.close()

    def try_lock(self, handle: NativeHandle, *, exclusive: bool) -> NativeLock:
        self._value(handle)
        with handle._io_lock:
            return self._try_lock(handle, exclusive=exclusive)

    def _try_lock(self, handle: NativeHandle, *, exclusive: bool) -> NativeLock:
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
        lock = NativeLock(handle, overlap, before.identity, exclusive=exclusive)
        handle._active_lock = lock
        try:
            if self.information(handle).identity != before.identity:
                raise ContextError("lock_changed", "获取锁时文件身份发生变化。")
            return lock
        except BaseException:
            lock.close()
            raise

    def write_lease_metadata(self, lock: NativeLock, body: bytes) -> None:
        """Replace bounded metadata on an existing exclusively locked HANDLE.

        Not an atomic commit: a partial native write/truncate/flush can leave
        unknown content. Such failure invalidates this lock's write authority,
        retains the kernel lock until explicitly closed, and never retries.
        The eventual ownership backend must treat torn metadata as unknown.
        """
        if type(body) is not bytes or len(body) > 65_536:
            raise ContextError("invalid_argument", "所有权元数据须为有界字节内容。")
        if not isinstance(lock, NativeLock):
            raise ContextError("forbidden_path", "写入需要本适配器持有的排他所有权锁。")
        handle = lock._handle
        self._value(handle)

        def locked_value() -> HANDLE:
            value = self._value(handle)
            if (
                lock.closed
                or not lock.exclusive
                or handle.role != "lease_file"
                or handle._active_lock is not lock
                or lock.identity != handle.identity
            ):
                raise ContextError("lock_changed", "排他所有权锁无效；没有写入。")
            return HANDLE(value)

        with handle._io_lock:
            locked_value()
            before = self.information(handle)
            if before.size > 65_536:
                raise ContextError("forbidden_path", "所有权元数据超过限制。")
            dlls, position = self._libraries(), I64(-1)
            if not dlls.kernel32.SetFilePointerEx(locked_value(), I64(0), ctypes.byref(position), 0):
                raise ContextError("storage_unavailable", "原生文件位置无法确认。")
            if position.value != 0:
                raise ContextError("storage_unavailable", "原生文件位置不符合写入边界。")
            try:
                offset = 0
                while offset < len(body):
                    remaining = body[offset:]
                    buffer, count = ctypes.create_string_buffer(remaining), U32()
                    if not dlls.kernel32.WriteFile(
                        locked_value(), buffer, len(remaining), ctypes.byref(count), None
                    ) or not 0 < count.value <= len(remaining):
                        raise ContextError("storage_unavailable", "原生元数据写入结果不能确认。")
                    offset += count.value
                # Empty WriteFile is not truncation. Establish exact EOF explicitly.
                if not dlls.kernel32.SetEndOfFile(locked_value()):
                    raise ContextError("storage_unavailable", "原生元数据长度不能确认。")
                if not dlls.kernel32.FlushFileBuffers(locked_value()):
                    raise ContextError("storage_unavailable", "原生元数据落盘不能确认。")
                after = self.information(handle)
                locked_value()
                if after.size != len(body):
                    raise ContextError("storage_unavailable", "原生元数据写后长度不符。")
                if self.read_file(handle, max_bytes=65_536) != body:
                    raise ContextError("storage_unavailable", "原生元数据写后内容不符。")
                locked_value()
            except BaseException as error:
                # Do not relinquish the kernel lock or reuse partial write authority.
                handle._active_lock = None
                if not isinstance(error, Exception):
                    raise
                raise ContextError(
                    "storage_unavailable",
                    "元数据写入结果未知；已停止写入，不会自动重试或接管任务。",
                    next_action="请关闭当前句柄并显式核对所有权元数据。",
                ) from None

    def require_private_security(self, handle: NativeHandle) -> None:
        self._value(handle)
        self.private_security().require_private(handle)

    def private_security(self) -> WindowsPrivateSecurity:
        if self._security is None:
            from collection_context.infrastructure.windows_security import WindowsPrivateSecurity

            self._security = WindowsPrivateSecurity(self)
        return self._security
