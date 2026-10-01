"""Read fixed, library-external installation receipts; never install or execute.

Receipts describe trusted installations, not authorization to fetch their URLs.
Static hash verification does not establish software provenance, functionality,
browser revision compatibility, or permission to synchronize/upload content.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.platform_safety import require_safe_files_runtime

RECEIPT_NAME = "runtime-dependencies.json"
MAX_RECEIPT_BYTES = 65_536
MAX_TOOL_BYTES = 1_073_741_824
TOOL_ROLES = frozenset({"ffmpeg", "ffprobe", "chromium", "chromium_headless_shell"})
MODEL_ROLES = frozenset({"ocr_det", "ocr_cls", "ocr_rec"})
DEPENDENCY_ROLES = TOOL_ROLES | MODEL_ROLES
MAX_MODEL_BYTES = 64_000_000
BROWSER_ROLES = frozenset({"chromium", "chromium_headless_shell"})
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGES = {
    "runtime_dependency_invalid": "运行依赖安装收据格式无效；未回显输入。",
    "runtime_dependency_unsafe": "运行依赖目录或文件不符合受控读取要求。",
    "runtime_dependency_missing": "所需运行依赖或安装收据不存在；不会自动安装。",
    "runtime_dependency_host_mismatch": "运行依赖与当前系统或架构不匹配。",
    "runtime_dependency_integrity": "运行依赖内容、大小或读取版本与安装收据不一致。",
    "runtime_dependency_version_mismatch": "浏览器绑定的 Playwright 包版本与当前运行包不一致。",
    "runtime_dependency_platform_unverified": "当前平台缺少已实现的安全读取原语；未降级读取。",
}


def _error(code: str) -> ContextError:
    return ContextError(code, _MESSAGES[code])


def _absolute(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or ".." in value.parts:
        raise _error("runtime_dependency_unsafe")
    if value == Path(value.anchor) or value == Path.home():
        raise _error("runtime_dependency_unsafe")
    for part in (value, *value.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _error("runtime_dependency_unsafe")
    return value


def _host() -> dict[str, str]:
    system = platform.system()
    architecture = platform.machine().lower()
    architecture = {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(architecture, architecture)
    if system not in {"Darwin", "Linux", "Windows"} or architecture not in {"arm64", "x86_64"}:
        raise _error("runtime_dependency_host_mismatch")
    return {"system": system, "arch": architecture}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _relative(value: Any) -> list[str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise _error("runtime_dependency_invalid")
    parts = value.split("/")
    if any(
        part in {"", ".", ".."}
        or len(part) > 255
        or any(ord(char) < 32 or char in "\\:\x7f" for char in part)
        for part in parts
    ):
        raise _error("runtime_dependency_invalid")
    return parts


def _public_url(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not value.isascii():
        return False
    if any(ord(char) <= 32 or char in "\\\x7f" for char in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_multicast and not address.is_reserved
        except ValueError:
            return (
                "." in host
                and not host.endswith((".local", ".localhost", ".internal", ".test", ".invalid"))
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split("."))
            )
    except ValueError:
        return False


def _identity(info) -> tuple:
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


@dataclass(frozen=True)
class ToolDependency:
    role: str
    path: Path
    bytes: int
    sha256: str
    version: str
    source_url: str
    license_id: str
    build_version: str | None = None
    playwright_package_version: str | None = None
    playwright_revision: str | None = None
    static_verified: bool = True
    functional_verified: bool = False
    revision_verified: bool = False


class RuntimeDependencies:
    """Explicit, read-only registry. resolve() rereads and rehashes on every call.

    library_dir is mandatory to reject runtime/library overlap. This reader does
    not acquire mutable installation or library ownership, and retains no fd.
    Windows currently fails closed until equivalent descriptor access is added.
    """

    def __init__(self, runtime_dir: Path, *, library_dir: Path):
        try:
            require_safe_files_runtime()
        except ContextError:
            raise _error("runtime_dependency_platform_unverified") from None
        try:
            self.runtime_dir = _absolute(runtime_dir)
            library = _absolute(library_dir)
            if (
                self.runtime_dir == library
                or self.runtime_dir in library.parents
                or library in self.runtime_dir.parents
            ):
                raise _error("runtime_dependency_unsafe")
            info = self.runtime_dir.lstat()
            self._root_identity = (info.st_dev, info.st_ino)
            self._root_check(info)
        except ContextError:
            raise
        except FileNotFoundError:
            raise _error("runtime_dependency_missing") from None
        except OSError:
            raise _error("runtime_dependency_unsafe") from None

    def _root_check(self, info) -> None:
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != self._root_identity
        ):
            raise _error("runtime_dependency_unsafe")

    @contextmanager
    def _open(self, relative: str, *, private: bool = False):
        descriptors = []
        directories = []
        try:
            _absolute(self.runtime_dir)
            root_fd = os.open(self.runtime_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(root_fd)
            self._root_check(os.fstat(root_fd))
            parts = _relative(relative)
            parent = root_fd
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(child)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise _error("runtime_dependency_unsafe")
                directories.append((parent, part, info.st_dev, info.st_ino))
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            descriptors.append(fd)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or before.st_mode & 0o022
                or private
                and stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise _error("runtime_dependency_unsafe")
            yield fd, before
            after = os.fstat(fd)
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(before) != _identity(current):
                raise _error("runtime_dependency_integrity")
            for ancestor, name, device, inode in directories:
                current_directory = os.stat(name, dir_fd=ancestor, follow_symlinks=False)
                if not stat.S_ISDIR(current_directory.st_mode) or (
                    current_directory.st_dev,
                    current_directory.st_ino,
                ) != (device, inode):
                    raise _error("runtime_dependency_integrity")
                if current_directory.st_uid != os.getuid() or current_directory.st_mode & 0o022:
                    raise _error("runtime_dependency_unsafe")
            _absolute(self.runtime_dir)
            self._root_check(self.runtime_dir.lstat())
        except ContextError:
            raise
        except FileNotFoundError:
            raise _error("runtime_dependency_missing") from None
        except OSError:
            raise _error("runtime_dependency_unsafe") from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _receipt(self) -> dict:
        with self._open(RECEIPT_NAME, private=True) as (fd, info):
            if not 0 < info.st_size <= MAX_RECEIPT_BYTES:
                raise _error("runtime_dependency_invalid")
            data = bytearray()
            while len(data) <= MAX_RECEIPT_BYTES:
                chunk = os.read(fd, min(8192, MAX_RECEIPT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) != info.st_size:
                raise _error("runtime_dependency_integrity")
        try:
            receipt = json.loads(data, object_pairs_hook=_pairs)
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"schema_version", "host", "tools"}
                or type(receipt["schema_version"]) is not int
                or receipt["schema_version"] != 1
                or not isinstance(receipt["host"], dict)
                or set(receipt["host"]) != {"system", "arch"}
                or not isinstance(receipt["tools"], dict)
                or not set(receipt["tools"]).issubset(DEPENDENCY_ROLES)
            ):
                raise ValueError
            if receipt["host"] != _host():
                raise _error("runtime_dependency_host_mismatch")
            common = {"relative_path", "bytes", "sha256", "version", "source_url", "license_id"}
            for role, tool in receipt["tools"].items():
                extra = {"playwright"} if role in BROWSER_ROLES else {"build_version"}
                if not isinstance(tool, dict) or set(tool) != common | extra:
                    raise ValueError
                _relative(tool["relative_path"])
                if (
                    type(tool["bytes"]) is not int
                    or not 0 < tool["bytes"] <= MAX_TOOL_BYTES
                    or role in MODEL_ROLES
                    and tool["bytes"] > MAX_MODEL_BYTES
                    or not isinstance(tool["sha256"], str)
                    or not _SHA256.fullmatch(tool["sha256"])
                    or any(
                        not isinstance(tool[k], str) or not _LABEL.fullmatch(tool[k])
                        for k in ("version", "license_id")
                    )
                    or not _public_url(tool["source_url"])
                ):
                    raise ValueError
                if role not in BROWSER_ROLES:
                    if not isinstance(tool["build_version"], str) or not _LABEL.fullmatch(
                        tool["build_version"]
                    ):
                        raise ValueError
                else:
                    binding = tool["playwright"]
                    if (
                        not isinstance(binding, dict)
                        or set(binding) != {"package_version", "revision"}
                        or not isinstance(binding["package_version"], str)
                        or not _LABEL.fullmatch(binding["package_version"])
                        or not isinstance(binding["revision"], str)
                        or not re.fullmatch(r"[0-9]{1,12}", binding["revision"])
                    ):
                        raise ValueError
            tools = receipt["tools"]
            model_roles = MODEL_ROLES.intersection(tools)
            if model_roles and (
                model_roles != MODEL_ROLES
                or len({tools[role]["build_version"] for role in MODEL_ROLES}) != 1
                or len({tools[role]["relative_path"].rsplit("/", 1)[0] for role in MODEL_ROLES}) != 1
            ):
                raise ValueError
            if ("ffmpeg" in tools) != ("ffprobe" in tools):
                raise ValueError
            if "ffmpeg" in tools and tools["ffmpeg"]["build_version"] != tools["ffprobe"]["build_version"]:
                raise ValueError
            if (
                BROWSER_ROLES.issubset(tools)
                and tools["chromium"]["playwright"] != tools["chromium_headless_shell"]["playwright"]
            ):
                raise ValueError
            paths = [tool["relative_path"] for tool in tools.values()]
            if len(paths) != len(set(paths)) or RECEIPT_NAME in paths:
                raise ValueError
            return receipt
        except ContextError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _error("runtime_dependency_invalid") from None

    def resolve(self, role: str) -> ToolDependency:
        if not isinstance(role, str) or role not in DEPENDENCY_ROLES:
            raise _error("runtime_dependency_invalid")
        tool = self._receipt()["tools"].get(role)
        if tool is None:
            raise _error("runtime_dependency_missing")
        if role in BROWSER_ROLES:
            from collection_context.infrastructure.runtime_browser_layout import verify_browser_aliases

            try:
                verify_browser_aliases(self.runtime_dir, tool)
            except (ContextError, OSError):
                raise _error("runtime_dependency_unsafe") from None
            try:
                installed = metadata.version("playwright")
            except Exception:
                raise _error("runtime_dependency_version_mismatch") from None
            if installed != tool["playwright"]["package_version"]:
                raise _error("runtime_dependency_version_mismatch")
        with self._open(tool["relative_path"]) as (fd, info):
            if info.st_size != tool["bytes"]:
                raise _error("runtime_dependency_integrity")
            path = self.runtime_dir.joinpath(*_relative(tool["relative_path"]))
            if role in TOOL_ROLES and (not info.st_mode & 0o111 or not os.access(path, os.X_OK)):
                raise _error("runtime_dependency_unsafe")
            if role in MODEL_ROLES and info.st_mode & 0o111:
                raise _error("runtime_dependency_unsafe")
            hasher = hashlib.sha256()
            count = 0
            while count <= tool["bytes"]:
                chunk = os.read(fd, min(1_048_576, tool["bytes"] + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                hasher.update(chunk)
            if count != tool["bytes"] or hasher.hexdigest() != tool["sha256"]:
                raise _error("runtime_dependency_integrity")
        binding = tool.get("playwright", {})
        return ToolDependency(
            role=role,
            path=path,
            bytes=tool["bytes"],
            sha256=tool["sha256"],
            version=tool["version"],
            source_url=tool["source_url"],
            license_id=tool["license_id"],
            build_version=tool.get("build_version"),
            playwright_package_version=binding.get("package_version"),
            playwright_revision=binding.get("revision"),
        )

    def read_model(self, role: str) -> bytes:
        """Return a bounded, descriptor-verified snapshot, not a mutable model path.

        Resource roles never grant executable permission. Each call checks the
        receipt again and verifies the exact bytes consumed by the OCR loader.
        """
        if not isinstance(role, str) or role not in MODEL_ROLES:
            raise _error("runtime_dependency_invalid")
        tool = self._receipt()["tools"].get(role)
        if tool is None:
            raise _error("runtime_dependency_missing")
        with self._open(tool["relative_path"]) as (fd, info):
            if info.st_mode & 0o111 or info.st_size != tool["bytes"]:
                raise _error("runtime_dependency_unsafe")
            body = bytearray()
            while len(body) <= tool["bytes"]:
                chunk = os.read(fd, min(1_048_576, tool["bytes"] + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) != tool["bytes"] or hashlib.sha256(body).hexdigest() != tool["sha256"]:
                raise _error("runtime_dependency_integrity")
        return bytes(body)
