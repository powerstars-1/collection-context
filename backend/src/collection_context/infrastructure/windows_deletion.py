"""One owned existing file, one delete-on-close request; never recursive cleanup.

Original implementation of documented FileDispositionInformation (class 13):
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information

After submission the only file-handle operation is close. Unknown results are
not retried or cleaned by pathname. Caller supplies root-attachment checks and
must separately authorize the exact library-relative file. This draft does not
select the public Windows backend or establish Windows kernel acceptance.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.windows_native import (
    HANDLE,
    I32,
    U32,
    IoStatusBlock,
    NativeHandle,
    WindowsNative,
    validate_component,
)


class FileDispositionInformation(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class WindowsDeletion:
    def __init__(self, native: WindowsNative):
        self.native = native
        self._bound = False

    def _function(self):
        if ctypes.sizeof(FileDispositionInformation) != 1:
            raise ContextError("unsupported_platform", "原生删除结构布局不可用。")
        try:
            function = self.native._libraries().ntdll.NtSetInformationFile
        except AttributeError:
            raise ContextError("unsupported_platform", "原生删除能力不可用；不会降级。") from None
        if not self._bound:
            function.restype = I32
            function.argtypes = [HANDLE, ctypes.POINTER(IoStatusBlock), HANDLE, U32, U32]
            self._bound = True
        return function

    def unlink(self, parent: NativeHandle, component: str, *, check_attachment: Callable[[], None]) -> None:
        validate_component(component)
        self.native._value(parent)
        if parent.role != "directory":
            raise ContextError("forbidden_path", "删除需要本适配器拥有的父目录句柄。")
        function = self._function()  # Fail before opening anything with DELETE rights.
        with parent._io_lock:
            check_attachment()
            parent_identity = self.native.information(parent).identity
            self.native.require_private_security(parent)
            with self.native.open_relative(parent, component, role="delete_file") as target:
                with target._io_lock:
                    before = self.native.information(target)
                    self.native.require_private_security(target)
                    self.native.require_private_security(parent)
                    check_attachment()
                    if self.native.information(target) != before:
                        raise ContextError("version_changed", "删除前文件身份或版本变化；未提交删除。")
                    if self.native.information(parent).identity != parent_identity:
                        raise ContextError("lock_changed", "删除前父目录身份变化；未提交删除。")
                    request, io = FileDispositionInformation(1), IoStatusBlock()
                    try:
                        status = function(
                            HANDLE(self.native._value(target)),
                            ctypes.byref(io),
                            ctypes.byref(request),
                            ctypes.sizeof(request),
                            13,
                        )
                        if status != 0 or io.Result.Status != 0:
                            raise ContextError("storage_unavailable", "删除提交结果不能确认。")
                        # Do not stat/read/flush/unlock a file marked for deletion.
                        target.close()
                        check_attachment()
                        if self.native.information(parent).identity != parent_identity:
                            raise ContextError("storage_unavailable", "删除后父目录身份无法确认。")
                        self.native.require_private_security(parent)
                        try:
                            with self.native.open_relative(parent, component, role="metadata"):
                                raise ContextError(
                                    "storage_unavailable", "删除后该位置仍有目录项；不再尝试删除。"
                                )
                        except ContextError as error:
                            if error.code != "not_found":
                                raise
                        check_attachment()
                    except Exception:
                        raise ContextError(
                            "storage_unavailable",
                            "文件删除结果不能确认；已停止，不会自动重试。",
                            next_action="请核对该文件及本次任务的结果，不要按旧名称再次删除。",
                        ) from None
