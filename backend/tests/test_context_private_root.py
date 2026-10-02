"""Real isolated POSIX private-root checks and shared credential call contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import files as module
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.system_secrets import _private_root


def test_private_root_uses_pinned_owner_mode_without_reading_files(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as files:
        files.require_private_root()
        assert list(root.iterdir()) == []


@pytest.mark.parametrize("mode", [0o701, 0o710, 0o740, 0o750, 0o755, 0o777])
def test_world_or_group_access_is_not_private_and_not_repaired(tmp_path, mode):
    root = tmp_path / "private"
    root.mkdir(mode=mode)
    root.chmod(mode)
    with SafeFiles(root) as files:
        with pytest.raises(ContextError) as caught:
            files.require_private_root()
        assert caught.value.code == "unsafe_secret_permissions"
        assert root.stat().st_mode & 0o777 == mode


def test_unexpected_owner_is_rejected_without_chown(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as files:
        monkeypatch.setattr(module.os, "getuid", lambda: root.stat().st_uid + 1)
        with pytest.raises(ContextError) as caught:
            files.require_private_root()
        assert caught.value.code == "unsafe_secret_permissions"


def test_permission_change_during_snapshot_blocks(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    fstat = module.os.fstat
    calls = []
    with SafeFiles(root) as files:

        def changing(fd):
            result = fstat(fd)
            calls.append(fd)
            if len(calls) == 1:
                root.chmod(0o750)
            return result

        monkeypatch.setattr(module.os, "fstat", changing)
        with pytest.raises(ContextError) as caught:
            files.require_private_root()
        assert caught.value.code == "version_changed" and len(calls) == 2


def test_path_replacement_and_closed_handle_do_not_query_other_directory(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with SafeFiles(root) as files:
        root.rename(tmp_path / "preserved")
        root.mkdir(mode=0o700)
        with pytest.raises(ContextError) as caught:
            files.require_private_root()
        assert caught.value.code == "storage_unavailable"
    with pytest.raises(ContextError) as caught:
        files.require_private_root()
    assert caught.value.code == "storage_unavailable"


def test_system_credentials_delegate_private_root_without_posix_descriptor():
    calls = []
    facade = SimpleNamespace(require_private_root=lambda: calls.append("private-root"))
    _private_root(facade)
    assert calls == ["private-root"]


def test_private_directory_contents_do_not_invalidate_ownership(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    fstat = module.os.fstat
    calls = []
    with SafeFiles(root) as files:

        def changing(fd):
            result = fstat(fd)
            calls.append(fd)
            if len(calls) == 1:
                (root / "original.txt").touch(mode=0o600)
            return result

        monkeypatch.setattr(module.os, "fstat", changing)
        files.require_private_root()
        assert len(calls) == 2
        assert (root / "original.txt").exists()
