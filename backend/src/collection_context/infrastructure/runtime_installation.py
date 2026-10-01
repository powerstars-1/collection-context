"""Fixed-catalog, explicit local installation; never execute an archive or enable jobs.

The archive provider is trusted application wiring, not a URL/path from a request.
This module has no network implementation. Failed/cancelled stages and old
generations stay unreferenced rather than being recursively removed. Publication
has a truthful commit-unknown boundary if replace happened but fsync failed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tarfile
import threading
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.ownership import _FileLease
from collection_context.infrastructure.platform_safety import require_safe_files_runtime
from collection_context.infrastructure.runtime_browser_layout import archive_aliases, create_archive_aliases
from collection_context.infrastructure.runtime_dependencies import (
    _LABEL,
    _SHA256,
    BROWSER_ROLES,
    DEPENDENCY_ROLES,
    MAX_MODEL_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_TOOL_BYTES,
    MODEL_ROLES,
    RECEIPT_NAME,
    TOOL_ROLES,
    RuntimeDependencies,
    _absolute,
    _host,
    _identity,
    _public_url,
    _relative,
)

MAX_ARCHIVE_BYTES = 2_147_483_648
MAX_MEMBER_BYTES = 1_073_741_824
MAX_UNPACKED_BYTES = 4_294_967_296
MAX_MEMBERS = 50_000
MAX_ZIP_DIRECTORY_BYTES = 67_108_864
MAX_TAR_METADATA_BYTES = 65_536
COPY_CHUNK_BYTES = 65_536
ARCHIVE_TYPES = frozenset({"zip", "tar.xz", "tar.gz"})
_SNAPSHOT = ".source.archive"


@dataclass(frozen=True)
class ToolSpec:
    role: str
    relative_path: str
    bytes: int
    sha256: str
    version: str
    license_id: str
    build_version: str | None = None
    playwright_package_version: str | None = None
    playwright_revision: str | None = None


@dataclass(frozen=True)
class ArtifactPlan:
    id: str
    host_system: str
    host_arch: str
    version: str
    source_url: str
    sha256: str
    bytes: int
    archive_type: str
    tools: tuple[ToolSpec, ...]


def _error(code: str) -> ContextError:
    messages = {
        "runtime_install_confirmation_required": "请明确确认安装固定目录中的运行依赖。",
        "runtime_install_invalid": "固定安装描述或制品身份无效；未回显输入。",
        "runtime_install_host_mismatch": "安装制品与本机系统或架构不匹配。",
        "runtime_install_source_unavailable": "未配置受信制品下载或本地来源；没有执行网络下载。",
        "runtime_install_unsafe": "安装目录或归档成员不安全；没有放宽路径或链接保护。",
        "runtime_install_integrity": "归档或工具大小、校验值不匹配；未发布安装收据。",
        "runtime_install_limit": "归档大小、展开量或成员数超过固定上限。",
        "runtime_install_cancelled": "安装已在提交前取消；原安装收据保留。",
        "runtime_install_existing_invalid": "旧安装收据或角色未完整校验；未替换旧收据。",
        "runtime_install_failed": "安装未完成，旧收据保留；未显示内部异常。",
        "runtime_install_outcome_unknown": "收据提交结果尚未确认；旧快照与所有分代保留，不自动回滚或重试。",
        "runtime_install_busy": "另一个运行依赖安装正在进行；未启动第二次安装。",
        "runtime_install_not_owned": "安装器没有取得目录所有权；停止安装。",
        "runtime_install_ownership_changed": "安装器目录所有权已变化；停止安装。",
        "runtime_install_lock_unavailable": "无法安全取得安装独占锁；停止安装。",
    }
    return ContextError(code, messages.get(code, messages["runtime_install_failed"]))


class _InstallationLease(_FileLease):
    path = ".runtime-installation.lock"
    busy = "runtime_install_busy"
    not_owned = "runtime_install_not_owned"
    changed = "runtime_install_ownership_changed"
    unavailable = "runtime_install_lock_unavailable"
    label = "运行依赖安装器"


def _private_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise _error("runtime_install_unsafe")


def _cancel(stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise _error("runtime_install_cancelled")


class _Budget:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0
        self.paths: dict[str, tuple[str, bool]] = {}

    def header(self) -> None:
        self.count += 1
        if self.count > MAX_MEMBERS:
            raise _error("runtime_install_limit")

    def size(self, size: int) -> None:
        if type(size) is not int or size < 0 or size > MAX_MEMBER_BYTES:
            raise _error("runtime_install_limit")
        self.total += size
        if self.total > MAX_UNPACKED_BYTES:
            raise _error("runtime_install_limit")

    def member(self, name: str, directory: bool) -> str:
        if directory and name.endswith("/"):
            name = name[:-1]
        try:
            parts = _relative(name)
        except ContextError:
            raise _error("runtime_install_unsafe") from None
        if parts[0] in {_SNAPSHOT, RECEIPT_NAME}:
            raise _error("runtime_install_unsafe")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            folded = unicodedata.normalize("NFC", prefix).casefold()
            is_directory = index < len(parts) or directory
            previous = self.paths.get(folded)
            if previous is not None and (previous[0] != prefix or not previous[1] or not is_directory):
                raise _error("runtime_install_unsafe")
            self.paths[folded] = (prefix, is_directory)
        return name


class RuntimeInstaller:
    """Synchronous business operation; UI callers put it on their owned background thread."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        library_dir: Path,
        catalog: Mapping[str, ArtifactPlan],
        _archive_source: Callable[[ArtifactPlan], Path] | None = None,
    ):
        # Fail closed on platforms without the descriptor/lock primitives,
        # before installation can create even the private runtime root.
        require_safe_files_runtime()
        self.runtime_dir, self.library_dir = _absolute(runtime_dir), _absolute(library_dir)
        if self.runtime_dir.is_relative_to(self.library_dir) or self.library_dir.is_relative_to(
            self.runtime_dir
        ):
            raise _error("runtime_install_unsafe")
        if not isinstance(catalog, Mapping) or _archive_source is not None and not callable(_archive_source):
            raise _error("runtime_install_invalid")
        self._catalog = dict(catalog)
        self._source = _archive_source
        for identity, plan in self._catalog.items():
            self._validate(plan)
            if identity != plan.id:
                raise _error("runtime_install_invalid")

    @staticmethod
    def _validate(plan: ArtifactPlan) -> None:
        if (
            not isinstance(plan, ArtifactPlan)
            or not isinstance(plan.id, str)
            or not _LABEL.fullmatch(plan.id)
            or not isinstance(plan.host_system, str)
            or plan.host_system not in {"Darwin", "Linux", "Windows"}
            or not isinstance(plan.host_arch, str)
            or plan.host_arch not in {"arm64", "x86_64"}
            or not isinstance(plan.version, str)
            or not _LABEL.fullmatch(plan.version)
            or not _public_url(plan.source_url)
            or not isinstance(plan.sha256, str)
            or not _SHA256.fullmatch(plan.sha256)
            or type(plan.bytes) is not int
            or not 0 < plan.bytes <= MAX_ARCHIVE_BYTES
            or not isinstance(plan.archive_type, str)
            or plan.archive_type not in ARCHIVE_TYPES
            or not isinstance(plan.tools, tuple)
            or not 1 <= len(plan.tools) <= len(DEPENDENCY_ROLES)
        ):
            raise _error("runtime_install_invalid")
        roles, paths = set(), set()
        for tool in plan.tools:
            if (
                not isinstance(tool, ToolSpec)
                or not isinstance(tool.role, str)
                or tool.role not in DEPENDENCY_ROLES
                or tool.role in roles
                or type(tool.bytes) is not int
                or not 0 < tool.bytes <= min(MAX_TOOL_BYTES, MAX_MEMBER_BYTES)
                or tool.role in MODEL_ROLES
                and tool.bytes > MAX_MODEL_BYTES
                or not isinstance(tool.sha256, str)
                or not _SHA256.fullmatch(tool.sha256)
                or any(
                    not isinstance(value, str) or not _LABEL.fullmatch(value)
                    for value in (tool.version, tool.license_id)
                )
            ):
                raise _error("runtime_install_invalid")
            try:
                _relative(tool.relative_path)
            except ContextError:
                raise _error("runtime_install_invalid") from None
            if tool.relative_path in paths or tool.relative_path.split("/")[0] in {_SNAPSHOT, RECEIPT_NAME}:
                raise _error("runtime_install_invalid")
            if tool.role in BROWSER_ROLES:
                if (
                    tool.build_version is not None
                    or not isinstance(tool.playwright_package_version, str)
                    or not _LABEL.fullmatch(tool.playwright_package_version)
                    or not isinstance(tool.playwright_revision, str)
                    or not tool.playwright_revision.isascii()
                    or not tool.playwright_revision.isdecimal()
                    or not 1 <= len(tool.playwright_revision) <= 12
                ):
                    raise _error("runtime_install_invalid")
            elif (
                not isinstance(tool.build_version, str)
                or not _LABEL.fullmatch(tool.build_version)
                or tool.playwright_package_version is not None
                or tool.playwright_revision is not None
            ):
                raise _error("runtime_install_invalid")
            roles.add(tool.role)
            paths.add(tool.relative_path)
        if ("ffmpeg" in roles) != ("ffprobe" in roles):
            raise _error("runtime_install_invalid")
        by_role = {tool.role: tool for tool in plan.tools}
        model_roles = MODEL_ROLES.intersection(roles)
        if model_roles and (
            model_roles != MODEL_ROLES
            or len({by_role[role].build_version for role in MODEL_ROLES}) != 1
            or len({by_role[role].relative_path.rsplit("/", 1)[0] for role in MODEL_ROLES}) != 1
        ):
            raise _error("runtime_install_invalid")
        if "ffmpeg" in roles and by_role["ffmpeg"].build_version != by_role["ffprobe"].build_version:
            raise _error("runtime_install_invalid")
        if BROWSER_ROLES.issubset(roles) and (
            by_role["chromium"].playwright_package_version,
            by_role["chromium"].playwright_revision,
        ) != (
            by_role["chromium_headless_shell"].playwright_package_version,
            by_role["chromium_headless_shell"].playwright_revision,
        ):
            raise _error("runtime_install_invalid")

    @staticmethod
    def _receipt(plan: ArtifactPlan, *, prefix: str = "") -> dict[str, Any]:
        tools = {}
        for tool in plan.tools:
            value = {
                "relative_path": prefix + tool.relative_path,
                "bytes": tool.bytes,
                "sha256": tool.sha256,
                "version": tool.version,
                "source_url": plan.source_url,
                "license_id": tool.license_id,
            }
            if tool.role in BROWSER_ROLES:
                value["playwright"] = {
                    "package_version": tool.playwright_package_version,
                    "revision": tool.playwright_revision,
                }
            else:
                value["build_version"] = tool.build_version
            tools[tool.role] = value
        return {
            "schema_version": 1,
            "host": {"system": plan.host_system, "arch": plan.host_arch},
            "tools": tools,
        }

    @staticmethod
    def _stage(files: SafeFiles, generation: str) -> Path:
        parent, name = files._parent("staging/" + generation, create=True)
        try:
            _private_directory(parent)
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        return files.root / "staging" / generation

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        while data:
            size = os.write(fd, data)
            if size <= 0:
                raise OSError
            data = data[size:]

    @classmethod
    def _snapshot(
        cls, stage: SafeFiles, path: Path, plan: ArtifactPlan, stop: threading.Event | None
    ) -> None:
        path = _absolute(path)
        with SafeFiles(path.parent) as source:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source.fd)
            target = -1
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != plan.bytes:
                    raise _error("runtime_install_integrity")
                target = os.open(
                    _SNAPSHOT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage.fd
                )
                digest, size = hashlib.sha256(), 0
                while True:
                    _cancel(stop)
                    data = os.read(fd, min(COPY_CHUNK_BYTES, plan.bytes + 1 - size))
                    if not data:
                        break
                    size += len(data)
                    if size > plan.bytes:
                        raise _error("runtime_install_integrity")
                    digest.update(data)
                    cls._write_all(target, data)
                current = os.stat(path.name, dir_fd=source.fd, follow_symlinks=False)
                source.check_root()
                if (
                    _identity(before) != _identity(os.fstat(fd))
                    or _identity(before) != _identity(current)
                    or size != plan.bytes
                    or digest.hexdigest() != plan.sha256
                ):
                    raise _error("runtime_install_integrity")
                os.fsync(target)
            finally:
                os.close(fd)
                if target >= 0:
                    os.close(target)

    @classmethod
    def _member(
        cls,
        stage: SafeFiles,
        name: str,
        stream: IO[bytes] | None,
        size: int,
        executable: bool,
        stop: threading.Event | None,
    ) -> None:
        parent, filename = stage._parent(name, create=True)
        fd = -1
        try:
            _private_directory(parent)
            if stream is None:
                try:
                    os.mkdir(filename, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    info = os.stat(filename, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                        raise _error("runtime_install_unsafe")
            else:
                fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o700 if executable else 0o600,
                    dir_fd=parent,
                )
                total = 0
                while True:
                    _cancel(stop)
                    data = stream.read(min(COPY_CHUNK_BYTES, size + 1 - total))
                    if not data:
                        break
                    total += len(data)
                    if total > size:
                        raise _error("runtime_install_integrity")
                    cls._write_all(fd, data)
                if total != size:
                    raise _error("runtime_install_integrity")
                os.fsync(fd)
            os.fsync(parent)
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent)

    @staticmethod
    def _zip_directory(stream: IO[bytes], size: int) -> None:
        # Reject oversized directory/entry counts before ZipFile allocates its
        # central-directory list. Our <=2GiB/50000-entry catalog never needs ZIP64.
        stream.seek(max(0, size - 65_557))
        tail = stream.read(65_557)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or len(tail) - position < 22:
            raise _error("runtime_install_unsafe")
        _, disk, start_disk, count_disk, count, directory_size, directory_offset, comment = struct.unpack(
            "<4s4H2IH", tail[position : position + 22]
        )
        if (
            disk
            or start_disk
            or count_disk != count
            or count == 65_535
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
            or len(tail) - position != 22 + comment
            or directory_offset + directory_size > size - (len(tail) - position)
        ):
            raise _error("runtime_install_unsafe")
        if count > MAX_MEMBERS or directory_size > MAX_ZIP_DIRECTORY_BYTES:
            raise _error("runtime_install_limit")
        stream.seek(0)

    def _unpack(self, stage: SafeFiles, plan: ArtifactPlan, stop: threading.Event | None) -> None:
        budget = _Budget()
        explicit = set()
        aliases = archive_aliases(plan.id, plan.sha256, plan.source_url)
        observed_aliases: set[str] = set()
        executables = {tool.relative_path for tool in plan.tools if tool.role in TOOL_ROLES}
        fd = os.open(_SNAPSHOT, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage.fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if plan.archive_type == "zip":
                self._zip_directory(stream, plan.bytes)
                with zipfile.ZipFile(stream) as archive:
                    for item in archive.infolist():
                        _cancel(stop)
                        budget.header()
                        directory = item.is_dir()
                        mode = item.external_attr >> 16
                        kind = stat.S_IFMT(mode)
                        if (
                            kind not in {0, stat.S_IFDIR if directory else stat.S_IFREG}
                            and not (kind == stat.S_IFLNK and item.filename in aliases and not directory)
                            or item.flag_bits & 1
                            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                            or item.orig_filename != item.filename
                        ):
                            raise _error("runtime_install_unsafe")
                        # Unknown Unix link/path extensions are not accepted.
                        extra = item.extra
                        while extra:
                            if len(extra) < 4:
                                raise _error("runtime_install_unsafe")
                            tag, length = struct.unpack("<HH", extra[:4])
                            if tag not in {0x5455, 0x7875} or length > len(extra) - 4:
                                raise _error("runtime_install_unsafe")
                            extra = extra[length + 4 :]
                        name = budget.member(item.orig_filename, directory)
                        if name in explicit or directory and item.file_size != 0:
                            raise _error("runtime_install_unsafe")
                        explicit.add(name)
                        budget.size(item.file_size)
                        if kind == stat.S_IFLNK:
                            target = aliases[name].encode("utf-8")
                            if item.file_size != len(target) or archive.read(item) != target:
                                raise _error("runtime_install_unsafe")
                            observed_aliases.add(name)
                        elif name in aliases:
                            raise _error("runtime_install_unsafe")
                        elif directory:
                            self._member(stage, name, None, 0, False, stop)
                        else:
                            with archive.open(item) as member:
                                self._member(
                                    stage,
                                    name,
                                    member,
                                    item.file_size,
                                    bool(mode & stat.S_IXUSR) or name in executables,
                                    stop,
                                )
                    if observed_aliases != set(aliases):
                        raise _error("runtime_install_unsafe")
                    _cancel(stop)
                    create_archive_aliases(stage, aliases)
            else:

                class BoundedTarInfo(tarfile.TarInfo):
                    @classmethod
                    def frombuf(cls, buf, encoding, errors):
                        _cancel(stop)
                        item = super().frombuf(buf, encoding, errors)
                        budget.header()
                        if item.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME}:
                            if item.size > MAX_TAR_METADATA_BYTES:
                                raise _error("runtime_install_limit")
                            budget.size(item.size)
                        elif not item.isreg() and not item.isdir():
                            raise _error("runtime_install_unsafe")
                        elif item.size > MAX_MEMBER_BYTES:
                            raise _error("runtime_install_limit")
                        return item

                with tarfile.open(
                    fileobj=stream,
                    mode="r|xz" if plan.archive_type == "tar.xz" else "r|gz",
                    tarinfo=BoundedTarInfo,
                ) as tar_archive:
                    for tar_item in tar_archive:
                        _cancel(stop)
                        directory = tar_item.isdir()
                        if (
                            not (tar_item.isreg() or directory)
                            or tar_item.sparse is not None
                            or any(key.startswith("GNU.sparse") for key in tar_item.pax_headers)
                        ):
                            raise _error("runtime_install_unsafe")
                        name = budget.member(tar_item.name, directory)
                        if name in explicit or directory and tar_item.size != 0:
                            raise _error("runtime_install_unsafe")
                        explicit.add(name)
                        budget.size(tar_item.size)
                        tar_member = tar_archive.extractfile(tar_item) if not directory else None
                        try:
                            self._member(
                                stage,
                                name,
                                tar_member,
                                tar_item.size,
                                bool(tar_item.mode & stat.S_IXUSR) or name in executables,
                                stop,
                            )
                        finally:
                            if tar_member is not None:
                                tar_member.close()
            if _identity(before) != _identity(os.fstat(stream.fileno())) or _identity(before) != _identity(
                os.stat(_SNAPSHOT, dir_fd=stage.fd, follow_symlinks=False)
            ):
                raise _error("runtime_install_integrity")

    def install(
        self, artifact_id: str, *, installation_confirmed: bool = False, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        if installation_confirmed is not True:
            raise _error("runtime_install_confirmation_required")
        if not isinstance(artifact_id, str) or artifact_id not in self._catalog:
            raise _error("runtime_install_invalid")
        plan = self._catalog[artifact_id]
        if {"system": plan.host_system, "arch": plan.host_arch} != _host():
            raise _error("runtime_install_host_mismatch")
        _cancel(stop)
        if self._source is None:
            raise _error("runtime_install_source_unavailable")
        publishing = False
        try:
            _absolute(self.runtime_dir)
            if not self.runtime_dir.exists():
                self.runtime_dir.mkdir(parents=True, mode=0o700)
            with SafeFiles(self.runtime_dir) as files, ExitStack() as resources:
                _private_directory(files.fd)
                lease = resources.enter_context(_InstallationLease(self.runtime_dir))
                _cancel(stop)
                old_bytes = None
                try:
                    old_bytes = files.read(RECEIPT_NAME, max_bytes=MAX_RECEIPT_BYTES, private=True)
                except ContextError as error:
                    if error.code != "not_found":
                        raise _error("runtime_install_existing_invalid") from None
                old: dict[str, Any] = {"schema_version": 1, "host": _host(), "tools": {}}
                if old_bytes is not None:
                    try:
                        registry = RuntimeDependencies(self.runtime_dir, library_dir=self.library_dir)
                        old = registry._receipt()
                        for role in old["tools"]:
                            _cancel(stop)
                            registry.resolve(role)
                    except ContextError:
                        raise _error("runtime_install_existing_invalid") from None
                generation = "g_" + uuid.uuid4().hex
                stage_path = self._stage(files, generation)
                with SafeFiles(stage_path) as stage:
                    _private_directory(stage.fd)
                    _cancel(stop)
                    source = self._source(plan)
                    _cancel(stop)
                    self._snapshot(stage, source, plan, stop)
                    self._unpack(stage, plan, stop)
                    _cancel(stop)
                    stage.unlink(_SNAPSHOT)
                    stage.write(RECEIPT_NAME, canonical_bytes(self._receipt(plan)))
                    candidate = RuntimeDependencies(stage_path, library_dir=self.library_dir)
                    for tool in plan.tools:
                        _cancel(stop)
                        candidate.resolve(tool.role)
                    stage.unlink(RECEIPT_NAME)
                    stage.check_root()
                    stage_identity = (os.fstat(stage.fd).st_dev, os.fstat(stage.fd).st_ino)
                lease.check()
                _cancel(stop)
                parent, name = files._parent("generations/" + generation, create=True)
                origin, old_name = files._parent("staging/" + generation)
                try:
                    _private_directory(parent)
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise _error("runtime_install_unsafe")
                    os.rename(old_name, name, src_dir_fd=origin, dst_dir_fd=parent)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != stage_identity or not stat.S_ISDIR(info.st_mode):
                        raise _error("runtime_install_unsafe")
                    os.fsync(origin)
                    os.fsync(parent)
                finally:
                    os.close(origin)
                    os.close(parent)
                merged = {
                    **old,
                    "tools": {
                        **old["tools"],
                        **self._receipt(plan, prefix="generations/" + generation + "/")["tools"],
                    },
                }
                body = canonical_bytes(merged)
                if len(body) > MAX_RECEIPT_BYTES:
                    raise _error("runtime_install_limit")
                # Verify combined metadata relationships before publication, by
                # using the existing schema reader in a small private stage.
                verification = self._stage(files, "v_" + uuid.uuid4().hex)
                with SafeFiles(verification) as check:
                    check.write(RECEIPT_NAME, body)
                    RuntimeDependencies(verification, library_dir=self.library_dir)._receipt()
                # Revalidate retained roles after the source callback and every
                # new role at its final generation path. A previous static
                # check is not evidence that files stayed unchanged.
                if old_bytes is not None:
                    registry = RuntimeDependencies(self.runtime_dir, library_dir=self.library_dir)
                    replaced_roles = {tool.role for tool in plan.tools}
                    for role in old["tools"]:
                        if role not in replaced_roles:
                            _cancel(stop)
                            registry.resolve(role)
                final_path = self.runtime_dir / "generations" / generation
                with SafeFiles(final_path) as final:
                    final.write(RECEIPT_NAME, canonical_bytes(self._receipt(plan)))
                    registry = RuntimeDependencies(final_path, library_dir=self.library_dir)
                    for tool in plan.tools:
                        _cancel(stop)
                        registry.resolve(tool.role)
                    final.unlink(RECEIPT_NAME)
                lease.check()
                _cancel(stop)
                if old_bytes is not None:
                    if files.read(RECEIPT_NAME, max_bytes=MAX_RECEIPT_BYTES, private=True) != old_bytes:
                        raise _error("runtime_install_existing_invalid")
                    files.write("receipts/" + generation + ".json", old_bytes)
                else:
                    try:
                        files.read(RECEIPT_NAME, max_bytes=MAX_RECEIPT_BYTES, private=True)
                    except ContextError as error:
                        if error.code != "not_found":
                            raise _error("runtime_install_existing_invalid") from None
                    else:
                        raise _error("runtime_install_existing_invalid")
                _cancel(stop)
                lease.check()
                publishing = True
                files.write(RECEIPT_NAME, body, replace=old_bytes is not None)
                return {
                    "state": "installed",
                    "installed_roles": sorted(tool.role for tool in plan.tools),
                    "generation": generation,
                    "static_verified": True,
                    "functional_verified": False,
                }
        except BaseException as error:
            if publishing:
                raise _error("runtime_install_outcome_unknown") from None
            if isinstance(error, ContextError) and error.code in {
                "runtime_install_unsafe",
                "runtime_install_integrity",
                "runtime_install_limit",
                "runtime_install_cancelled",
                "runtime_install_existing_invalid",
                "runtime_install_busy",
                "runtime_install_not_owned",
                "runtime_install_ownership_changed",
                "runtime_install_lock_unavailable",
            }:
                raise _error(error.code) from None
            raise _error("runtime_install_failed") from None
