"""Offline installation of original inert fixture bytes; no downloaded/executed tools."""

import hashlib
import io
import json
import os
import socket
import stat
import struct
import subprocess
import tarfile
import threading
import zipfile
from dataclasses import FrozenInstanceError, replace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure import runtime_dependencies as dependencies
from collection_context.infrastructure import runtime_installation as installation
from collection_context.infrastructure.files import SafeFiles

PAYLOADS = {
    "tools/ffmpeg": b"Original inert fixture ffmpeg bytes, never execute.",
    "tools/ffprobe": b"Original inert fixture ffprobe bytes, never execute.",
}


@pytest.fixture(autouse=True)
def no_network_or_execution(monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("Offline installer must not execute programs or access network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(dependencies.metadata, "version", lambda _: "1.63.0")


def archive_bytes(kind="zip", members=None):
    entries = list(PAYLOADS.items()) if members is None else members
    stream = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries:
                if isinstance(name, zipfile.ZipInfo):
                    info = name
                else:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, value)
    else:
        with tarfile.open(fileobj=stream, mode="w:xz" if kind == "tar.xz" else "w:gz") as archive:
            for name, value in entries:
                info = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                if info.isreg():
                    info.size = len(value)
                archive.addfile(info, io.BytesIO(value) if info.isreg() else None)
    return stream.getvalue()


def plan_for(data, kind="zip", *, identity="original-fixture", browser=False):
    host = dependencies._host()
    if browser:
        tools = (
            installation.ToolSpec(
                "chromium_headless_shell",
                "tools/browser",
                len(PAYLOADS["tools/ffmpeg"]),
                hashlib.sha256(PAYLOADS["tools/ffmpeg"]).hexdigest(),
                "153.0.0",
                "BSD-3-Clause",
                playwright_package_version="1.63.0",
                playwright_revision="1243",
            ),
        )
    else:
        tools = tuple(
            installation.ToolSpec(
                name.rsplit("/", 1)[1],
                name,
                len(value),
                hashlib.sha256(value).hexdigest(),
                "8.1.3",
                "LGPL-2.1-or-later",
                build_version="8.1.3-original-build",
            )
            for name, value in PAYLOADS.items()
        )
    return installation.ArtifactPlan(
        identity,
        host["system"],
        host["arch"],
        "1.0",
        "https://example.org/original-fixture",
        hashlib.sha256(data).hexdigest(),
        len(data),
        kind,
        tools,
    )


def fixture_installer(tmp_path, *, kind="zip", members=None, data=None, plan=None, source=None, runtime=None):
    content = archive_bytes(kind, members) if data is None else data
    source_path = tmp_path / ("fixture-" + hashlib.sha256(content).hexdigest()[:12] + ".archive")
    source_path.write_bytes(content)
    descriptor = plan or plan_for(content, kind)
    calls = []

    def provide(actual):
        calls.append(actual.id)
        return source(actual, source_path) if source is not None else source_path

    root = runtime or tmp_path / "runtime 中文"
    library = tmp_path / "library not initialized"
    installer = installation.RuntimeInstaller(
        root,
        library_dir=library,
        catalog={descriptor.id: descriptor},
        _archive_source=provide,
    )
    return installer, root, library, descriptor, source_path, calls


def rejected(call, code=None):
    with pytest.raises(ContextError) as caught:
        call()
    if code is not None:
        assert caught.value.code == code
    assert "private-original-error" not in caught.value.message
    assert "original-fixture" not in caught.value.message
    assert "example.org" not in caught.value.message
    return caught.value


def install(installer, descriptor, **kwargs):
    return installer.install(descriptor.id, installation_confirmed=True, **kwargs)


@pytest.mark.parametrize("kind", ["zip", "tar.xz", "tar.gz"])
def test_install_static_receipt_modes_and_read_only_constructor(tmp_path, kind):
    runner, root, library, descriptor, _, calls = fixture_installer(tmp_path, kind=kind)
    assert not root.exists() and not library.exists() and calls == []
    result = install(runner, descriptor)
    assert result["state"] == "installed"
    assert result["installed_roles"] == ["ffmpeg", "ffprobe"]
    assert result["static_verified"] is True and result["functional_verified"] is False
    assert calls == [descriptor.id] and not library.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    receipt = root / dependencies.RECEIPT_NAME
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    registry = dependencies.RuntimeDependencies(root, library_dir=library)
    for role in result["installed_roles"]:
        tool = registry.resolve(role)
        assert tool.path.read_bytes() == PAYLOADS["tools/" + role]
        assert stat.S_IMODE(tool.path.stat().st_mode) == 0o700
        assert tool.static_verified is True and tool.functional_verified is False
    assert not list(root.rglob(".source.archive"))
    assert not list((root / "generations").rglob(dependencies.RECEIPT_NAME))


@pytest.mark.parametrize("confirmed", [False, None, 1, "yes"])
def test_explicit_boolean_confirmation_no_source_or_directory(tmp_path, confirmed):
    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path)
    rejected(
        lambda: runner.install(descriptor.id, installation_confirmed=confirmed),
        "runtime_install_confirmation_required",
    )
    assert not root.exists() and calls == []


@pytest.mark.parametrize("identity", ["unknown", "https://example.org/arbitrary", "../escape", None, []])
def test_only_fixed_id_no_user_url(tmp_path, identity):
    (
        runner,
        root,
        _,
        _,
        _,
        _,
    ) = fixture_installer(tmp_path)
    rejected(lambda: runner.install(identity, installation_confirmed=True), "runtime_install_invalid")
    assert not root.exists()


def test_no_default_source_never_claims_network(tmp_path):
    descriptor = plan_for(archive_bytes())
    root = tmp_path / "missing-runtime"
    runner = installation.RuntimeInstaller(
        root, library_dir=tmp_path / "lib", catalog={descriptor.id: descriptor}
    )
    rejected(lambda: runner.install(descriptor.id), "runtime_install_confirmation_required")
    error = rejected(lambda: install(runner, descriptor), "runtime_install_source_unavailable")
    assert "没有执行网络下载" in error.message and not root.exists()


def test_host_mismatch_no_source(tmp_path):
    descriptor = replace(
        plan_for(archive_bytes()),
        host_system="Linux" if dependencies._host()["system"] != "Linux" else "Darwin",
    )
    runner, root, _, _, _, calls = fixture_installer(tmp_path, plan=descriptor)
    rejected(lambda: install(runner, descriptor), "runtime_install_host_mismatch")
    assert calls == [] and not root.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../escape"},
        {"host_system": []},
        {"host_arch": "unknown"},
        {"bytes": True},
        {"sha256": "A" * 64},
        {"source_url": "https://user:private@example.org/file"},
        {"source_url": "https://example.org/file?private=key"},
        {"archive_type": "rar"},
        {"archive_type": []},
        {"tools": []},
        {"tools": ()},
        {"version": "private\nvalue"},
    ],
)
def test_fixed_catalog_schema_rejected_before_writes(tmp_path, change):
    descriptor = replace(plan_for(archive_bytes()), **change)
    root = tmp_path / "absent"
    rejected(
        lambda: installation.RuntimeInstaller(
            root, library_dir=tmp_path / "lib", catalog={descriptor.id: descriptor}
        ),
        "runtime_install_invalid",
    )
    assert not root.exists()


def test_tool_pair_and_frozen_catalog(tmp_path):
    descriptor = plan_for(archive_bytes())
    rejected(
        lambda: fixture_installer(tmp_path, plan=replace(descriptor, tools=descriptor.tools[:1])),
        "runtime_install_invalid",
    )
    with pytest.raises(FrozenInstanceError):
        descriptor.id = "changed"
    catalog = {descriptor.id: descriptor}
    root = tmp_path / "frozen"
    runner = installation.RuntimeInstaller(root, library_dir=tmp_path / "lib", catalog=catalog)
    catalog.clear()
    rejected(lambda: install(runner, descriptor), "runtime_install_source_unavailable")


def test_inconsistent_pair_build_rejected_at_construction(tmp_path):
    descriptor = plan_for(archive_bytes())
    descriptor = replace(
        descriptor, tools=(descriptor.tools[0], replace(descriptor.tools[1], build_version="different"))
    )
    rejected(lambda: fixture_installer(tmp_path, plan=descriptor), "runtime_install_invalid")


def test_platform_without_safe_primitives_constructor_no_creation(tmp_path, monkeypatch):
    def unavailable():
        raise ContextError("unsupported_platform", "固定平台不可用。")

    monkeypatch.setattr(installation, "require_safe_files_runtime", unavailable)
    root = tmp_path / "unsupported"
    rejected(
        lambda: installation.RuntimeInstaller(root, library_dir=tmp_path / "lib", catalog={}),
        "unsupported_platform",
    )
    assert not root.exists()


@pytest.mark.parametrize("case", ["same", "child", "parent", "relative", "link"])
def test_runtime_library_overlap_or_alias(tmp_path, case):
    root, library = tmp_path / "runtime", tmp_path / "library"
    if case == "same":
        library = root
    elif case == "child":
        library = root / "inside"
    elif case == "parent":
        root = library / "inside"
    elif case == "relative":
        root = root.relative_to(tmp_path)
    else:
        library.mkdir()
        root.symlink_to(library, target_is_directory=True)
    rejected(lambda: installation.RuntimeInstaller(root, library_dir=library, catalog={}))


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "folder/../escape",
        "folder//bad",
        "C:/bad",
        "folder\\bad",
        "./bad",
        "bad\nname",
        ".source.archive",
        dependencies.RECEIPT_NAME,
    ],
)
@pytest.mark.parametrize("kind", ["zip", "tar.xz"])
def test_archive_path_escape_rejected(tmp_path, kind, name):
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, kind=kind, members=[(name, b"bad")])
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")
    assert not (root / dependencies.RECEIPT_NAME).exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "members",
    [
        [("duplicate", b"a"), ("duplicate", b"b")],
        [("Folder/a", b"a"), ("folder/b", b"b")],
        [("é/a", b"a"), ("e\u0301/b", b"b")],
        [("file", b"a"), ("file/child", b"b")],
        [("dir/child", b"a"), ("dir", b"b")],
    ],
)
@pytest.mark.parametrize("kind", ["zip", "tar.xz"])
def test_duplicates_aliases_and_file_ancestors(tmp_path, kind, members):
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, kind=kind, members=members)
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")
    assert not (root / dependencies.RECEIPT_NAME).exists()


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.GNUTYPE_SPARSE,
    ],
)
def test_tar_links_and_special_entries_rejected(tmp_path, kind):
    info = tarfile.TarInfo("unsafe")
    info.type, info.linkname = kind, "../../outside"
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, kind="tar.xz", members=[(info, b"")])
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")
    assert not (root / dependencies.RECEIPT_NAME).exists()


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600])
def test_zip_unix_links_or_special_rejected(tmp_path, mode):
    info = zipfile.ZipInfo("unsafe")
    info.external_attr = mode << 16
    runner, _, _, descriptor, _, _ = fixture_installer(tmp_path, members=[(info, b"outside")])
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")


def test_unknown_zip_extra_and_zip64_rejected(tmp_path):
    info = zipfile.ZipInfo("unsafe")
    info.extra = struct.pack("<HH", 0x000D, 0)
    runner, _, _, descriptor, _, _ = fixture_installer(tmp_path, members=[(info, b"bad")])
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")
    data = bytearray(archive_bytes())
    eocd = data.rfind(b"PK\x05\x06")
    struct.pack_into("<H", data, eocd + 10, 65535)
    runner, _, _, descriptor, _, _ = fixture_installer(tmp_path, data=bytes(data), runtime=tmp_path / "zip64")
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")


@pytest.mark.parametrize(
    "constant,value",
    [
        ("MAX_MEMBERS", 1),
        ("MAX_UNPACKED_BYTES", 1),
        ("MAX_ZIP_DIRECTORY_BYTES", 1),
    ],
)
def test_archive_limits_before_publication(tmp_path, monkeypatch, constant, value):
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path)
    monkeypatch.setattr(installation, constant, value)
    rejected(lambda: install(runner, descriptor), "runtime_install_limit")
    assert not (root / dependencies.RECEIPT_NAME).exists()


@pytest.mark.parametrize("kind", ["zip", "tar.xz"])
def test_per_member_limit_in_non_tool_files(tmp_path, monkeypatch, kind):
    runner, root, _, descriptor, _, _ = fixture_installer(
        tmp_path, kind=kind, members=[("oversized", b"a" * 100)]
    )
    monkeypatch.setattr(installation, "MAX_MEMBER_BYTES", 99)
    rejected(lambda: install(runner, descriptor), "runtime_install_limit")
    assert not (root / dependencies.RECEIPT_NAME).exists()


def test_archive_and_tool_hard_limits_catalog(tmp_path, monkeypatch):
    descriptor = plan_for(archive_bytes())
    monkeypatch.setattr(installation, "MAX_ARCHIVE_BYTES", descriptor.bytes - 1)
    rejected(lambda: fixture_installer(tmp_path, plan=descriptor), "runtime_install_invalid")
    monkeypatch.setattr(installation, "MAX_ARCHIVE_BYTES", descriptor.bytes)
    monkeypatch.setattr(installation, "MAX_TOOL_BYTES", descriptor.tools[0].bytes - 1)
    rejected(lambda: fixture_installer(tmp_path, plan=descriptor), "runtime_install_invalid")


def test_exact_limit_boundary_and_bounded_chunk_copy(tmp_path, monkeypatch):
    data = archive_bytes()
    monkeypatch.setattr(installation, "MAX_ARCHIVE_BYTES", len(data))
    monkeypatch.setattr(installation, "MAX_MEMBER_BYTES", max(map(len, PAYLOADS.values())))
    monkeypatch.setattr(installation, "MAX_UNPACKED_BYTES", sum(map(len, PAYLOADS.values())))
    monkeypatch.setattr(installation, "MAX_MEMBERS", len(PAYLOADS))
    monkeypatch.setattr(installation, "COPY_CHUNK_BYTES", 7)
    sizes = []
    original = installation.RuntimeInstaller._write_all

    def bounded(fd, content):
        sizes.append(len(content))
        assert len(content) <= 7
        return original(fd, content)

    monkeypatch.setattr(installation.RuntimeInstaller, "_write_all", staticmethod(bounded))
    runner, _, _, descriptor, _, _ = fixture_installer(tmp_path, data=data)
    assert install(runner, descriptor)["state"] == "installed" and sizes


def test_pax_metadata_bounded_before_parse(tmp_path, monkeypatch):
    info = tarfile.TarInfo("file")
    info.pax_headers = {"comment": "x" * 200}
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, kind="tar.xz", members=[(info, b"data")])
    monkeypatch.setattr(installation, "MAX_TAR_METADATA_BYTES", 50)
    rejected(lambda: install(runner, descriptor), "runtime_install_limit")
    assert not (root / dependencies.RECEIPT_NAME).exists()


def test_tar_header_count_bounded_before_member_reads(tmp_path, monkeypatch):
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, kind="tar.xz")
    monkeypatch.setattr(installation, "MAX_MEMBERS", 1)
    rejected(lambda: install(runner, descriptor), "runtime_install_limit")
    assert not (root / dependencies.RECEIPT_NAME).exists()


@pytest.mark.parametrize("kind", ["zip", "tar.xz"])
def test_explicit_directory_after_implicit_parent_is_valid(tmp_path, kind):
    if kind == "zip":
        directory = zipfile.ZipInfo("tools/")
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    else:
        directory = tarfile.TarInfo("tools")
        directory.type = tarfile.DIRTYPE
    members = [
        ("tools/ffmpeg", PAYLOADS["tools/ffmpeg"]),
        (directory, b""),
        ("tools/ffprobe", PAYLOADS["tools/ffprobe"]),
    ]
    runner, _, _, descriptor, _, _ = fixture_installer(tmp_path, kind=kind, members=members)
    assert install(runner, descriptor)["state"] == "installed"


@pytest.mark.parametrize("change", [{"sha256": "0" * 64}, {"bytes": 1}])
def test_source_archive_hash_size_integrity(tmp_path, change):
    descriptor = replace(plan_for(archive_bytes()), **change)
    runner, root, _, _, _, calls = fixture_installer(tmp_path, plan=descriptor)
    rejected(lambda: install(runner, descriptor), "runtime_install_integrity")
    assert calls == [descriptor.id] and not (root / dependencies.RECEIPT_NAME).exists()


def test_tool_hash_integrity_no_receipt(tmp_path):
    descriptor = plan_for(archive_bytes())
    descriptor = replace(
        descriptor, tools=(replace(descriptor.tools[0], sha256="0" * 64), descriptor.tools[1])
    )
    runner, root, _, _, _, _ = fixture_installer(tmp_path, plan=descriptor)
    rejected(lambda: install(runner, descriptor))
    assert not (root / dependencies.RECEIPT_NAME).exists()


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo"])
def test_source_not_regular_unique_file_no_blocking(tmp_path, kind):
    def source(_, path):
        alternative = tmp_path / "unsafe-source"
        if kind == "hardlink":
            os.link(path, alternative)
            return path
        if kind == "symlink":
            alternative.symlink_to(path)
        else:
            os.mkfifo(alternative)
        return alternative

    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path, source=source)
    rejected(lambda: install(runner, descriptor))
    assert not (root / dependencies.RECEIPT_NAME).exists()


def test_old_generation_and_precommit_snapshot_preserved(tmp_path):
    runner, root, library, descriptor, _, _ = fixture_installer(tmp_path)
    first = install(runner, descriptor)
    old = (root / dependencies.RECEIPT_NAME).read_bytes()
    second = install(runner, descriptor)
    assert first["generation"] != second["generation"]
    assert (root / "generations" / first["generation"] / "tools/ffmpeg").read_bytes() == PAYLOADS[
        "tools/ffmpeg"
    ]
    assert (root / "receipts" / (second["generation"] + ".json")).read_bytes() == old
    receipt = json.loads((root / dependencies.RECEIPT_NAME).read_bytes())
    assert second["generation"] in receipt["tools"]["ffmpeg"]["relative_path"]
    assert dependencies.RuntimeDependencies(root, library_dir=library).resolve("ffmpeg").static_verified


def test_install_different_role_keeps_verified_old_roles(tmp_path):
    runner, root, library, descriptor, _, _ = fixture_installer(tmp_path)
    first = install(runner, descriptor)
    data = archive_bytes(members=[("tools/browser", PAYLOADS["tools/ffmpeg"])])
    browser = plan_for(data, identity="browser-original", browser=True)
    second, _, _, _, _, _ = fixture_installer(tmp_path, plan=browser, data=data, runtime=root)
    assert install(second, browser)["installed_roles"] == ["chromium_headless_shell"]
    registry = dependencies.RuntimeDependencies(root, library_dir=library)
    assert first["generation"] in str(registry.resolve("ffmpeg").path)
    assert registry.resolve("chromium_headless_shell").functional_verified is False


def test_retained_role_changes_during_source_callback_not_published(tmp_path):
    runner, root, library, descriptor, _, _ = fixture_installer(tmp_path)
    install(runner, descriptor)
    old_bytes = (root / dependencies.RECEIPT_NAME).read_bytes()
    old_tool = dependencies.RuntimeDependencies(root, library_dir=library).resolve("ffmpeg").path
    data = archive_bytes(members=[("tools/browser", PAYLOADS["tools/ffmpeg"])])
    browser = plan_for(data, identity="browser-original", browser=True)

    def changed(_, path):
        old_tool.write_bytes(b"corrupt after initial old-role check")
        return path

    second, _, _, _, _, calls = fixture_installer(
        tmp_path, plan=browser, data=data, runtime=root, source=changed
    )
    rejected(lambda: install(second, browser))
    assert calls == [browser.id] and (root / dependencies.RECEIPT_NAME).read_bytes() == old_bytes


def test_final_generation_hash_revalidated_before_publication(tmp_path, monkeypatch):
    runner, root, _, descriptor, _, _ = fixture_installer(tmp_path)
    old = SafeFiles.write

    def changed(files, relative, data, **kwargs):
        old(files, relative, data, **kwargs)
        if files.root.parent.name == "generations" and relative == dependencies.RECEIPT_NAME:
            (files.root / "tools/ffmpeg").write_bytes(b"corrupt after stage hash")

    monkeypatch.setattr(SafeFiles, "write", changed)
    rejected(lambda: install(runner, descriptor))
    assert not (root / dependencies.RECEIPT_NAME).exists()


def test_browser_sdk_mismatch_old_role_is_not_merged(tmp_path, monkeypatch):
    data = archive_bytes(members=[("tools/browser", PAYLOADS["tools/ffmpeg"])])
    browser = plan_for(data, identity="browser-original", browser=True)
    runner, root, _, _, _, calls = fixture_installer(tmp_path, plan=browser, data=data)
    install(runner, browser)
    before = (root / dependencies.RECEIPT_NAME).read_bytes()
    monkeypatch.setattr(dependencies.metadata, "version", lambda _: "1.62.0")
    rejected(lambda: install(runner, browser), "runtime_install_existing_invalid")
    assert len(calls) == 1 and (root / dependencies.RECEIPT_NAME).read_bytes() == before


@pytest.mark.parametrize("damage", ["receipt", "tool", "permissions", "link"])
def test_invalid_old_installation_refuses_source_and_preserves_receipt(tmp_path, damage):
    runner, root, library, descriptor, _, calls = fixture_installer(tmp_path)
    install(runner, descriptor)
    if damage == "receipt":
        (root / dependencies.RECEIPT_NAME).write_bytes(b"bad private-original-error")
    else:
        path = dependencies.RuntimeDependencies(root, library_dir=library).resolve("ffmpeg").path
        if damage == "tool":
            path.write_bytes(b"changed")
        elif damage == "permissions":
            path.chmod(0o777)
        else:
            os.link(path, path.parent / "linked")
    before = (root / dependencies.RECEIPT_NAME).read_bytes()
    rejected(lambda: install(runner, descriptor), "runtime_install_existing_invalid")
    assert len(calls) == 1 and (root / dependencies.RECEIPT_NAME).read_bytes() == before


def test_existing_root_permissions_no_lock_mutation(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    runner, _, _, descriptor, _, calls = fixture_installer(tmp_path, runtime=root)
    rejected(lambda: install(runner, descriptor), "runtime_install_unsafe")
    assert calls == [] and list(root.iterdir()) == []


def test_cancel_before_start_no_directory_or_source(tmp_path):
    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path)
    stop = threading.Event()
    stop.set()
    rejected(lambda: install(runner, descriptor, stop=stop), "runtime_install_cancelled")
    assert calls == [] and not root.exists()


@pytest.mark.parametrize("boundary", ["source", "copy", "unpack", "before-publication"])
def test_stop_preserves_old_receipt_and_does_not_retry(tmp_path, monkeypatch, boundary):
    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path)
    install(runner, descriptor)
    before = (root / dependencies.RECEIPT_NAME).read_bytes()
    stop = threading.Event()
    if boundary == "source":
        old = runner._source

        def stopped(plan):
            result = old(plan)
            stop.set()
            return result

        runner._source = stopped
    elif boundary == "copy":
        old = installation.RuntimeInstaller._write_all

        def stopped(fd, data):
            old(fd, data)
            stop.set()

        monkeypatch.setattr(installation.RuntimeInstaller, "_write_all", staticmethod(stopped))
    elif boundary == "unpack":
        old = runner._unpack

        def stopped(*args):
            old(*args)
            stop.set()

        monkeypatch.setattr(runner, "_unpack", stopped)
    else:
        old = SafeFiles.write

        def stopped(files, relative, data, **kwargs):
            old(files, relative, data, **kwargs)
            if relative.startswith("receipts/"):
                stop.set()

        monkeypatch.setattr(SafeFiles, "write", stopped)
    rejected(lambda: install(runner, descriptor, stop=stop), "runtime_install_cancelled")
    assert len(calls) == 2 and (root / dependencies.RECEIPT_NAME).read_bytes() == before


def test_live_install_lease_refuses_second_provider(tmp_path):
    entered, release = threading.Event(), threading.Event()
    errors = []

    def blocked(_, path):
        entered.set()
        assert release.wait(5)
        return path

    first, root, _, descriptor, _, _ = fixture_installer(tmp_path, source=blocked)
    second, _, _, _, _, calls = fixture_installer(tmp_path, runtime=root)

    def work():
        try:
            install(first, descriptor)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(5)
        rejected(lambda: install(second, descriptor), "runtime_install_busy")
        assert calls == []
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private-original-error"),
        SystemExit("private-original-error"),
        ContextError("runtime_install_failed", "private-original-error"),
        ContextError("runtime_install_integrity", "private-original-error"),
    ],
)
def test_unknown_and_trusted_source_errors_fixed_sanitized_no_retry(tmp_path, error, capsys):
    def failed(_, __):
        raise error

    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path, source=failed)
    rejected(lambda: install(runner, descriptor))
    assert calls == [descriptor.id] and not (root / dependencies.RECEIPT_NAME).exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("after_replace", [False, True])
def test_publication_failure_outcome_unknown_no_rollback_and_old_snapshot(
    tmp_path, monkeypatch, after_replace
):
    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path)
    install(runner, descriptor)
    before = (root / dependencies.RECEIPT_NAME).read_bytes()
    old = SafeFiles.write

    def failed(files, relative, data, **kwargs):
        if files.root == root and relative == dependencies.RECEIPT_NAME:
            if after_replace:
                old(files, relative, data, **kwargs)
            raise OSError("private-original-error")
        return old(files, relative, data, **kwargs)

    monkeypatch.setattr(SafeFiles, "write", failed)
    rejected(lambda: install(runner, descriptor), "runtime_install_outcome_unknown")
    assert len(calls) == 2
    assert (
        (root / dependencies.RECEIPT_NAME).read_bytes() != before
        if after_replace
        else (root / dependencies.RECEIPT_NAME).read_bytes() == before
    )
    assert before in [path.read_bytes() for path in (root / "receipts").iterdir()]
    assert len(list((root / "generations").iterdir())) == 2


def test_source_callback_root_swap_cannot_publish(tmp_path):
    runner, root, _, descriptor, _, calls = fixture_installer(tmp_path)

    def replaced(_, path):
        root.rename(tmp_path / "moved-generation")
        root.mkdir(mode=0o700)
        return path

    runner._source = lambda plan: replaced(
        plan, tmp_path / ("fixture-" + descriptor.sha256[:12] + ".archive")
    )
    rejected(lambda: install(runner, descriptor))
    assert not (root / dependencies.RECEIPT_NAME).exists()
