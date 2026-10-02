"""Unselected Windows new-root bootstrap, not business-library initialization.

Only call after the user explicitly selected an absolute local drive path.
Every existing ancestor remains pinned by an owned native directory HANDLE;
only missing descendants receive a new protected private DACL. An existing
final directory is never adopted, emptied, overwritten, or permission-repaired.
Failure closes handles but never deletes potentially user-modified directories.
Windows kernel behavior still requires separate native acceptance.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.windows_native import (
    FileIdentity,
    NativeHandle,
    WindowsNative,
    validate_component,
)

MAX_COMPONENTS = 64


@dataclass(frozen=True)
class _Anchor:
    handle: NativeHandle
    identity: FileIdentity
    component: str | None
    created: bool


def _selected_path(path: str) -> tuple[str, list[str]]:
    # No normalization, CWD, env expansion, UNC, device alias, or drive-root creation.
    if not isinstance(path, str) or len(path) > 1024 or not re.match(r"^[A-Za-z]:\\", path):
        raise ContextError("forbidden_path", "新资料目录须为明确选择的绝对本地盘符路径。")
    components = path[3:].split("\\")
    if len(components) > MAX_COMPONENTS:
        raise ContextError("forbidden_path", "所选新目录层数超过受控范围。")
    for component in components:
        validate_component(component)
    return path[:3], components


def _directory_identity(native: WindowsNative, handle: NativeHandle) -> FileIdentity:
    info = native.information(handle)
    # Native already rejects reparse points, wrong roles and delete-pending objects.
    # Explicitly disallow ambiguous directory links as well as regular-file links.
    if not info.directory or info.links != 1:
        raise ContextError("forbidden_path", "初始化只接受单一身份的非链接目录。")
    return info.identity


def _close_owned(anchors: list[_Anchor]) -> None:
    error: BaseException | None = None
    for anchor in reversed(anchors):
        try:
            anchor.handle.close()
        except BaseException as caught:
            # A failure closing one handle cannot leave the remaining handles unattempted.
            if error is None:
                error = caught
    if error is not None:
        raise error


def _check_chain(native: WindowsNative, drive: str, anchors: list[_Anchor]) -> None:
    if not anchors:
        raise ContextError("storage_unavailable", "目录初始化没有存活的根句柄。")
    for index, anchor in enumerate(anchors):
        if _directory_identity(native, anchor.handle) != anchor.identity:
            raise ContextError("storage_unavailable", "目录句柄身份发生变化；停止初始化。")
        if index == 0:
            current = native.open_root_directory(drive)
        else:
            assert anchor.component is not None
            current = native.open_relative(anchors[index - 1].handle, anchor.component, role="directory")
        with current:
            if _directory_identity(native, current) != anchor.identity:
                raise ContextError("storage_unavailable", "目录路径绑定发生变化；停止初始化。")
        if anchor.created:
            native.require_private_security(anchor.handle)
        if _directory_identity(native, anchor.handle) != anchor.identity:
            raise ContextError("storage_unavailable", "目录核对期间身份发生变化。")


class InitializedWindowsRoot:
    """Own the complete anchored chain until explicit close; never transfer a HANDLE.

    ``handle`` is the newly created final directory, not a POSIX file descriptor.
    Consumers must retain this result and call ``check`` while opening any separate
    WindowsFiles facade. No LibraryStore configuration, content, or credentials exist
    as a result of this operation. The injected/native adapter remains caller-owned.
    """

    def __init__(self, root: str, native: WindowsNative, drive: str, anchors: list[_Anchor]):
        self.root = root
        self.native = native
        self._drive = drive
        self._anchors = anchors
        self._lock = threading.RLock()
        self._closed = False
        self.handle = anchors[-1].handle
        self.identity = anchors[-1].identity
        self.created_count = sum(anchor.created for anchor in anchors)

    @property
    def closed(self) -> bool:
        return self._closed

    def check(self) -> None:
        with self._lock:
            if self._closed:
                raise ContextError("storage_unavailable", "新目录句柄已经关闭；不能继续初始化。")
            _check_chain(self.native, self._drive, self._anchors)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            _close_owned(self._anchors)

    def __enter__(self) -> InitializedWindowsRoot:
        try:
            self.check()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def initialize_windows_root(path: str, *, _native: WindowsNative | None = None) -> InitializedWindowsRoot:
    """Exclusively create an explicitly selected private root; never reuse the final path.

    A competing intermediate creation is a conflict, not permission to adopt it.
    On failure, new private directories may remain; no recursive deletion/ACL repair
    occurs, and a retry must not silently reuse an already-created final directory.
    """
    drive, components = _selected_path(path)
    native = _native if _native is not None else WindowsNative()
    anchors: list[_Anchor] = []
    try:
        volume = native.open_root_directory(drive)
        try:
            identity = _directory_identity(native, volume)
            anchors.append(_Anchor(volume, identity, None, False))
        except BaseException:
            volume.close()
            raise
        for index, component in enumerate(components):
            _check_chain(native, drive, anchors)
            parent = anchors[-1].handle
            final = index == len(components) - 1
            created = False
            if not final:
                try:
                    child = native.open_relative(parent, component, role="directory")
                except ContextError as error:
                    if error.code != "not_found":
                        raise
                    _check_chain(native, drive, anchors)
                    child = native.create_directory(parent, component)
                    created = True
            else:
                try:
                    child = native.create_directory(parent, component)
                except ContextError as error:
                    if error.code == "write_conflict":
                        raise ContextError(
                            "workspace_not_empty", "所选最终路径已存在；不会覆盖、接管或修改其权限。"
                        ) from None
                    raise
                created = True
            try:
                identity = _directory_identity(native, child)
                if identity.volume_serial != anchors[0].identity.volume_serial or any(
                    identity == anchor.identity for anchor in anchors
                ):
                    raise ContextError("forbidden_path", "新目录链包含跨卷或重复身份；未继续初始化。")
                anchors.append(_Anchor(child, identity, component, created))
            except BaseException:
                child.close()
                raise
            _check_chain(native, drive, anchors)
        result = InitializedWindowsRoot(path, native, drive, anchors)
        result.check()
        return result
    except BaseException:
        _close_owned(anchors)
        raise
