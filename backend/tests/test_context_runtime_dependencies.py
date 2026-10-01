"""Original, non-executable fixtures for a no-download/no-execution receipt reader."""

import copy
import hashlib
import json
import os
import platform
import socket
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_dependencies as runtime


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "owned runtime 中文"
    root.mkdir(mode=0o700)
    library = tmp_path / "separate library"
    tools = {}
    for role in ("ffmpeg", "ffprobe", "chromium", "chromium_headless_shell"):
        path = root / "tools" / role / "original-fixture"
        path.parent.mkdir(mode=0o700, parents=True)
        content = f"Original test bytes for {role}; never execute.".encode()
        path.write_bytes(content)
        path.chmod(0o700)
        tool = {
            "relative_path": path.relative_to(root).as_posix(),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "version": "8.1.3" if role.startswith("ff") else "153.0.8010.12",
            "source_url": "https://example.org/public/original-fixture",
            "license_id": "LGPL-2.1-or-later" if role.startswith("ff") else "BSD-3-Clause",
        }
        if role.startswith("ff"):
            tool["build_version"] = "8.1.3-original-test-build"
        else:
            tool["playwright"] = {"package_version": "1.63.0", "revision": "1243"}
        tools[role] = tool
    payload = {"schema_version": 1, "host": runtime._host(), "tools": tools}
    write_receipt(root, payload)
    return root, library, payload


def write_receipt(root, payload):
    path = root / runtime.RECEIPT_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def registry(installation):
    root, library, _ = installation
    return runtime.RuntimeDependencies(root, library_dir=library)


def rejected(call, code=None):
    with pytest.raises(ContextError) as caught:
        call()
    assert caught.value.code in runtime._MESSAGES
    if code:
        assert caught.value.code == code
    assert "original-fixture" not in caught.value.message
    assert "example.org" not in caught.value.message
    return caught.value


def test_resolve_fixed_tools_metadata_not_functional_proof(installation, monkeypatch):
    monkeypatch.setattr(runtime.metadata, "version", lambda _: "1.63.0")
    root, _, payload = installation
    for role in runtime.TOOL_ROLES:
        tool = registry(installation).resolve(role)
        assert tool.path == root / payload["tools"][role]["relative_path"]
        assert tool.static_verified is True
        assert tool.functional_verified is False
        assert tool.revision_verified is False
        assert tool.bytes == payload["tools"][role]["bytes"]
        with pytest.raises(FrozenInstanceError):
            tool.version = "changed"


@pytest.mark.parametrize("role", ["bash", "python", "../ffmpeg", "", None, 1])
def test_role_whitelist(installation, role):
    rejected(lambda: registry(installation).resolve(role), "runtime_dependency_invalid")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(schema_version=2),
        lambda p: p.update(secret="must not be accepted"),
        lambda p: p["host"].update(extra="unknown"),
        lambda p: p["tools"].update(shell={}),
        lambda p: p["tools"]["ffmpeg"].update(command="not allowed"),
        lambda p: p["tools"]["ffmpeg"].update(bytes=True),
        lambda p: p["tools"]["ffmpeg"].update(bytes=0),
        lambda p: p["tools"]["ffmpeg"].update(bytes=runtime.MAX_TOOL_BYTES + 1),
        lambda p: p["tools"]["ffmpeg"].update(sha256="invalid"),
        lambda p: p["tools"]["ffmpeg"].update(version="value\nprivate"),
        lambda p: p["tools"]["ffmpeg"].update(license_id=""),
        lambda p: p["tools"].pop("ffprobe"),
        lambda p: p["tools"]["ffprobe"].update(build_version="other-build"),
        lambda p: p["tools"]["ffprobe"].update(relative_path=p["tools"]["ffmpeg"]["relative_path"]),
        lambda p: p["tools"]["chromium"]["playwright"].update(revision="secret"),
        lambda p: p["tools"]["chromium_headless_shell"]["playwright"].update(revision="1242"),
    ],
)
def test_strict_receipt_schema(installation, mutation):
    root, _, original = installation
    payload = copy.deepcopy(original)
    mutation(payload)
    write_receipt(root, payload)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_invalid")


@pytest.mark.parametrize(
    "path",
    [
        "/bin/sh",
        "../ffmpeg",
        "tools/../file",
        "tools//file",
        "C:/file",
        "tools\\file",
        "./file",
        "tools/file\x00",
    ],
)
def test_receipt_paths_reject_escape_or_ambiguous_syntax(installation, path):
    root, _, payload = installation
    payload["tools"]["ffmpeg"]["relative_path"] = path
    write_receipt(root, payload)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_invalid")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/tool",
        "https://localhost/file",
        "https://127.0.0.1/file",
        "https://10.0.0.1/file",
        "https://[::1]/file",
        "https://host.local/file",
        "https://user:password@example.org/file",
        "https://example.org/file?key=private",
        "https://example.org/file#private",
        "https://example.org:99999/file",
    ],
)
def test_source_url_metadata_never_accepts_credentials_or_private_destination(installation, url):
    root, _, payload = installation
    payload["tools"]["ffmpeg"]["source_url"] = url
    write_receipt(root, payload)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_invalid")


@pytest.mark.parametrize("part", ["system", "arch"])
def test_host_mismatch(installation, part):
    root, _, payload = installation
    payload["host"][part] = "other"
    write_receipt(root, payload)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_host_mismatch")


def test_host_aliases_only_not_cwd_or_environment(installation, monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert runtime._host() == {"system": "Windows", "arch": "x86_64"}
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert runtime._host()["arch"] == "arm64"
    monkeypatch.setattr(platform, "system", lambda: installation[2]["host"]["system"])
    monkeypatch.setattr(platform, "machine", lambda: installation[2]["host"]["arch"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FFMPEG", "/private/ignored")
    assert registry(installation).resolve("ffmpeg").version == "8.1.3"


@pytest.mark.parametrize("kind", ["relative", "root", "home", "same", "inside", "ancestor"])
def test_directory_scope_is_explicit_and_outside_library(installation, kind):
    root, library, _ = installation
    choices = {
        "relative": (Path("relative"), library),
        "root": (Path(root.anchor), library),
        "home": (Path.home(), library),
        "same": (root, root),
        "inside": (root, root.parent),
        "ancestor": (root, root / "library"),
    }
    target, excluded = choices[kind]
    rejected(lambda: runtime.RuntimeDependencies(target, library_dir=excluded), "runtime_dependency_unsafe")


@pytest.mark.parametrize("kind", ["root", "ancestor", "parent", "file", "receipt", "hardlink"])
def test_links_cannot_redirect_runtime_files(installation, kind, tmp_path):
    root, library, payload = installation
    path = root / payload["tools"]["ffmpeg"]["relative_path"]
    if kind in {"root", "ancestor"}:
        link = tmp_path / "linked"
        link.symlink_to(root if kind == "root" else root.parent, target_is_directory=True)
        root = link if kind == "root" else link / root.name
    elif kind == "parent":
        directory = path.parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    elif kind == "hardlink":
        os.link(path, path.with_name("second-link"))
    else:
        selected = path if kind == "file" else root / runtime.RECEIPT_NAME
        moved = selected.with_name("moved")
        selected.rename(moved)
        selected.symlink_to(moved)
    rejected(
        lambda: runtime.RuntimeDependencies(root, library_dir=library).resolve("ffmpeg"),
        "runtime_dependency_unsafe",
    )


@pytest.mark.parametrize(
    "target,mode", [("root", 0o755), ("receipt", 0o644), ("tool", 0o777), ("tool", 0o600)]
)
def test_permissions_fail_closed(installation, target, mode):
    root, _, payload = installation
    paths = {
        "root": root,
        "receipt": root / runtime.RECEIPT_NAME,
        "tool": root / payload["tools"]["ffmpeg"]["relative_path"],
    }
    paths[target].chmod(mode)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_unsafe")


def test_wrong_owner_and_unverified_platform(installation, monkeypatch):
    monkeypatch.setattr(runtime.os, "getuid", lambda: -1)
    rejected(lambda: registry(installation), "runtime_dependency_unsafe")
    monkeypatch.undo()

    def unavailable():
        raise ContextError("unsupported_platform", "private platform details")

    monkeypatch.setattr(runtime, "require_safe_files_runtime", unavailable)
    rejected(lambda: registry(installation), "runtime_dependency_platform_unverified")


@pytest.mark.parametrize("change", ["content", "size", "missing"])
def test_integrity_and_missing_tool(installation, change):
    root, _, payload = installation
    path = root / payload["tools"]["ffmpeg"]["relative_path"]
    if change == "missing":
        path.unlink()
    elif change == "size":
        path.write_bytes(b"changed")
    else:
        path.write_bytes(b"X" * path.stat().st_size)
    rejected(
        lambda: registry(installation).resolve("ffmpeg"),
        "runtime_dependency_missing" if change == "missing" else "runtime_dependency_integrity",
    )


@pytest.mark.parametrize(
    "content", [b"x" * 65_537, b"{broken", b'{"schema_version":1,"schema_version":1}', b"[]", b"\xff"]
)
def test_malformed_or_oversized_receipt(installation, content):
    root, _, _ = installation
    (root / runtime.RECEIPT_NAME).write_bytes(content)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_invalid")


def test_browser_package_bound_revision_not_falsely_verified(installation, monkeypatch):
    monkeypatch.setattr(runtime.metadata, "version", lambda _: "other")
    rejected(lambda: registry(installation).resolve("chromium"), "runtime_dependency_version_mismatch")
    monkeypatch.setattr(runtime.metadata, "version", lambda _: "1.63.0")
    result = registry(installation).resolve("chromium")
    assert result.playwright_revision == "1243"
    assert result.revision_verified is False
    assert result.functional_verified is False


def test_rereads_receipt_and_rehashes_each_resolve(installation):
    root, _, payload = installation
    reader = registry(installation)
    first = reader.resolve("ffmpeg")
    path = first.path
    path.write_bytes(b"B" * first.bytes)
    rejected(lambda: reader.resolve("ffmpeg"), "runtime_dependency_integrity")
    payload["tools"]["ffmpeg"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_receipt(root, payload)
    second = reader.resolve("ffmpeg")
    assert second.path == first.path and second.sha256 != first.sha256


def test_file_changes_during_hash_are_rejected(installation, monkeypatch):
    root, _, payload = installation
    target = root / payload["tools"]["ffmpeg"]["relative_path"]
    reader = registry(installation)
    original_read = os.read
    changed = False

    def read(fd, size):
        nonlocal changed
        data = original_read(fd, size)
        if os.fstat(fd).st_ino == target.stat().st_ino and not changed:
            changed = True
            target.write_bytes(b"X" * target.stat().st_size)
        return data

    monkeypatch.setattr(runtime.os, "read", read)
    rejected(lambda: reader.resolve("ffmpeg"), "runtime_dependency_integrity")


def test_no_process_network_download_or_writes(installation, monkeypatch):
    reader = registry(installation)
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("No runtime side effects allowed")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    reader.resolve("ffmpeg")
    assert attempts == []


def test_missing_receipt_or_role_does_not_create_or_fallback(installation):
    root, _, payload = installation
    payload["tools"] = {}
    write_receipt(root, payload)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_missing")
    (root / runtime.RECEIPT_NAME).unlink()
    before = sorted(root.rglob("*"))
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_missing")
    assert sorted(root.rglob("*")) == before


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_non_regular_tool_rejected_without_blocking(installation, kind):
    root, _, payload = installation
    target = root / payload["tools"]["ffmpeg"]["relative_path"]
    target.unlink()
    if kind == "directory":
        target.mkdir(mode=0o700)
    else:
        os.mkfifo(target, mode=0o700)
    rejected(lambda: registry(installation).resolve("ffmpeg"), "runtime_dependency_unsafe")


def test_parent_directory_replaced_during_hash_cannot_return_new_path(installation, monkeypatch):
    root, _, payload = installation
    target = root / payload["tools"]["ffmpeg"]["relative_path"]
    reader = registry(installation)
    original_read = os.read
    original_inode = target.stat().st_ino
    changed = False

    def read(fd, size):
        nonlocal changed
        data = original_read(fd, size)
        if os.fstat(fd).st_ino == original_inode and not changed:
            changed = True
            parent = target.parent
            parent.rename(parent.with_name("original-moved"))
            parent.mkdir(mode=0o700)
            target.write_bytes(b"replacement must not be returned")
            target.chmod(0o700)
        return data

    monkeypatch.setattr(runtime.os, "read", read)
    rejected(lambda: reader.resolve("ffmpeg"), "runtime_dependency_integrity")


def test_runtime_root_replacement_after_constructor_is_rejected(installation):
    root, _, _ = installation
    reader = registry(installation)
    root.rename(root.with_name("original-runtime"))
    root.mkdir(mode=0o700)
    rejected(lambda: reader.resolve("ffmpeg"), "runtime_dependency_unsafe")


def test_browser_metadata_failure_is_sanitized(installation, monkeypatch):
    def failed(_):
        raise OSError("secret environment contents")

    monkeypatch.setattr(runtime.metadata, "version", failed)
    error = rejected(
        lambda: registry(installation).resolve("chromium"), "runtime_dependency_version_mismatch"
    )
    assert "secret" not in error.message


def test_bounded_reads_and_only_installation_metadata_accessed(installation, monkeypatch):
    reader = registry(installation)
    original_open, original_read = os.open, os.read
    opened, sizes = [], []

    def open_file(path, *args, **kwargs):
        opened.append(str(path))
        return original_open(path, *args, **kwargs)

    def read(fd, size):
        sizes.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(runtime.os, "open", open_file)
    monkeypatch.setattr(runtime.os, "read", read)
    reader.resolve("ffmpeg")
    assert set(opened) == {str(installation[0]), runtime.RECEIPT_NAME, "tools", "ffmpeg", "original-fixture"}
    assert all(0 < size <= 1_048_576 for size in sizes)
