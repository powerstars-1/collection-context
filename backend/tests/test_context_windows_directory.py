"""Bounded enumeration ABI/namespace tests; NOT Windows kernel acceptance.

Records are packed independently from the implementation ctypes structure.
The injected DLL models output bytes and name changes, not Windows sharing,
NTFS consistency, device latency, or real reparse behavior.
"""

from __future__ import annotations

import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest
from test_context_windows_native import code
from test_context_windows_publication import PublicationDLLs

from collection_context.infrastructure import platform_safety
from collection_context.infrastructure import windows_native as native
from collection_context.infrastructure.windows_files import WindowsFiles


def record(name, *, identity=b"i" * 16, attributes=0x80, tag=0):
    return name, identity, attributes, tag


def page(*entries):
    result = bytearray()
    for index, (name, identity, attributes, tag) in enumerate(entries):
        encoded = name.encode("utf-16-le")
        extent = 88 + len(encoded)
        following = (extent + 7) & ~7 if index < len(entries) - 1 else 0
        # Six LARGE_INTEGER fields, four DWORD fields, then FILE_ID_128.
        header = struct.pack(
            "<IIqqqqqqIIII16s", following, 0, 1, 1, 2, 3, 0, 0, attributes, len(encoded), 0, tag, identity
        )
        assert len(header) == 88
        result.extend(header + encoded)
        if following:
            result.extend(b"\x9d" * (following - extent))  # Nonzero padding must be ignored.
    return bytes(result)


class DirectoryDLLs(PublicationDLLs):
    def __init__(self):
        super().__init__()
        self.pages = None
        self.cursors = {}
        self.enumerations = []
        self.enumeration_hook = None
        self.end_hook = None
        self.end_error = 18
        self.enumeration_error = None
        self.metadata_hook = None

    def info(self, handle, kind, output, length):
        if kind not in {19, 20}:
            result = super().info(handle, kind, output, length)
            if self.metadata_hook is not None:
                self.metadata_hook(handle.value, kind, output)
            return result
        assert length == 65_536 and ctypes.addressof(output) % 8 == 0
        assert self.files[handle.value]["directory"]
        self.enumerations.append((handle.value, kind, length))
        if self.enumeration_error is not None:
            self.error = self.enumeration_error
            return 0
        if kind == 20:
            self.cursors[handle.value] = 0
        position = self.cursors[handle.value]
        pages = self.pages
        if pages is None:
            directory_id = int.from_bytes(self.files[handle.value]["id"], "little")
            entries = [
                record(name, identity=data["id"], attributes=data["attributes"], tag=data["tag"])
                for (parent_id, name), data in self.names.items()
                if parent_id == directory_id
            ]
            pages = [page(*entries)] if entries else []
        if position == len(pages):
            self.error = self.end_error
            if self.end_hook is not None:
                self.end_hook(handle.value)
            return 0
        body = pages[position]
        assert len(body) <= length
        ctypes.memmove(output, body, len(body))
        self.cursors[handle.value] += 1
        if self.enumeration_hook is not None:
            self.enumeration_hook(handle.value, position)
        return 1


@pytest.fixture
def listing():
    dlls = DirectoryDLLs()
    tree = WindowsFiles("C:\\原创库", _native=dlls.native)
    tree.mkdir("资料/作品")
    try:
        yield dlls, tree
    finally:
        tree.close()
        # SecurityDLLs separately reuses token HANDLE 777; account only for
        # file/directory allocations here, each closed exactly once on error too.
        assert sorted(value for value in dlls.closed if value in dlls.files) == list(
            range(100, dlls.next_handle)
        )


def directory_data(dlls, tree):
    first = dlls.names[dlls.name_key(tree.handle._value, "资料")]
    return dlls.names[(int.from_bytes(first["id"], "little"), "作品")]


def add_entry(dlls, tree, name, **changes):
    directory = directory_data(dlls, tree)
    # Original synthetic content only. No real filesystem IO or platform call.
    data = {
        **directory,
        "id": (10_000 + len(dlls.names)).to_bytes(16, "little"),
        "directory": False,
        "attributes": 0x80,
        "body": b"original fixture",
        **changes,
    }
    dlls.names[(int.from_bytes(directory["id"], "little"), name.casefold())] = data
    return data


def row_for(name, data, **changes):
    return record(
        name, **{"identity": data["id"], "attributes": data["attributes"], "tag": data["tag"], **changes}
    )


def assert_borrowed_root_only(dlls, tree):
    assert set(range(100, dlls.next_handle)) - set(dlls.closed) == {tree.handle._value}


def test_layout_pagination_restart_relative_metadata_and_least_rights(listing):
    dlls, tree = listing
    a = add_entry(dlls, tree, "a.md")
    z = add_entry(dlls, tree, "中文 😀.md")
    sub = add_entry(dlls, tree, "子目录", directory=True, attributes=0x10, links=3)
    dlls.pages = [
        page(record("."), record(".."), row_for("中文 😀.md", z)),
        page(row_for("子目录", sub), row_for("a.md", a)),
    ]
    native._check_layout()
    assert ctypes.sizeof(native.FileIdExtdDirectoryHeader) == 88
    assert native.FileIdExtdDirectoryHeader.FileAttributes.offset == 56
    assert native.FileIdExtdDirectoryHeader.FileNameLength.offset == 60
    assert native.FileIdExtdDirectoryHeader.FileId.offset == 72
    start, writes = len(dlls.opened), len(dlls.writes)
    expected = [
        {"name": name, "kind": kind}
        for name, kind in [("a.md", "file"), ("中文 😀.md", "file"), ("子目录", "directory")]
    ]
    assert tree.list_directory("资料/作品", max_entries=3) == expected
    assert [kind for _, kind, _ in dlls.enumerations] == [20, 19, 19]
    requests = dlls.opened[start:]
    enumerating = [request for request in requests if request["access"] == 0x001200A1]
    assert len(enumerating) == 1
    scan = enumerating[0]
    assert scan["name"] == "作品" and scan["root"] is not None
    assert scan["shares"] == 3 and scan["options"] == 0x00200021 and scan["attributes"] == 0x1040
    assert all(request["disposition"] == 1 and not request["security"] for request in requests)
    assert {request["access"] for request in requests} == {0x001200A0, 0x001200A1, 0x00120080}
    metadata = [request for request in requests if request["access"] == 0x00120080]
    listing_handle = dlls.enumerations[0][0]
    assert len(metadata) == 3 and all(request["root"] == listing_handle for request in metadata)
    assert not dlls.reads and len(dlls.writes) == writes
    assert_borrowed_root_only(dlls, tree)
    assert tree.list_directory("资料/作品", max_entries=3) == expected
    assert [kind for _, kind, _ in dlls.enumerations] == [20, 19, 19, 20, 19, 19]


def test_native_borrows_listing_handle_and_restarts_its_cursor(listing):
    dlls, tree = listing
    with dlls.native.open_relative(tree.handle, "资料", role="directory_listing") as handle:
        expected = [{"name": "作品", "kind": "directory"}]
        assert dlls.native.list_directory(handle) == expected
        assert dlls.native.list_directory(handle) == expected
        assert handle._value not in dlls.closed
        assert [kind for _, kind, _ in dlls.enumerations] == [20, 19, 20, 19]
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("limit", [0, -1, 100_001, True, None, 1.5, "2"])
def test_invalid_limits_rejected_before_any_native_probe(listing, limit):
    dlls, tree = listing
    before = len(dlls.opened), len(dlls.information_classes)
    assert code(lambda: tree.list_directory("资料/作品", max_entries=limit)) == "invalid_argument"
    assert code(lambda: dlls.native.list_directory(tree.handle, max_entries=limit)) == "invalid_argument"
    assert before == (len(dlls.opened), len(dlls.information_classes))
    assert not dlls.enumerations


@pytest.mark.parametrize("path", ["", "/资料", "资料/..", "资料//作品", "资料\\作品", "资料/x:ads", None])
def test_invalid_relative_paths_do_not_probe_or_create(listing, path):
    dlls, tree = listing
    before = len(dlls.opened)
    assert code(lambda: tree.list_directory(path)) == "forbidden_path"
    assert len(dlls.opened) == before and not dlls.enumerations


def test_only_listing_role_and_owned_live_handle_can_enumerate(listing):
    dlls, tree = listing
    assert code(lambda: dlls.native.list_directory(tree.handle)) == "forbidden_path"
    with dlls.native.open_relative(tree.handle, "资料", role="directory_listing") as handle:
        other = DirectoryDLLs().native
        assert code(lambda: other.list_directory(handle)) == "storage_unavailable"
    assert code(lambda: dlls.native.list_directory(handle)) == "storage_unavailable"
    assert not dlls.enumerations


def test_empty_directory_and_maximum_valid_limit(listing):
    dlls, tree = listing
    assert tree.list_directory("资料/作品", max_entries=100_000) == []
    assert [kind for _, kind, _ in dlls.enumerations] == [20]
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("split_pages", [False, True])
def test_entry_cap_fails_without_partial_success_or_opening_excess_entry(listing, split_pages):
    dlls, tree = listing
    a, b = add_entry(dlls, tree, "a"), add_entry(dlls, tree, "b")
    rows = [row_for("a", a), row_for("b", b)]
    dlls.pages = [page(row) for row in rows] if split_pages else [page(*rows)]
    start = len(dlls.opened)
    assert code(lambda: tree.list_directory("资料/作品", max_entries=1)) == "scan_limit"
    assert not any(request["name"] == "b" for request in dlls.opened[start:])
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("attributes,tag", [(0x400, 0xA000000C), (0x410, 0xA0000003), (0x400, 0)])
def test_reparse_entries_are_unsafe_and_never_opened(listing, attributes, tag):
    dlls, tree = listing
    dlls.pages = [page(record("链接", attributes=attributes, tag=tag, identity=bytes(16)))]
    before = len(dlls.opened)
    assert tree.list_directory("资料/作品") == [{"name": "链接", "kind": "unsafe"}]
    assert not any(request["name"] == "链接" for request in dlls.opened[before:])


@pytest.mark.parametrize("name", ["CON", "x:ads", "xx.", "xx ", "a/b", "a\\b", "a\x00b"])
def test_ambiguous_component_is_unsafe_without_relative_open(listing, name):
    dlls, tree = listing
    dlls.pages = [page(record(name))]
    start = len(dlls.opened)
    assert tree.list_directory("资料/作品") == [{"name": name, "kind": "unsafe"}]
    assert not any(request["name"] == name for request in dlls.opened[start:])


@pytest.mark.parametrize("change", [{"links": 2}, {"attributes": 0x480, "tag": 0xA000000C}, {"delete": 1}])
def test_entry_that_becomes_unsafe_is_classified_and_handle_closed(listing, change):
    dlls, tree = listing
    entry = add_entry(dlls, tree, "a")
    dlls.pages = [page(row_for("a", entry))]
    dlls.enumeration_hook = lambda *_: entry.update(change)
    assert tree.list_directory("资料/作品") == [{"name": "a", "kind": "unsafe"}]
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("tag", [1, 0xA000000C, 0xDEADBEEF, 0xFFFFFFFF])
def test_undefined_tag_on_ordinary_record_does_not_replace_metadata_check(listing, tag):
    dlls, tree = listing
    entry = add_entry(dlls, tree, "a")
    dlls.pages = [page(record("a", identity=entry["id"], tag=tag))]
    before = len(dlls.opened)
    assert tree.list_directory("资料/作品") == [{"name": "a", "kind": "file"}]
    assert any(request["name"] == "a" for request in dlls.opened[before:])
    assert_borrowed_root_only(dlls, tree)


def test_ordinary_record_with_undefined_tag_still_rejects_changed_reparse_metadata(listing):
    dlls, tree = listing
    entry = add_entry(dlls, tree, "a")
    dlls.pages = [page(record("a", identity=entry["id"], tag=0xFFFFFFFF))]
    dlls.enumeration_hook = lambda *_: entry.update(attributes=0x480, tag=0xA000000C)
    assert tree.list_directory("资料/作品") == [{"name": "a", "kind": "unsafe"}]
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("changes", [{"id": b"z" * 16}, {"directory": True, "attributes": 0x10}])
def test_metadata_identity_or_type_must_match_enumerated_record(listing, changes):
    dlls, tree = listing
    entry = add_entry(dlls, tree, "a")
    dlls.pages = [page(row_for("a", entry))]
    dlls.enumeration_hook = lambda *_: entry.update(changes)
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert_borrowed_root_only(dlls, tree)


def test_metadata_volume_must_match_parent_volume(listing):
    dlls, tree = listing
    entry = add_entry(dlls, tree, "a")

    def change_volume(value, kind, output):
        if kind == 18 and dlls.files[value] is entry:
            ctypes.cast(output, ctypes.POINTER(native.FileIdInfo)).contents.VolumeSerialNumber = 13

    dlls.metadata_hook = change_volume
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert_borrowed_root_only(dlls, tree)


def test_disappearing_entry_aborts_complete_result(listing):
    dlls, tree = listing
    add_entry(dlls, tree, "a")

    def remove_entry(*_):
        directory_id = int.from_bytes(directory_data(dlls, tree)["id"], "little")
        del dlls.names[(directory_id, "a")]

    dlls.enumeration_hook = remove_entry
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("at_end", [False, True])
def test_directory_metadata_is_checked_after_each_page_and_eof(listing, at_end):
    dlls, tree = listing
    add_entry(dlls, tree, "a")

    def mutate(value, *_):
        dlls.files[value].update(change_time=70)

    if at_end:
        dlls.end_hook = mutate
    else:
        dlls.enumeration_hook = mutate
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert_borrowed_root_only(dlls, tree)


def test_directory_namespace_replacement_is_rejected_after_enumeration(listing):
    dlls, tree = listing
    add_entry(dlls, tree, "a")

    def replace_leaf(value, _):
        original = dlls.files[value]
        for key, data in list(dlls.names.items()):
            if data is original:
                dlls.names[key] = dict(data, id=b"r" * 16)

    dlls.enumeration_hook = replace_leaf
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert_borrowed_root_only(dlls, tree)


def test_root_attachment_change_is_rejected_before_return(listing):
    dlls, tree = listing
    add_entry(dlls, tree, "a")

    def replace_root(*_):
        for key, data in list(dlls.names.items()):
            if key[0] is None:
                dlls.names[key] = dict(data, id=b"r" * 16)

    dlls.enumeration_hook = replace_root
    assert code(lambda: tree.list_directory("资料/作品")) == "storage_unavailable"
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize("name", ["a", ".", ".."])
def test_repeated_record_across_pages_never_loops_or_deduplicates_silently(listing, name):
    dlls, tree = listing
    data = add_entry(dlls, tree, "a")
    body = page(row_for(name, data))
    dlls.pages = [body, body]
    assert code(lambda: tree.list_directory("资料/作品")) == "version_changed"
    assert len(dlls.enumerations) == 2
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize(
    "offset,value",
    [(0, 1), (0, 88), (0, 65_528), (0, 0xFFFFFFF8), (60, 0), (60, 1), (60, 512), (60, 0xFFFFFFFE)],
)
def test_bad_record_offsets_and_lengths_fail_closed(listing, offset, value):
    dlls, tree = listing
    body = bytearray(page(record("aa")))
    struct.pack_into("<I", body, offset, value)
    dlls.pages = [bytes(body)]
    assert code(lambda: tree.list_directory("资料/作品")) == "storage_unavailable"
    assert_borrowed_root_only(dlls, tree)


def test_utf16_surrogate_corruption_is_not_replaced_or_opened(listing):
    dlls, tree = listing
    body = bytearray(page(record("a")))
    body[88:90] = b"\x00\xd8"
    dlls.pages = [bytes(body)]
    assert code(lambda: tree.list_directory("资料/作品")) == "storage_unavailable"
    assert_borrowed_root_only(dlls, tree)


def test_short_or_zeroed_success_buffer_is_not_an_empty_directory(listing):
    dlls, tree = listing
    dlls.pages = [b""]
    assert code(lambda: tree.list_directory("资料/作品")) == "storage_unavailable"
    assert_borrowed_root_only(dlls, tree)


def test_zero_128bit_identity_refuses_unsupported_filesystem(listing):
    dlls, tree = listing
    dlls.pages = [page(record("a", identity=bytes(16)))]
    start = len(dlls.opened)
    assert code(lambda: tree.list_directory("资料/作品")) == "unsupported_platform"
    assert not any(request["name"] == "a" for request in dlls.opened[start:])
    assert_borrowed_root_only(dlls, tree)


@pytest.mark.parametrize(
    "error,expected",
    [
        (1, "unsupported_platform"),
        (50, "unsupported_platform"),
        (87, "unsupported_platform"),
        (5, "storage_unavailable"),
        (38, "storage_unavailable"),
        (234, "storage_unavailable"),
    ],
)
@pytest.mark.parametrize("after_page", [False, True])
def test_only_no_more_files_is_completion_other_native_errors_never_fallback(
    listing, error, expected, after_page
):
    dlls, tree = listing
    if after_page:
        add_entry(dlls, tree, "a")
        dlls.end_error = error
    else:
        dlls.enumeration_error = error
    assert code(lambda: tree.list_directory("资料/作品")) == expected
    assert len(dlls.enumerations) == (2 if after_page else 1)
    assert_borrowed_root_only(dlls, tree)


def test_no_new_public_windows_backend_gate(monkeypatch):
    monkeypatch.setattr(
        platform_safety,
        "os",
        SimpleNamespace(
            name="nt", open=None, stat=None, mkdir=None, unlink=None, link=None, replace=lambda: None
        ),
    )
    report = platform_safety.detect_platform_safety()
    assert report.safe_files_backend is None and report.ownership_backend is None
    assert not report.runtime_supported


def test_unsafe_entries_still_count_toward_cap(listing):
    dlls, tree = listing
    dlls.pages = [page(record("a", attributes=0x400), record("b", attributes=0x400))]
    assert code(lambda: tree.list_directory("资料/作品", max_entries=1)) == "scan_limit"
    assert_borrowed_root_only(dlls, tree)


def test_non_directory_cannot_acquire_listing_handle(listing):
    dlls, tree = listing
    add_entry(dlls, tree, "a")
    assert code(lambda: tree.list_directory("资料/作品/a")) == "forbidden_path"
    assert not dlls.enumerations
    assert_borrowed_root_only(dlls, tree)


def test_failed_child_metadata_query_is_not_downgraded_to_unsafe(listing):
    dlls, tree = listing
    add_entry(dlls, tree, "a")
    dlls.enumeration_hook = lambda *_: setattr(dlls, "info_failure", 9)
    try:
        assert code(lambda: tree.list_directory("资料/作品")) == "storage_unavailable"
    finally:
        dlls.info_failure = None
    assert_borrowed_root_only(dlls, tree)


def test_native_listing_serializes_close_until_cursor_finishes(listing):
    dlls, tree = listing
    handle = dlls.native.open_relative(tree.handle, "资料", role="directory_listing")
    entered, release, close_started, close_done = (threading.Event() for _ in range(4))
    outcomes = []

    def pause(*_):
        entered.set()
        assert release.wait(2), "test must release the mock native enumeration"

    def enumerate_now():
        try:
            outcomes.append(dlls.native.list_directory(handle))
        except BaseException as error:
            outcomes.append(error)

    def close_now():
        close_started.set()
        handle.close()
        close_done.set()

    dlls.enumeration_hook = pause
    reader = threading.Thread(target=enumerate_now)
    closer = threading.Thread(target=close_now)
    reader.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert close_started.wait(2)
        assert not close_done.wait(0.05)
        assert handle._value not in dlls.closed
    finally:
        release.set()
        reader.join(2)
        if closer.ident is not None:
            closer.join(2)
        handle.close()
    assert not reader.is_alive() and not closer.is_alive()
    assert outcomes == [[{"name": "作品", "kind": "directory"}]]
    assert close_done.is_set()
    assert_borrowed_root_only(dlls, tree)
