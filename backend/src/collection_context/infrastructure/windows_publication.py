"""Private exclusive staging and handle-relative rename, not a public backend.

Original implementation using documented NtCreateFile/FileRenameInformation.
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information

One owned directory, one component, one request, no path fallback or automatic
retry. The caller must still prove root attachment and own the library writer
lease. Rename visibility is not a claim of power-loss/directory durability.
Failure can leave a private staging file; unknown outcomes are never cleaned by
spelling alone. Root/facade/recovery integration and actual Windows acceptance
remain separate requirements.
"""

from __future__ import annotations

import ctypes
import hashlib
import uuid
from collections.abc import Callable, Iterable

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.file_stream import STREAM_CHUNK_BYTES, validate_stream, verified_chunks
from collection_context.infrastructure.windows_native import (
    HANDLE,
    I32,
    U16,
    U32,
    FileIdentity,
    IoStatusBlock,
    NativeHandle,
    WindowsNative,
    validate_component,
)


class FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", HANDLE),
        ("FileNameLength", U32),
        ("FileName", U16 * 1),
    ]


class WindowsPublication:
    def __init__(self, native: WindowsNative):
        self.native = native
        self._bound = False

    def _rename_function(self):
        if (
            ctypes.sizeof(FileRenameInformation) != 24
            or FileRenameInformation.RootDirectory.offset != 8
            or FileRenameInformation.FileNameLength.offset != 16
            or FileRenameInformation.FileName.offset != 20
        ):
            raise ContextError("unsupported_platform", "原生发布结构布局不可用。")
        dlls = self.native._libraries()
        try:
            function = dlls.ntdll.NtSetInformationFile
        except AttributeError:
            raise ContextError("unsupported_platform", "原生发布能力不可用；不会降级。") from None
        if not self._bound:
            function.restype = I32
            function.argtypes = [HANDLE, ctypes.POINTER(IoStatusBlock), HANDLE, U32, U32]
            self._bound = True
        return function

    def write(
        self,
        parent: NativeHandle,
        component: str,
        body: bytes,
        *,
        replace: bool = False,
        _check_attachment: Callable[[], None] | None = None,
    ) -> FileIdentity:
        """Write complete bytes, flush and verify, then rename once in this parent.

        A false replace flag is enforced by the native operation, never by a
        check-then-rename. On uncertain submission/readback/close results, stop;
        do not retry, remove a target or declare the original target unchanged.
        """
        validate_component(component)
        if type(body) is not bytes or type(replace) is not bool:
            raise ContextError("invalid_argument", "文件内容须为字节，覆盖须明确指定。")
        return self.write_chunks(
            parent,
            component,
            (
                body[offset : offset + STREAM_CHUNK_BYTES]
                for offset in range(0, len(body), STREAM_CHUNK_BYTES)
            ),
            expected_size=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
            replace=replace,
            _check_attachment=_check_attachment,
        )

    def write_chunks(
        self,
        parent: NativeHandle,
        component: str,
        chunks: Iterable[bytes],
        *,
        expected_size: int,
        expected_sha256: str,
        replace: bool = False,
        check_cancel: Callable[[], None] = lambda: None,
        _check_attachment: Callable[[], None] | None = None,
    ) -> FileIdentity:
        validate_component(component)
        validate_stream(expected_size, expected_sha256, replace)
        source = verified_chunks(
            chunks, expected_size=expected_size, expected_sha256=expected_sha256, check_cancel=check_cancel
        )
        check_cancel()
        self.native._value(parent)
        if parent.role != "directory":
            raise ContextError("forbidden_path", "发布需要本适配器拥有的目录句柄。")
        rename = self._rename_function()  # Missing capability fails before creation.
        with parent._io_lock:
            if _check_attachment is not None:
                _check_attachment()
            parent_identity = self.native.information(parent).identity
            self.native.require_private_security(parent)
            security = self.native.private_security()
            descriptor = security.creation_descriptor()
            if self.native.information(parent).identity != parent_identity:
                raise ContextError("lock_changed", "创建前目录身份变化。")
            temp = ".context-" + uuid.uuid4().hex + ".tmp"
            stage = self.native._open(temp, parent, "publication_file", _creation=descriptor)
            submitted = False
            try:
                with stage._io_lock:
                    initial = self.native.information(stage)
                    if initial.size != 0 or security.current_user() != descriptor.user:
                        raise ContextError("storage_unavailable", "新文件状态或运行身份不能确认。")
                    self.native.require_private_security(stage)
                    dlls = self.native._libraries()
                    for chunk in source:
                        offset = 0
                        while offset < len(chunk):
                            check_cancel()
                            remaining = chunk[offset:]
                            buffer, count = ctypes.create_string_buffer(remaining), U32()
                            if not dlls.kernel32.WriteFile(
                                HANDLE(self.native._value(stage)),
                                buffer,
                                len(remaining),
                                ctypes.byref(count),
                                None,
                            ) or not 0 < count.value <= len(remaining):
                                raise ContextError("storage_unavailable", "新文件写入结果不能确认。")
                            offset += count.value
                    # Exclusive FILE_CREATE starts at exact empty EOF; no truncate
                    # or seek into an existing file is needed, including empty body.
                    if not dlls.kernel32.FlushFileBuffers(HANDLE(self.native._value(stage))):
                        raise ContextError("storage_unavailable", "新文件落盘结果不能确认。")
                    if (
                        self.native.file_digest(stage, expected_size=expected_size, check_cancel=check_cancel)
                        != expected_sha256
                    ):
                        raise ContextError("storage_unavailable", "新文件写后核对不符。")
                    self.native.require_private_security(parent)
                    if security.current_user() != descriptor.user:
                        raise ContextError("version_changed", "提交前运行身份变化。")
                    self.native.information(stage)
                    self.native.information(parent)
                    if _check_attachment is not None:
                        _check_attachment()
                    check_cancel()
                    encoded = component.encode("utf-16-le")
                    # The documented minimum includes sizeof(struct), not just
                    # the filename offset. Extra bytes are zeroed, not a path.
                    request = ctypes.create_string_buffer(ctypes.sizeof(FileRenameInformation) + len(encoded))
                    header = FileRenameInformation.from_buffer(request)
                    header.ReplaceIfExists = int(replace)
                    header.RootDirectory = self.native._value(parent)
                    header.FileNameLength = len(encoded)
                    ctypes.memmove(
                        ctypes.addressof(request) + FileRenameInformation.FileName.offset,
                        encoded,
                        len(encoded),
                    )
                    io = IoStatusBlock()
                    submitted = True
                    status = rename(
                        HANDLE(self.native._value(stage)), ctypes.byref(io), request, len(request), 10
                    )
                    if status != 0:
                        if (int(status) & 0xFFFFFFFF) == 0xC0000035:
                            # Confirmed native collision, not an uncertain success.
                            submitted = False
                            raise ContextError("write_conflict", "目标已存在，未覆盖原文件。")
                        raise ContextError("storage_unavailable", "原生文件提交结果不能确认。")
                    if io.Result.Status != 0:
                        raise ContextError("storage_unavailable", "原生提交完成状态不能确认。")
                    published = self.native.information(stage)
                    self.native.require_private_security(stage)
                    self.native.require_private_security(parent)
                    # Release exclusive share restrictions before reopening the
                    # target spelling. No success is reported if CloseHandle fails.
                    stage.close()
                    with self.native.open_relative(parent, component, role="read_file") as result:
                        if self.native.information(result) != published:
                            raise ContextError("version_changed", "提交后的文件身份或版本变化。")
                        if (
                            self.native.file_digest(
                                result, expected_size=expected_size, check_cancel=check_cancel
                            )
                            != expected_sha256
                        ):
                            raise ContextError("version_changed", "提交后的文件内容变化。")
                        self.native.require_private_security(parent)
                        if _check_attachment is not None:
                            _check_attachment()
                        return result.identity
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                if (
                    not submitted
                    and isinstance(error, ContextError)
                    and error.code
                    in {
                        "write_conflict",
                        "file_stream_integrity",
                        "runtime_install_cancelled",
                        "runtime_download_integrity",
                        "runtime_download_failed",
                    }
                ):
                    raise
                raise ContextError(
                    "storage_unavailable",
                    "文件写入或提交结果不能确认；已停止，不会自动重试或删除。",
                    next_action="请显式核对目标与本次暂存结果，再决定恢复。",
                ) from None
            finally:
                stage.close()
