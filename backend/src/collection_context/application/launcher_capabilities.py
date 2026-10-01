"""Fixed local startup capabilities, distinct from individual task authorization.

Construction only inspects paths. No credentials, platform pages, processes,
receipts or model requests are read or executed. The resource factory uses the
explicit credential backend; private-file is an UNENCRYPTED service option, not a
fallback after a system error. Opening resources never authorizes queued work.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import KW_ONLY, dataclass
from pathlib import Path

from collection_context import diagnostics
from collection_context.application.connection_runner import ConnectionRunner
from collection_context.application.contracts import ContextError
from collection_context.infrastructure.platform_safety import require_safe_files_runtime
from collection_context.infrastructure.runtime_dependencies import RECEIPT_NAME
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.infrastructure.system_secrets import SystemSecrets


def _invalid() -> ContextError:
    return ContextError("launcher_capabilities_invalid", "启动能力或固定目录无效；未回显配置。")


def _unsafe() -> ContextError:
    return ContextError("launcher_directory_unsafe", "固定目录不符合隔离或私有访问要求；未改变权限。")


def _path(value: Path, *, private: bool = False) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or any("\x00" in part for part in value.parts)
        or value == Path(value.anchor)
        or value == Path.home()
    ):
        raise _unsafe()
    try:
        for part in (value, *value.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise _unsafe()
            if part == value and private:
                if os.name != "posix" or not hasattr(os, "getuid"):
                    raise ContextError("unsupported_platform", "当前系统尚无已验证的私有目录保护。")
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise _unsafe()
        # Do not resolve through links or depend on cwd. Path is already absolute.
        return value
    except OSError:
        raise _unsafe() from None


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


@dataclass(frozen=True)
class LauncherCapabilities:
    """Four independent explicit grants, bound to one fixed workspace.

    Execution flags require their configuration/connection prerequisites, but do not
    implicitly grant them. Missing directories are not created by this constructor.
    An explicitly supplied absent runtime directory never triggers installation.
    """

    workspace: Path
    _: KW_ONLY
    allow_model_config: bool = False
    allow_source_connect: bool = False
    allow_model_calls: bool = False
    allow_source_sync: bool = False
    credential_dir: Path | None = None
    browser_dir: Path | None = None
    runtime_dir: Path | None = None
    credential_backend: str = "private-file"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.credential_backend) is not str or self.credential_backend not in {
            "private-file",
            "system",
        }:
            raise _invalid()
        flags = (
            self.allow_model_config,
            self.allow_source_connect,
            self.allow_model_calls,
            self.allow_source_sync,
        )
        if any(type(flag) is not bool for flag in flags):
            raise _invalid()
        if (
            self.allow_model_calls
            and not self.allow_model_config
            or self.allow_source_sync
            and not self.allow_source_connect
            or self.allow_model_config
            and self.credential_dir is None
            or self.allow_source_connect
            and self.browser_dir is None
        ):
            raise _invalid()
        require_safe_files_runtime()  # Unknown OS/protection never passes by name.
        workspace = _path(self.workspace)
        directories = [
            _path(path, private=True)
            for path in (self.credential_dir, self.browser_dir, self.runtime_dir)
            if path is not None
        ]
        if any(_overlap(workspace, path) for path in directories) or any(
            _overlap(left, right)
            for index, left in enumerate(directories)
            for right in directories[index + 1 :]
        ):
            raise _unsafe()


def default_desktop_capabilities(
    workspace: Path,
    *,
    allow_model_config: bool = False,
    allow_source_connect: bool = False,
    allow_model_calls: bool = False,
    allow_source_sync: bool = False,
) -> LauncherCapabilities:
    """Product-owned defaults; never import old project, browser or .env settings.

    No receipt means no fixed runtime registry: normal read-only startup is still
    possible, but dependency installation/functionality remains separately required.
    Receipt existence alone is NOT validation and no receipt contents are read here.
    """
    require_safe_files_runtime()
    root = _path(diagnostics.default_workspace().parent)
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        root_info = None
    except OSError:
        raise _unsafe() from None
    if root_info is not None and (root_info.st_uid != os.getuid() or root_info.st_mode & 0o022):
        raise _unsafe()
    if _overlap(root, _path(workspace)):
        # The canonical library is an intended sibling of the three private dirs.
        if workspace != diagnostics.default_workspace():
            raise _unsafe()
    runtime = root / "runtime"
    _path(runtime)
    try:
        receipt = runtime / RECEIPT_NAME
        try:
            info = receipt.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise _unsafe()
    except OSError:
        raise _unsafe() from None
    return LauncherCapabilities(
        workspace=workspace,
        allow_model_config=allow_model_config,
        allow_source_connect=allow_source_connect,
        allow_model_calls=allow_model_calls,
        allow_source_sync=allow_source_sync,
        credential_dir=root / "credentials-system" if allow_model_config else None,
        browser_dir=root / "browser" if allow_source_connect else None,
        runtime_dir=runtime if info is not None else None,
        credential_backend="system",
    )


@dataclass(frozen=True)
class LauncherResources:
    model_secrets: CredentialBackend | None = None
    connection_runner: ConnectionRunner | None = None
    credential_storage: str = "not_enabled"
    worker_started: bool = False


@contextmanager
def launcher_resources(
    capabilities: LauncherCapabilities, *, headless: bool = False
) -> Iterator[LauncherResources]:
    """Own setup handles only; close on all body/partial-construction failures.

    No browser profile is created until an explicitly requested ConnectionRunner
    operation. Existing key files are not read while opening the secret backend.
    System backend failure never falls back to plaintext or imports older files.
    """
    if not isinstance(capabilities, LauncherCapabilities) or type(headless) is not bool:
        raise _invalid()
    capabilities.validate()  # Recheck after GUI selection, before opening resources.
    with ExitStack() as cleanup:
        secrets = None
        runner = None
        if capabilities.allow_model_config:
            assert capabilities.credential_dir is not None
            backend = SystemSecrets if capabilities.credential_backend == "system" else FileSecrets
            secrets = (
                backend(capabilities.credential_dir)
                if capabilities.credential_dir.exists()
                else backend.initialize(capabilities.credential_dir)
            )
            cleanup.callback(secrets.close)
        if capabilities.allow_source_connect:
            assert capabilities.browser_dir is not None
            runner = ConnectionRunner(
                capabilities.workspace,
                capabilities.browser_dir,
                headless=headless,
                runtime_dir=capabilities.runtime_dir,
            )
            # Desktop ownership must outlive any still-finishing observation;
            # HTTP's bounded close does not prove its thread exited.
            cleanup.callback(lambda: runner.close(timeout=None))
        yield LauncherResources(
            model_secrets=secrets,
            connection_runner=runner,
            credential_storage=secrets.storage_kind if secrets else "not_enabled",
        )
