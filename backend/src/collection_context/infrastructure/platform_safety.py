"""Native capability gates for security-sensitive filesystem adapters.

The report describes adapters available in the *running* interpreter.  It is
not a release-support claim: each target OS/filesystem still has to run the
native acceptance module and the full product regression suite.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import platform
import sys
from dataclasses import asdict, dataclass

from collection_context.application.contracts import ContextError


@dataclass(frozen=True)
class PlatformSafetyReport:
    os_name: str
    system: str
    release: str
    machine: str
    python: str
    safe_files_backend: str | None
    ownership_backend: str | None
    blockers: tuple[str, ...]

    @property
    def runtime_supported(self) -> bool:
        return self.safe_files_backend is not None and self.ownership_backend is not None

    def json(self) -> dict[str, object]:
        return {**asdict(self), "runtime_supported": self.runtime_supported}


def detect_platform_safety() -> PlatformSafetyReport:
    """Inspect only the real current runtime; no platform-name override is accepted."""

    blockers: list[str] = []
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
    required_dir_fd = (os.open, os.stat, os.mkdir, os.unlink, os.link)
    try:
        supports_relative_replace = {"src_dir_fd", "dst_dir_fd"}.issubset(
            inspect.signature(os.replace).parameters
        )
    except (TypeError, ValueError):
        supports_relative_replace = False
    if os.name != "posix":
        blockers.append("safe_files_backend_not_implemented")
        safe_files_backend = None
    elif any(not hasattr(os, name) for name in required_flags):
        blockers.append("required_open_flags_unavailable")
        safe_files_backend = None
    elif any(function not in os.supports_dir_fd for function in required_dir_fd):
        blockers.append("descriptor_relative_operations_unavailable")
        safe_files_backend = None
    elif os.stat not in os.supports_follow_symlinks or os.link not in os.supports_follow_symlinks:
        blockers.append("nofollow_operations_unavailable")
        safe_files_backend = None
    elif not supports_relative_replace:
        blockers.append("descriptor_relative_replace_unavailable")
        safe_files_backend = None
    elif not hasattr(os, "getuid"):
        blockers.append("private_file_owner_check_unavailable")
        safe_files_backend = None
    else:
        safe_files_backend = "posix_dirfd_v1"

    if os.name != "posix" or importlib.util.find_spec("fcntl") is None or not hasattr(os, "pread"):
        blockers.append("kernel_lease_backend_not_implemented")
        ownership_backend = None
    else:
        ownership_backend = "posix_flock_v1"

    return PlatformSafetyReport(
        os_name=os.name,
        system=platform.system() or "unknown",
        release=platform.release() or "unknown",
        machine=platform.machine() or "unknown",
        python=platform.python_version(),
        safe_files_backend=safe_files_backend,
        ownership_backend=ownership_backend,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def require_safe_files_runtime() -> PlatformSafetyReport:
    report = detect_platform_safety()
    if report.safe_files_backend is None:
        raise ContextError(
            "unsupported_platform",
            "当前系统没有通过受控文件访问能力检测；不会降级为普通路径读取。",
        )
    return report


def require_ownership_runtime() -> PlatformSafetyReport:
    report = require_safe_files_runtime()
    if report.ownership_backend is None:
        raise ContextError(
            "unsupported_platform",
            "当前系统没有通过操作系统文件所有权能力检测；不会根据 PID 或旧文件接管任务。",
        )
    return report


def current_runtime_identity() -> dict[str, str]:
    """Small stable identity used by native acceptance reports."""

    return {
        "implementation": sys.implementation.name,
        "python": platform.python_version(),
        "system": platform.system() or "unknown",
        "release": platform.release() or "unknown",
        "machine": platform.machine() or "unknown",
    }
