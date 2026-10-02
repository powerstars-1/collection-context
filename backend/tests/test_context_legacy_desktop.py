"""Original local legacy fixtures; no real vault, platform or model requests."""

from __future__ import annotations

import hashlib
import io
import stat
from contextlib import closing

import pytest

from collection_context.application import legacy_desktop as desktop
from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.interfaces.access import AccessRegistry
from collection_context.launcher import _ensure_owner_access
from collection_context.library.legacy_layout import INBOX, LegacyLayoutReader
from collection_context.library.store import LibraryStore


def legacy_fixture(root):
    vault = root / "content-vault"
    inbox = vault / INBOX
    inbox.mkdir(parents=True)
    (inbox / "原创演示.md").write_text(
        "# 原创纸飞机教程\n\n## 基本信息\n- 平台: 抖音\n- 作品ID: 123\n"
        "- 来源: 收藏\n- 附件目录: 80_附件/抖音/123\n\n## 原始材料\n纸飞机沿虚线折叠。\n",
        encoding="utf-8",
    )
    attachment = vault / "80_附件/抖音/123"
    attachment.mkdir(parents=True)
    (attachment / "内容总结.md").write_text("原创测试正文，不应被启动诊断读取。", encoding="utf-8")
    (root / ".env.local").write_text("SYNTHETIC_DO_NOT_IMPORT=original_fixture\n")
    return vault


def tree(root):
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.lstat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *root.rglob("*")]
    }


def test_diagnose_uses_metadata_only_and_does_not_initialize_or_change_permissions(tmp_path, monkeypatch):
    root = tmp_path / "旧项目"
    legacy_fixture(root)
    before = tree(root)
    original_read = SafeFiles.read

    def no_source_read(self, *args, **kwargs):
        assert not self.root.is_relative_to(root), "Legacy text must not be read during setup"
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(SafeFiles, "read", no_source_read)
    monkeypatch.setattr(LegacyLayoutReader, "_text", lambda *_: pytest.fail("No full scan"))
    monkeypatch.setattr(LibraryStore, "initialize", lambda *_: pytest.fail("No initialization"))
    report = desktop.legacy_startup_report(root, 0)
    assert set(report) == {"platform", "workspace", "listen", "capabilities", "authorizations"}
    assert report["workspace"] == {
        "path": str(root),
        "state": "legacy_readonly",
        "requires_explicit_initialization": False,
        "preserved_without_migration": True,
        "readonly": True,
        "content_scanned": False,
        "code": None,
    }
    assert report["listen"]["code"] == "invalid_port"
    assert not any(report["authorizations"].values())
    assert tree(root) == before


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "ancestor_link", "new_marker", "inbox_file", "inbox_link"]
)
def test_invalid_source_diagnostics_and_prepare_refuse_before_any_access_creation(tmp_path, kind):
    source = tmp_path / "旧项目"
    vault = legacy_fixture(source)
    if kind == "missing":
        source = tmp_path / "没有此目录"
    elif kind == "symlink":
        link = tmp_path / "指向旧库"
        link.symlink_to(source, target_is_directory=True)
        source = link
    elif kind == "ancestor_link":
        link = tmp_path / "父目录链接"
        link.symlink_to(tmp_path / "旧项目", target_is_directory=True)
        source = link / "content-vault"
    elif kind == "new_marker":
        (source / "context-workspace.json").write_text("{}")
    else:
        inbox = vault / INBOX
        inbox.rename(inbox.with_name("保留原始目录"))
        if kind == "inbox_file":
            inbox.write_text("not a directory")
        else:
            inbox.symlink_to(inbox.with_name("保留原始目录"), target_is_directory=True)
    base = tmp_path / "未创建的配置目录"
    report = desktop.legacy_startup_report(source, 0)
    assert report["workspace"]["state"] == "legacy_unavailable"
    assert report["workspace"]["code"]
    with pytest.raises(ContextError):
        desktop.prepare_legacy_access(source, base=base)
    assert not base.exists()


def test_default_access_directory_is_stable_separate_and_never_created(tmp_path, monkeypatch):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    default = tmp_path / "产品数据/workspace"
    monkeypatch.setattr(desktop, "default_workspace", lambda: default)
    first = desktop.legacy_access_directory(source)
    assert first.parent == default.parent / "legacy-access"
    assert first == desktop.legacy_access_directory(source)
    assert first != desktop.legacy_access_directory(source / "content-vault")
    assert len(first.name) == 64
    assert not default.parent.exists()


def test_prepare_binds_an_empty_independent_store_and_preserves_existing_access(tmp_path):
    source = tmp_path / "旧项目"
    vault = legacy_fixture(source)
    before = tree(source)
    base = tmp_path / "独立产品配置"
    store, created = desktop.prepare_legacy_access(source, base=base)
    with closing(store):
        assert created is True
        state = store.snapshot()
        assert state["items"] == state["jobs"] == state["scopes"] == {}
        binding = state[desktop.BINDING_KEY]
        assert binding["source_path"] == str(source)
        assert binding["vault_path"] == str(vault)
        assert binding["source_identity"] == [source.stat().st_dev, source.stat().st_ino]
        assert not (store.files.root / ".env.local").exists()
        assert not (store.files.root / "content-vault").exists()
        credential = AccessRegistry(store).create("原创只读 AI")
        access_before = tree(store.files.root)
        with closing(LegacyLayoutReader(source)) as reader:
            desktop.verify_legacy_binding(store, reader)
    reopened, created = desktop.prepare_legacy_access(source, base=base)
    with closing(reopened):
        assert created is False
        assert reopened.snapshot() == state
        assert AccessRegistry(reopened).authenticate(credential["token"]).principal == credential["principal"]
        assert tree(reopened.files.root) == access_before
    assert tree(source) == before


def test_existing_ordinary_product_store_is_never_adopted_or_modified(tmp_path):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    base = tmp_path / "配置"
    path = desktop.legacy_access_directory(source, base=base)
    with closing(LibraryStore.initialize(path)):
        pass
    before = tree(path)
    with pytest.raises(ContextError, match="独立访问配置"):
        desktop.prepare_legacy_access(source, base=base)
    assert tree(path) == before


@pytest.mark.parametrize("selected_vault", [False, True])
def test_nested_access_configuration_is_rejected_before_creating_any_directory(tmp_path, selected_vault):
    root = tmp_path / "旧项目"
    vault = legacy_fixture(root)
    before = tree(root)
    source = vault if selected_vault else root
    base = root / "不能放入此处的配置"
    with pytest.raises(ContextError) as error:
        desktop.prepare_legacy_access(source, base=base)
    assert error.value.code == "legacy_paths_overlap"
    assert tree(root) == before and not base.exists()


def test_owner_cancellation_retains_bound_empty_configuration_without_live_token(tmp_path):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    before = tree(source)
    base = tmp_path / "配置"
    store, _ = desktop.prepare_legacy_access(source, base=base)
    output = io.StringIO()
    with closing(store):
        state = store.snapshot()
        with pytest.raises(ContextError) as error:
            _ensure_owner_access(store, output, owner_presenter=lambda _: False)
        assert error.value.code == "owner_confirmation_cancelled"
        assert AccessRegistry(store).records() == []
        assert store.snapshot() == state
        assert "scc_" not in output.getvalue()
    reopened, created = desktop.prepare_legacy_access(source, base=base)
    with closing(reopened):
        assert not created and reopened.snapshot() == state
        assert AccessRegistry(reopened).records() == []
    assert tree(source) == before


def test_changed_source_after_owner_preparation_cannot_reuse_binding_or_create_new_access(tmp_path):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    base = tmp_path / "配置"
    store, _ = desktop.prepare_legacy_access(source, base=base)
    with closing(store), closing(LegacyLayoutReader(source)) as original_reader:
        source.rename(tmp_path / "保留原库")
        legacy_fixture(source)
        with closing(LegacyLayoutReader(source)) as replacement:
            with pytest.raises(ContextError):
                desktop.verify_legacy_binding(store, replacement)
        with pytest.raises(ContextError):
            desktop.verify_legacy_binding(store, original_reader)
        with pytest.raises(ContextError):
            AccessRegistry(store).create("不能沿用的权限")
        assert AccessRegistry(store).records() == []
    with pytest.raises(ContextError):
        desktop.prepare_legacy_access(source, base=base)


def test_another_reader_new_library_marker_and_nonprivate_config_fail_closed(tmp_path):
    source = tmp_path / "旧项目"
    other = tmp_path / "另一旧项目"
    legacy_fixture(source)
    legacy_fixture(other)
    base = tmp_path / "配置"
    store, _ = desktop.prepare_legacy_access(source, base=base)
    with closing(store), closing(LegacyLayoutReader(other)) as reader:
        with pytest.raises(ContextError):
            desktop.verify_legacy_binding(store, reader)
        (source / ".context").mkdir()
        with pytest.raises(ContextError):
            AccessRegistry(store).create("禁止新库绕过")
    (source / ".context").rmdir()
    store.files.root.chmod(0o755)
    before = tree(store.files.root)
    with pytest.raises(ContextError) as error:
        desktop.prepare_legacy_access(source, base=base)
    assert error.value.code == "unsafe_secret_permissions"
    assert tree(store.files.root) == before


@pytest.mark.parametrize("source_change", ["moved", "replacement", "new_marker"])
def test_revocation_only_cleanup_survives_source_change_without_granting_or_rebinding(
    tmp_path, source_change
):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    base = tmp_path / "配置"
    store, _ = desktop.prepare_legacy_access(source, base=base)
    with closing(store):
        registry = AccessRegistry(store)
        retained = registry.create("已有只读权限")
        pending = registry.create("本次未确认管理口令", ui=True, manage=True)
        state = store.snapshot()
        if source_change == "new_marker":
            (source / ".context").mkdir()
            retained_source = source
        else:
            retained_source = tmp_path / "保留原始目录"
            source.rename(retained_source)
            if source_change == "replacement":
                legacy_fixture(source)
        original_before = tree(retained_source)
        replacement_before = tree(source) if source.exists() else None

        # Ordinary access creation/revocation remains bound to the source.
        with pytest.raises(ContextError):
            registry.revoke(pending["principal"])
        with pytest.raises(ContextError):
            registry.create("不能新增的权限")
        desktop.revoke_legacy_owner_access(store, pending["principal"], pending["token"])
        assert [record["principal"] for record in registry.records()] == [retained["principal"]]
        assert registry.authenticate(retained["token"]).principal == retained["principal"]
        with pytest.raises(ContextError):
            registry.authenticate(pending["token"])
        assert store.snapshot() == state  # Binding and empty manifest are unchanged.
        with pytest.raises(ContextError):
            desktop.prepare_legacy_access(source, base=base)
        assert tree(retained_source) == original_before
        if replacement_before is not None:
            assert tree(source) == replacement_before

    # Returning the exact old directory makes the existing empty configuration
    # usable again; it does not recreate the cancelled token or grant an owner.
    if source_change == "new_marker":
        (source / ".context").rmdir()
    else:
        if source.exists():
            source.rename(tmp_path / "保留替换目录")
        retained_source.rename(source)
    reopened, created = desktop.prepare_legacy_access(source, base=base)
    with closing(reopened):
        assert not created and reopened.snapshot() == state
        assert [record["principal"] for record in AccessRegistry(reopened).records()] == [
            retained["principal"]
        ]


@pytest.mark.parametrize("change", ["root_replaced", "binding_changed"])
def test_revocation_refuses_changed_access_configuration_without_touching_other_data(tmp_path, change):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    store, _ = desktop.prepare_legacy_access(source, base=tmp_path / "配置")
    with closing(store):
        pending = AccessRegistry(store).create("本次未确认管理口令", ui=True, manage=True)
        path = store.files.root
        if change == "root_replaced":
            path.rename(tmp_path / "保留原访问配置")
            with closing(LibraryStore.initialize(path)):
                pass
        else:
            with closing(LibraryStore(path)) as externally_changed:
                externally_changed.transact(
                    lambda state: state[desktop.BINDING_KEY]["source_identity"].__setitem__(1, 999999)
                )
        before = tree(path)
        with pytest.raises(ContextError):
            desktop.revoke_legacy_owner_access(store, pending["principal"], pending["token"])
        assert tree(path) == before


def test_revocation_requires_exact_prepared_store_owner_principal_and_token(tmp_path):
    source = tmp_path / "旧项目"
    legacy_fixture(source)
    store, _ = desktop.prepare_legacy_access(source, base=tmp_path / "配置")
    with closing(store):
        registry = AccessRegistry(store)
        owner = registry.create("本次未确认管理口令", ui=True, manage=True)
        readonly = registry.create("已有只读权限")
        before = registry.records()
        for principal, token in (
            (readonly["principal"], owner["token"]),
            (owner["principal"], readonly["token"]),
            (readonly["principal"], readonly["token"]),
        ):
            with pytest.raises(ContextError):
                desktop.revoke_legacy_owner_access(store, principal, token)
            assert registry.records() == before
        with closing(LibraryStore(store.files.root)) as unprepared:
            with pytest.raises(ContextError):
                desktop.revoke_legacy_owner_access(unprepared, owner["principal"], owner["token"])
        assert registry.records() == before
