"""Explicit desktop access to an existing legacy library, without migrating it.

Only metadata is examined here. The independent, empty LibraryStore holds access
rules and a committed source binding; it never receives the old library's data.
The caller must obtain the user's explicit legacy selection before prepare().
"""

from __future__ import annotations

import hashlib
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.diagnostics import default_workspace, startup_report
from collection_context.infrastructure.files import SafeFiles
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.legacy_layout import INBOX, LegacyLayoutReader
from collection_context.library.store import LibraryStore

BINDING_KEY = "legacy_readonly_source"
_BINDING_KEYS = {
    "schema_version",
    "source_path",
    "source_identity",
    "vault_path",
    "vault_identity",
    "inbox_identity",
}


def _plain_path(path: Path) -> Path:
    """Canonical spelling, with existing links rejected before resolution."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ContextError("forbidden_path", "旧库和访问配置路径不能包含上级目录跳转。")
    try:
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ContextError("forbidden_path", "旧库和访问配置须使用普通目录，不跟随链接。")
        return path.resolve(strict=False)
    except OSError:
        raise ContextError("storage_unavailable", "目录位置无法安全确认。") from None


def _source_binding(source: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
    source = _plain_path(source)
    reader = LegacyLayoutReader(source)
    try:
        vault = _plain_path(reader.safe.root)
        inbox = _plain_path(vault / INBOX)
        with SafeFiles(source) as selected, SafeFiles(inbox) as cards:
            reader._check_layout()
            selected.check_root()
            cards.check_root()
            return {
                "schema_version": 1,
                "source_path": str(source),
                "source_identity": list(selected.identity),
                "vault_path": str(vault),
                "vault_identity": list(reader.safe.identity),
                "inbox_identity": list(cards.identity),
            }, reader.guard_roots
    finally:
        reader.close()


def legacy_access_directory(source: Path, *, base: Path | None = None) -> Path:
    """Map one source spelling to one product-owned directory, without creating it.

    ``base`` is an optional access-directory parent, primarily for isolated tests.
    The default is alongside, never inside, the product's ordinary workspace.
    This function does not authorize legacy mode or validate any library contents.
    """
    source = _plain_path(source)
    parent = _plain_path(base if base is not None else default_workspace().parent / "legacy-access")
    name = hashlib.sha256(os.fsencode(source)).hexdigest()
    return parent / name


def _separate_access(path: Path, roots: tuple[Path, ...]) -> Path:
    path = _plain_path(path)
    for root in roots:
        root = _plain_path(root)
        if path.is_relative_to(root) or root.is_relative_to(path):
            raise ContextError("legacy_paths_overlap", "旧库与访问配置必须互不包含；未创建或修改目录。")
    return path


def _committed_binding(store: LibraryStore) -> dict[str, Any]:
    state = store.snapshot()
    binding = state.get(BINDING_KEY)
    if (
        not isinstance(binding, dict)
        or set(binding) != _BINDING_KEYS
        or type(binding.get("schema_version")) is not int
        or binding["schema_version"] != 1
        or any(not isinstance(binding.get(key), str) for key in ("source_path", "vault_path"))
        or any(
            not isinstance(binding.get(key), list)
            or len(binding[key]) != 2
            or any(type(value) is not int or value < 0 for value in binding[key])
            for key in ("source_identity", "vault_identity", "inbox_identity")
        )
        or any(state.get(key) != {} for key in ("items", "jobs", "scopes", "idempotency"))
        or state.get("settings") != {"auto_sync": False, "auto_process": False}
    ):
        raise ContextError(
            "legacy_access_incompatible", "此目录不是绑定旧库的独立访问配置；未覆盖或改作他用。"
        )
    return binding


def _check_bound_source(store: LibraryStore, expected: dict[str, Any]) -> None:
    store.files.require_private_root()
    if _committed_binding(store) != expected:
        raise ContextError("legacy_access_incompatible", "访问配置的旧库绑定已变化；停止沿用权限。")
    current, roots = _source_binding(Path(expected["source_path"]))
    _separate_access(store.files.root, roots)
    if current != expected:
        raise ContextError("legacy_source_changed", "旧库目录身份已变化；不能把旧权限用于另一个目录。")


class _LegacyAccessStore(LibraryStore):
    """Keep the prepared binding in memory for narrowly scoped revocation only."""

    def __init__(self, root: Path, binding: dict[str, Any]):
        self._legacy_binding = deepcopy(binding)
        super().__init__(root, mutation_guard=lambda: _check_bound_source(self, self._legacy_binding))


def revoke_legacy_owner_access(store: LibraryStore, principal: str, token: str) -> None:
    """Remove exactly the just-presented owner credential after cancel/failure.

    This is a revocation-only lane for the live store returned by prepare(). Its
    access-directory identity, workspace and original committed binding must all
    still match. The source may be offline/replaced; it is never read or written.
    This lane cannot create credentials, rebind a source or adopt another store.
    """
    if not isinstance(store, _LegacyAccessStore):
        raise ContextError("legacy_access_incompatible", "撤销仅适用于此次已验证的旧库访问配置。")
    expected_binding = deepcopy(store._legacy_binding)
    expected_identity = store.files.identity
    expected_workspace = store.workspace_id

    def check_original():
        _plain_path(store.files.root)
        store.files.require_private_root()
        if store.files.identity != expected_identity or _committed_binding(store) != expected_binding:
            raise ContextError("legacy_access_incompatible", "原访问配置身份或绑定已变化；未清理其他配置。")

    check_original()  # Refuse replacements before creating/opening writer metadata.
    control: LibraryStore

    def check_control():
        check_original()
        control.files.require_private_root()
        if (
            control.files.identity != expected_identity
            or control.workspace_id != expected_workspace
            or _committed_binding(control) != expected_binding
        ):
            raise ContextError("legacy_access_incompatible", "独立访问配置已变化；未清理其他配置。")

    control = LibraryStore(store.files.root, mutation_guard=check_control)
    try:
        check_control()
        with control.writer():
            registry = AccessRegistry(control)
            credential = registry.authenticate(token)
            if credential.principal != principal or "ui:manage" not in credential.permissions:
                raise ContextError("permission_denied", "待撤销口令与此次管理身份不符，未移除任何权限。")
            records = registry.records()
            check_control()
            registry._save([record for record in records if record["principal"] != principal])
    finally:
        control.close()


def verify_legacy_binding(store: LibraryStore, reader: LegacyLayoutReader) -> None:
    """Verify a desktop binding against the exact reader that will serve requests.

    Explicit callers only: manually configured legacy servers need not have a
    desktop binding. No caller-supplied HTTP path is accepted by this operation.
    """
    binding = _committed_binding(store)
    _check_bound_source(store, binding)
    reader._check_layout()
    if (
        str(_plain_path(reader.safe.root)) != binding["vault_path"]
        or list(reader.safe.identity) != binding["vault_identity"]
        or Path(binding["source_path"]) not in reader.guard_roots
    ):
        raise ContextError("legacy_source_changed", "待打开的只读资料库与已登记来源不同，未启动读取。")


def legacy_startup_report(source: Path, port: int) -> dict[str, Any]:
    """Reuse normal startup diagnostics, replacing only the workspace assessment."""
    report = startup_report(source, port)
    workspace = {
        "path": str(Path(source).absolute()),
        "state": "legacy_unavailable",
        "requires_explicit_initialization": False,
        "preserved_without_migration": True,
        "readonly": True,
        "content_scanned": False,
        "code": None,
    }
    try:
        binding, _ = _source_binding(source)
        workspace.update(path=binding["source_path"], state="legacy_readonly")
    except ContextError as error:
        workspace["code"] = error.code
    except (OSError, ValueError):
        workspace["code"] = "storage_unavailable"
    report["workspace"] = workspace
    return report


def _create_access_directory(path: Path) -> None:
    """Create only a new leaf, through an existing no-follow SafeFiles ancestor."""
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    with SafeFiles(ancestor) as files:
        parent, name = files._parent(path.relative_to(ancestor).as_posix(), create=True)
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            files.check_root()
        except FileExistsError:
            raise ContextError(
                "legacy_access_conflict", "访问配置位置已被创建；请重新检查，不覆盖并发结果。"
            ) from None
        except OSError:
            raise ContextError("storage_unavailable", "无法创建独立访问配置目录。") from None
        finally:
            os.close(parent)


def prepare_legacy_access(source: Path, *, base: Path | None = None) -> tuple[LibraryStore, bool]:
    """Explicit legacy choice only; caller owns and closes the returned store.

    Cancellation after this call leaves the empty, bound access configuration in
    place. Owner tokens are handled by the existing confirmation/revocation flow.
    Existing unbound/ordinary workspaces are refused, never adopted or repaired.
    """
    binding, roots = _source_binding(source)
    path = _separate_access(legacy_access_directory(source, base=base), roots)
    created = not path.exists()
    if created:
        _create_access_directory(path)
        store = LibraryStore.initialize(path)
        try:
            store.files.require_private_root()

            def confirm_source():
                current, current_roots = _source_binding(Path(binding["source_path"]))
                _separate_access(path, current_roots)
                if current != binding:
                    raise ContextError("legacy_source_changed", "旧库在创建访问配置期间变化；未绑定新来源。")

            def bind(state):
                state[BINDING_KEY] = binding

            store.transact(bind, before_commit=confirm_source)
        finally:
            store.close()

    store = _LegacyAccessStore(path, binding)
    try:
        _check_bound_source(store, binding)
    except BaseException:
        store.close()
        raise
    return store, created
