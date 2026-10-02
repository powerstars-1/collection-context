"""Bounded, descriptor-based metadata enumeration needed by legacy read-only mode."""

from __future__ import annotations

import os

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def test_directory_enumeration_classifies_links_without_following_them(tmp_path):
    entries = tmp_path / "资料"
    entries.mkdir()
    (entries / "a.md").write_text("regular", encoding="utf-8")
    (entries / "z").mkdir()
    outside = tmp_path / "外部.txt"
    outside.write_text("not readable through alias", encoding="utf-8")
    os.link(outside, entries / "b-hard.md")
    (entries / "c-soft.md").symlink_to(outside)
    (entries / "d-folder").symlink_to(tmp_path, target_is_directory=True)
    with SafeFiles(tmp_path) as files:
        assert files.list_directory("资料", max_entries=5) == [
            {"name": "a.md", "kind": "file"},
            {"name": "b-hard.md", "kind": "unsafe"},
            {"name": "c-soft.md", "kind": "unsafe"},
            {"name": "d-folder", "kind": "unsafe"},
            {"name": "z", "kind": "directory"},
        ]
        failure("scan_limit", lambda: files.list_directory("资料", max_entries=4))


@pytest.mark.parametrize("limit", [0, -1, True, "3", 100_001])
def test_invalid_enumeration_limit_is_rejected(tmp_path, limit):
    with SafeFiles(tmp_path) as files:
        failure("invalid_argument", lambda: files.list_directory("missing", max_entries=limit))


def test_directory_paths_cannot_traverse_links_or_escape_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    with SafeFiles(tmp_path) as files:
        failure("forbidden_path", lambda: files.list_directory("alias"))
        failure("forbidden_path", lambda: files.list_directory("../target"))
        failure("not_found", lambda: files.list_directory("missing"))


def test_replaced_directory_during_enumeration_is_not_returned_as_current(tmp_path, monkeypatch):
    target = tmp_path / "资料"
    target.mkdir()
    (target / "old.md").write_text("old", encoding="utf-8")
    real_scandir = os.scandir

    class ReplacingScan:
        def __init__(self, fd):
            self.scan = real_scandir(fd)

        def __enter__(self):
            target.rename(tmp_path / "retired")
            target.mkdir()
            (target / "new.md").write_text("new", encoding="utf-8")
            return self.scan

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(os, "scandir", ReplacingScan)
    with SafeFiles(tmp_path) as files:
        failure("version_changed", lambda: files.list_directory("资料"))
