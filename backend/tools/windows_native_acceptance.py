"""Developer-only real Windows x64 kernel acceptance, original fixtures only.

Requires an existing CPython 3.12+ x64 interpreter and this source checkout;
uses stdlib only, never installs dependencies or opens the public backend gate.
This is NOT the Python-free end-user distribution acceptance.

PowerShell, from backend (the final output directory must NOT exist):
  py -3.12 -I -B tools/windows_native_acceptance.py --output 'C:\\Users\\YOU\\Desktop\\cc-win-native-001'

An existing local parent directory is required. No UNC, mapped drive, reparse
ancestor, existing output, private library, credentials, network, or cleanup of
old runs. The exact newly created private tree is retained as evidence. Helpers
can only run this script's fixed actions, authenticated by a bounded stdin
run receipt and native directory identity. The parent can kill only its own
helper. Synchronous native calls in stage bootstrap are not IO deadlines.
The retained report.json is a pre-close checks snapshot; the final JSON printed
to the terminal is authoritative, including outer directory handle closure.

Host architecture: Microsoft IsWow64Process2 and GetCurrentProcess, loaded from
System32 only. https://learn.microsoft.com/en-us/windows/win32/api/wow64apiset/nf-wow64apiset-iswow64process2
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any

FILE_CHECKS = (
    "private_acl",
    "unicode_roundtrip",
    "bounded_directory",
    "stream_hash",
    "same_name_version",
    "reader_sharing_and_close",
    "hardlink_unsafe",
    "delete_owned_file",
    "closed_handle_rejected",
)
LEASE_CHECKS = ("cross_process_lock", "process_death_release")
HELPERS = ("files", "lease-hold", "lease-probe", "lease-recover")
MARKER = "acceptance-run.json"
SCRIPT = Path(__file__).resolve()
SOURCE = SCRIPT.parents[1] / "src"
MAX_MESSAGE = 32_768
MAX_STDERR = 8_192
CHILD_TIMEOUT = 45
CRASH_EXIT = 71


class AcceptanceError(RuntimeError):
    pass


def ensure(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def runtime_identity() -> dict[str, Any]:
    return {
        "os_name": os.name,
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pointer_bits": struct.calcsize("P") * 8,
    }


def require_windows_x64() -> None:
    identity = runtime_identity()
    ensure(
        identity["os_name"] == "nt"
        and sys.platform == "win32"
        and identity["system"] == "Windows"
        and identity["machine"].lower() in {"amd64", "x86_64"}
        and identity["pointer_bits"] == 64,
        "requires_real_windows_x64",
    )
    ensure(sys.version_info >= (3, 12), "requires_python_312_or_newer")
    try:
        kernel = getattr(ctypes, "WinDLL")("kernel32.dll", use_last_error=True, winmode=0x800)
        current = kernel.GetCurrentProcess
        current.restype, current.argtypes = ctypes.c_void_p, []
        query = kernel.IsWow64Process2
        query.restype = ctypes.c_int32
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint16)]
        process, host = ctypes.c_uint16(), ctypes.c_uint16()
        ensure(bool(query(current(), ctypes.byref(process), ctypes.byref(host))), "architecture_query_failed")
        ensure(host.value == 0x8664 and process.value == 0, "requires_native_amd64_not_emulation")
    except (AttributeError, OSError):
        raise AcceptanceError("architecture_query_unavailable") from None


def load_product():
    # -I ignores cwd/PYTHONPATH/user site. The only additional import root is
    # this checkout's fixed source sibling, not a CLI-supplied path.
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from collection_context.application.contracts import ContextError
    from collection_context.infrastructure.windows_files import WindowsFiles
    from collection_context.infrastructure.windows_native import WindowsNative
    from collection_context.infrastructure.windows_ownership import WindowsWriterLease

    return ContextError, WindowsFiles, WindowsNative, WindowsWriterLease


def checked_output(raw: str, *, repository: str, home: str) -> PureWindowsPath:
    ensure(type(raw) is str and 0 < len(raw) <= 900 and "\x00" not in raw, "invalid_output_path")
    path = PureWindowsPath(raw)
    ensure(
        path.is_absolute()
        and re.fullmatch(r"[A-Za-z]:", path.drive) is not None
        and len(path.parts) >= 3
        and ".." not in path.parts,
        "invalid_output_path",
    )
    ensure(
        all(
            part not in {".", "..", ""}
            and not part.endswith((".", " "))
            and not any(char in part for char in ':<>"|?*')
            for part in path.parts[1:]
        ),
        "invalid_output_path",
    )
    # Do not create evidence under this repository or use a broad home root.
    repo, user = PureWindowsPath(repository), PureWindowsPath(home)
    ensure(path != user and path != repo and repo not in path.parents, "output_must_be_library_external")
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        ensure(key not in result, "duplicate_protocol_key")
        result[key] = value
    return result


def decode_message(raw: bytes) -> dict[str, Any]:
    ensure(type(raw) is bytes and 0 < len(raw) <= MAX_MESSAGE, "invalid_protocol_size")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(AcceptanceError("invalid_number")),
        )
    except (ValueError, UnicodeError):
        raise AcceptanceError("invalid_protocol_json") from None
    ensure(isinstance(value, dict), "invalid_protocol_object")
    return value


def encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n").encode("ascii")


def file_identity(identity) -> dict[str, Any]:
    return {"volume": identity.volume_serial, "file_id": identity.file_id.hex()}


def marker_for(receipt: dict[str, Any]) -> dict[str, Any]:
    # The live parent's random nonce travels only through its helper pipes.
    # Persist a verifier, not a reusable helper capability for old output trees.
    validate_receipt(receipt)
    return {
        "schema": 1,
        "root_identity": receipt["root_identity"],
        "run_id_sha256": hashlib.sha256(receipt["run_id"].encode("ascii")).hexdigest(),
    }


def validate_receipt(value: dict[str, Any]) -> dict[str, Any]:
    ensure(
        set(value) == {"schema", "run_id", "root_identity"}
        and type(value["schema"]) is int
        and value["schema"] == 1,
        "invalid_run_receipt",
    )
    ensure(
        type(value["run_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) is not None,
        "invalid_run_receipt",
    )
    identity = value["root_identity"]
    ensure(isinstance(identity, dict) and set(identity) == {"volume", "file_id"}, "invalid_run_receipt")
    ensure(
        type(identity["volume"]) is int
        and 0 <= identity["volume"] < 2**64
        and type(identity["file_id"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", identity["file_id"]) is not None
        and identity["file_id"] != "0" * 32,
        "invalid_run_receipt",
    )
    return value


def safe_error(error: BaseException) -> str:
    if isinstance(error, AcceptanceError) and re.fullmatch(r"[a-z0-9_]{1,80}", str(error)):
        return str(error)
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code):
        return code
    if isinstance(error, subprocess.TimeoutExpired):
        return "owned_helper_timed_out"
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    return "native_check_failed"  # Never echo exception paths, environment or child output.


def base_report() -> dict[str, Any]:
    return {
        "schema": 1,
        "state": "not_run",
        "scope": "source_windows_native_adapter_only",
        "runtime": runtime_identity(),
        "native_windows_verified": False,
        "public_windows_backend_enabled": False,
        "python_free_distribution_verified": False,
        "output_created": False,
        "report_persisted": False,
        "report_file_scope": "pre_close_checks_snapshot; authoritative completion is final stdout JSON",
        "checks": [
            {"name": name, "state": "not_verified"}
            for name in ("stage_private_acl", *FILE_CHECKS, *LEASE_CHECKS)
        ],
        "network_requests": 0,
        "credentials_read": False,
        "limitations": [
            "No frozen installer or public facade acceptance",
            "No real platform or model request",
            "No power-loss, storage disconnect or full task recovery validation",
            "No symlink/junction creation or system privilege changes",
            "Native bootstrap IO has no deadline; directory observation is not an atomic snapshot",
            "Only this newly created synthetic tree is retained; no automatic recursive cleanup",
        ],
    }


def set_check(report, name, state, **details):
    ensure(name in {row["name"] for row in report["checks"]}, "unknown_check")
    ensure(state in {"passed", "failed", "not_verified"}, "invalid_check_state")
    row = next(row for row in report["checks"] if row["name"] == name)
    row.update(state=state, **details)


def pre_close_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        **report,
        "state": "failed" if report["state"] == "failed" else "checks_complete_pending_outer_close",
        "native_windows_verified": False,
        "final_completion_evidence": "the final JSON printed on stdout",
    }


def no_network(event: str, _arguments) -> None:
    if event.startswith("socket."):
        raise AcceptanceError("network_forbidden")


@contextmanager
def new_stage(output: PureWindowsPath, report: dict[str, Any]) -> Iterator[Any]:
    _, WindowsFiles, _, _ = load_product()
    with WindowsFiles(str(output.parent)) as parent:
        # FILE_CREATE is exclusive; even an existing empty directory is rejected.
        with parent.native.create_directory(parent.handle, output.name) as created:
            report["output_created"] = True
            parent.check_root()
            with WindowsFiles(str(output)) as stage:
                ensure(stage.identity == created.identity, "new_stage_identity_changed")
                stage.require_private_root()
                set_check(report, "stage_private_acl", "passed")
                yield stage
                parent.check_root()


@contextmanager
def owned_stage(output: str, receipt: dict[str, Any]) -> Iterator[Any]:
    _, WindowsFiles, _, _ = load_product()
    with WindowsFiles(output) as stage:
        stage.require_private_root()
        ensure(file_identity(stage.identity) == receipt["root_identity"], "helper_root_identity_changed")
        actual = decode_message(stage.read(MARKER, max_bytes=4096, private=True))
        ensure(actual == marker_for(receipt), "helper_not_current_owned_run")
        yield stage
        stage.check_root()


def expected_error(action: Callable[[], Any], allowed: set[str]) -> str:
    ContextError, _, _, _ = load_product()
    try:
        action()
    except ContextError as error:
        ensure(error.code in allowed, "unexpected_native_error")
        return error.code
    raise AcceptanceError("required_native_rejection_missing")


class FileLinkInformation(ctypes.Structure):
    # FileLinkInformation (11), NOT the Ex/BypassAccessCheck variants.
    # BOOLEAN + x64 padding has the same layout as the documented union.
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


def create_owned_hardlink(stage) -> None:
    """One fixed synthetic link, no path-based link or generic link capability.

    Microsoft documents no specific source access right for class 11; the
    destination directory's FILE_ADD_FILE permission is still kernel checked.
    Reuse the existing exclusive private file-creation role, not new rights.
    https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_link_information
    https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_file_information_class
    """
    from collection_context.infrastructure.windows_native import HANDLE, I32, U32, IoStatusBlock

    ensure(
        ctypes.sizeof(FileLinkInformation) == 24
        and FileLinkInformation.RootDirectory.offset == 8
        and FileLinkInformation.FileNameLength.offset == 16
        and FileLinkInformation.FileName.offset == 20,
        "hardlink_abi_unavailable",
    )
    native = stage.native
    function = native._libraries().ntdll.NtSetInformationFile
    function.restype = I32
    function.argtypes = [HANDLE, ctypes.POINTER(IoStatusBlock), HANDLE, U32, U32]
    stage.check_root()
    # Every child is FILE_CREATE (never open-or-create), and retained until the
    # one native submission and post-checks finish. No ancestor spelling is
    # involved in the link operation, even if an ancestor path is exchanged.
    with ExitStack() as owned:
        folder = owned.enter_context(native.create_directory(stage.handle, "硬链接 合成"))
        source_parent = owned.enter_context(native.create_directory(folder, "原件"))
        target_parent = owned.enter_context(native.create_directory(folder, "副本"))
        source = owned.enter_context(native.create_lease_file(source_parent, "原件.md"))
        parents = (stage.handle, folder, source_parent, target_parent)
        identities = tuple(native.information(parent).identity for parent in parents)
        for handle in (*parents, source):
            native.require_private_security(handle)
        ensure(native.information(source).size == 0, "hardlink_source_not_new_empty_file")
        name = "副本.md".encode("utf-16-le")
        request = ctypes.create_string_buffer(ctypes.sizeof(FileLinkInformation) + len(name))
        header = FileLinkInformation.from_buffer(request)
        header.ReplaceIfExists = 0
        header.RootDirectory = native._value(target_parent)
        header.FileNameLength = len(name)
        ctypes.memmove(ctypes.addressof(request) + FileLinkInformation.FileName.offset, name, len(name))
        io = IoStatusBlock()
        status = function(HANDLE(native._value(source)), ctypes.byref(io), request, len(request), 11)
        ensure(status == 0 and io.Result.Status == 0, "hardlink_submission_failed")
        # The linked source now intentionally has two names; normal information
        # rejects it. Check directory identities only, then close every owned
        # handle before the caller verifies both names are unreadable/unsafe.
        ensure(
            tuple(native.information(parent).identity for parent in parents) == identities,
            "hardlink_parent_identity_changed",
        )
        stage.check_root()


def check_files(stage) -> list[dict[str, Any]]:
    results = []

    def identity_at(relative):
        with stage._parent(relative) as (parent, name):
            with stage.native.open_relative(parent, name, role="metadata") as handle:
                stage.native.require_private_security(handle)
                return handle.identity

    def private_acl():
        stage.require_private_root()
        stage.mkdir("ACL 合成")
        stage.write("ACL 合成/合成.md", b"owned original")
        identity_at("ACL 合成/合成.md")

    def unicode_roundtrip():
        path, body = "中文 空格/子 目录/原创 😀.md", "原创测试，不读取用户资料。\n".encode()
        stage.write(path, body)
        ensure(stage.read(path, max_bytes=len(body), private=True) == body, "unicode_read_mismatch")
        ensure(stage.file_size(path) == len(body), "unicode_size_mismatch")

    def bounded_directory():
        stage.mkdir("枚举 合成/子 目录")
        stage.write("枚举 合成/中文 😀.md", b"original")
        ensure(
            stage.list_directory("枚举 合成", max_entries=2)
            == [{"name": "中文 😀.md", "kind": "file"}, {"name": "子 目录", "kind": "directory"}],
            "directory_classification_mismatch",
        )
        expected_error(lambda: stage.list_directory("枚举 合成", max_entries=1), {"scan_limit"})

    def stream_hash():
        block, tail = bytes(range(256)) * 256, b"original tail"
        chunks = (block, block, block, tail)
        size = sum(map(len, chunks))
        digest = hashlib.sha256(b"".join(chunks)).hexdigest()
        path = "流式 合成/分块.bin"
        stage.write_chunks(path, iter(chunks), expected_size=size, expected_sha256=digest)
        actual, count, total = hashlib.sha256(), 0, 0
        with stage.read_chunks(path, max_bytes=size, private=True) as source:
            for chunk in source:
                ensure(0 < len(chunk) <= 65_536, "unbounded_stream_chunk")
                actual.update(chunk)
                count, total = count + 1, total + len(chunk)
        ensure(count > 1 and total == size and actual.hexdigest() == digest, "stream_hash_mismatch")
        with stage._parent(path) as (parent, name):
            with stage.native.open_relative(parent, name, role="read_file") as handle:
                ensure(
                    stage.native.file_digest(handle, expected_size=size) == digest, "native_digest_mismatch"
                )

    def same_name_version():
        path = "版本 合成/同名.md"
        stage.write(path, b"first")
        old = identity_at(path)
        expected_error(lambda: stage.write(path, b"forbidden implicit replacement"), {"write_conflict"})
        ensure(stage.read(path, private=True) == b"first", "collision_changed_original")
        stage.write(path, b"second version", replace=True)
        ensure(
            old != identity_at(path) and stage.read(path, private=True) == b"second version",
            "replacement_version_not_observed",
        )

    def reader_sharing_and_close():
        path = "关闭 合成/持有.md"
        stage.write(path, b"held original")
        with stage._parent(path) as (parent, name):
            handle = stage.native.open_relative(parent, name, role="read_file")
            try:
                expected_error(
                    lambda: stage.write(path, b"blocked", replace=True),
                    {"storage_unavailable", "version_changed"},
                )
                ensure(
                    stage.native.read_file(handle, private=True) == b"held original",
                    "held_reader_original_changed",
                )
            finally:
                handle.close()
            ensure(handle.closed, "read_handle_not_closed")
        stage.write(path, b"after close", replace=True)
        ensure(stage.read(path, private=True) == b"after close", "closed_handle_still_blocks_replace")

    def hardlink_unsafe():
        create_owned_hardlink(stage)
        for directory, name in (("原件", "原件.md"), ("副本", "副本.md")):
            ensure(
                stage.list_directory(f"硬链接 合成/{directory}", max_entries=1)
                == [{"name": name, "kind": "unsafe"}],
                "hardlink_not_unsafe",
            )
            expected_error(lambda: stage.read(f"硬链接 合成/{directory}/{name}"), {"forbidden_path"})

    def delete_owned_file():
        stage.write("删除 合成/仅此文件.md", b"original disposable")
        stage.unlink("删除 合成/仅此文件.md")
        ensure(not stage.entry_exists("删除 合成/仅此文件.md"), "owned_delete_not_observed")

    def closed_handle_rejected():
        stage.mkdir("句柄 合成")
        handle = stage.native.open_relative(stage.handle, "句柄 合成", role="directory_listing")
        handle.close()
        handle.close()  # Idempotent wrapper close, not a second kernel close.
        expected_error(lambda: stage.native.list_directory(handle), {"storage_unavailable"})

    checks = (
        private_acl,
        unicode_roundtrip,
        bounded_directory,
        stream_hash,
        same_name_version,
        reader_sharing_and_close,
        hardlink_unsafe,
        delete_owned_file,
        closed_handle_rejected,
    )
    for name, action in zip(FILE_CHECKS, checks, strict=True):
        try:
            action()
            results.append({"name": name, "state": "passed"})
        except Exception as error:
            results.append({"name": name, "state": "failed", "error": safe_error(error)})
    return results


def lease_body(stage) -> bytes:
    with stage._parent("锁 合成/.context/写入所有权.lock") as (parent, name):
        with stage.native.open_relative(parent, name, role="lease_observer") as observer:
            return stage.native.read_file(observer, max_bytes=65_536, private=True)


def helper(action: str, output: str, receipt: dict[str, Any]) -> dict[str, Any]:
    ensure(action in HELPERS, "unknown_helper_action")
    validate_receipt(receipt)
    _, _, _, WindowsWriterLease = load_product()
    base = {"action": action, "run_id": receipt["run_id"], "native": True}
    with owned_stage(output, receipt) as stage:
        if action == "files":
            rows = check_files(stage)
            return {
                **base,
                "state": "passed" if all(row["state"] == "passed" for row in rows) else "failed",
                "checks": rows,
            }
        root = str(Path(stage.root) / "锁 合成")
        if action == "lease-probe":
            before = lease_body(stage)
            expected_error(lambda: WindowsWriterLease(root), {"writer_busy"})
            ensure(lease_body(stage) == before, "busy_contender_changed_metadata")
            return {**base, "state": "passed", "body_sha256": hashlib.sha256(before).hexdigest()}
        with WindowsWriterLease(root) as lease:
            lease.check()
            value = {
                **base,
                "state": "ready" if action == "lease-hold" else "passed",
                "body_sha256": hashlib.sha256(lease.body).hexdigest(),
                "lease_identity": file_identity(lease.identity),
            }
            if action == "lease-hold":
                sys.stdout.buffer.write(encoded(value))
                sys.stdout.buffer.flush()
                command = sys.stdin.buffer.readline(32)
                if command == b"CRASH\n":
                    os._exit(CRASH_EXIT)  # Deliberately skip close/finally in ONLY this owned helper.
                ensure(command == b"", "unexpected_helper_control")
                return {**base, "state": "cancelled"}
            return value


def child_command(action: str, output: str) -> list[str]:
    ensure(action in HELPERS, "unknown_helper_action")
    return [sys.executable, "-I", "-B", str(SCRIPT), "--helper", action, "--output", output]


def child_environment(output: str) -> dict[str, str]:
    return {
        "PATH": "",
        "TEMP": output,
        "TMP": output,
        **{key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ},
    }


def checked_reply(raw: bytes, action: str, receipt: dict[str, Any]) -> dict[str, Any]:
    ensure(action in HELPERS, "unknown_helper_action")
    value = decode_message(raw)
    ensure(
        value.get("action") == action
        and value.get("run_id") == receipt["run_id"]
        and value.get("native") is True,
        "helper_reply_identity_mismatch",
    )
    if action == "files":
        rows = value.get("checks")
        if not isinstance(rows, list) or len(rows) != len(FILE_CHECKS):
            raise AcceptanceError("incomplete_file_check_report")
        ensure(set(value) == {"action", "run_id", "native", "state", "checks"}, "invalid_reply_fields")
        ensure(
            [row.get("name") for row in rows if isinstance(row, dict)] == list(FILE_CHECKS)
            and all(row.get("state") in {"passed", "failed"} for row in rows),
            "invalid_file_check_report",
        )
        expected = "passed" if all(row["state"] == "passed" for row in rows) else "failed"
        for row in rows:
            fields = {"name", "state"} if row["state"] == "passed" else {"name", "state", "error"}
            ensure(set(row) == fields, "invalid_check_fields")
            if row["state"] == "failed":
                ensure(
                    type(row["error"]) is str and re.fullmatch(r"[a-z0-9_]{1,80}", row["error"]) is not None,
                    "invalid_check_error",
                )
        ensure(value.get("state") == expected, "false_native_success")
    else:
        fields = {"action", "run_id", "native", "state", "body_sha256"}
        if action != "lease-probe":
            fields.add("lease_identity")
            validate_receipt(
                {"schema": 1, "run_id": receipt["run_id"], "root_identity": value.get("lease_identity")}
            )
        ensure(set(value) == fields, "invalid_reply_fields")
        ensure(
            value.get("state") == ("ready" if action == "lease-hold" else "passed"), "helper_not_successful"
        )
        ensure(
            type(value.get("body_sha256")) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["body_sha256"]) is not None,
            "invalid_lease_proof",
        )
    return value


def run_child(action: str, output: str, receipt: dict[str, Any]) -> dict[str, Any]:
    ensure(action != "lease-hold", "holder_requires_bounded_protocol")
    validate_receipt(receipt)
    child = subprocess.Popen(
        child_command(action, output),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=output,
        env=child_environment(output),
    )
    # Bounded storage from the first read, not communicate()/capture_output
    # followed by a length check after unbounded output has accumulated.
    captured = (bytearray(), bytearray())
    wake = threading.Event()
    failures: queue.Queue[str] = queue.Queue(maxsize=3)
    readers: list[threading.Thread] = []

    def collect(pipe, data: bytearray, limit: int, label: str) -> None:
        try:
            while True:
                chunk = pipe.read(min(4096, limit - len(data) + 1))
                if not chunk:
                    break
                if len(data) + len(chunk) > limit:
                    failures.put_nowait(f"owned_helper_{label}_limit")
                    break
                data.extend(chunk)
        except Exception:
            failures.put_nowait("owned_helper_pipe_failed")
        finally:
            wake.set()

    try:
        ensure(
            child.stdin is not None and child.stdout is not None and child.stderr is not None,
            "missing_helper_pipes",
        )
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        for pipe, data, limit, label in (
            (child.stdout, captured[0], MAX_MESSAGE, "stdout"),
            (child.stderr, captured[1], MAX_STDERR, "stderr"),
        ):
            thread = threading.Thread(target=collect, args=(pipe, data, limit, label), daemon=True)
            thread.start()
            readers.append(thread)
        # The validated fixed receipt is < 512 bytes, below Windows' pipe
        # capacity; the reader may not accept arbitrary/unbounded stdin.
        body = encoded(receipt)
        ensure(len(body) < 512, "invalid_run_receipt")
        ensure(child.stdin.write(body) == len(body), "owned_helper_stdin_failed")
        child.stdin.close()
        deadline = time.monotonic() + CHILD_TIMEOUT
        while True:
            if not failures.empty():
                raise AcceptanceError(failures.get_nowait())
            if child.poll() is not None and not any(thread.is_alive() for thread in readers):
                break
            ensure(time.monotonic() < deadline, "owned_helper_timed_out")
            wake.wait(timeout=min(0.05, max(0, deadline - time.monotonic())))
            wake.clear()
        if not failures.empty():
            raise AcceptanceError(failures.get_nowait())
        ensure(child.returncode == 0, "owned_helper_failed")
        return checked_reply(bytes(captured[0]), action, receipt)
    finally:
        with ExitStack() as pipes:
            for closing_pipe in (child.stdin, child.stdout, child.stderr):
                if closing_pipe is not None:
                    pipes.callback(closing_pipe.close)
            try:
                if child.poll() is None:
                    child.kill()  # Only this directly owned Popen, never a PID/group search.
                    child.wait(timeout=10)
            finally:
                for thread in readers:
                    thread.join(timeout=2)
            # Helpers never spawn descendants. A leaked pipe is a failure, not
            # success. Unbuffered pipes avoid buffered close locks; ExitStack
            # attempts every pipe close even when one close fails.
            ensure(not any(thread.is_alive() for thread in readers), "owned_helper_pipe_not_closed")


def run_lock_checks(output: str, receipt: dict[str, Any], report) -> None:
    child = subprocess.Popen(
        child_command("lease-hold", output),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=output,
        env=child_environment(output),
    )
    if child.stdin is None or child.stdout is None:
        child.kill()
        child.wait(timeout=10)
        raise AcceptanceError("missing_helper_pipes")
    writer, output_pipe = child.stdin, child.stdout
    replies: queue.Queue[bytes] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: replies.put(output_pipe.readline(MAX_MESSAGE + 1)), daemon=True)
    try:
        writer.write(encoded(receipt))
        writer.flush()
        reader.start()
        try:
            ready = checked_reply(replies.get(timeout=15), "lease-hold", receipt)
        except queue.Empty:
            raise AcceptanceError("holder_readiness_timeout") from None
        probe = run_child("lease-probe", output, receipt)
        ensure(probe["body_sha256"] == ready["body_sha256"], "contender_changed_holder_metadata")
        set_check(report, "cross_process_lock", "passed")
        writer.write(b"CRASH\n")
        writer.flush()
        ensure(child.wait(timeout=15) == CRASH_EXIT, "owned_process_death_not_observed")
        recovered = run_child("lease-recover", output, receipt)
        ensure(
            recovered["lease_identity"] == ready["lease_identity"]
            and recovered["body_sha256"] != ready["body_sha256"],
            "kernel_lease_recovery_unproven",
        )
        set_check(report, "process_death_release", "passed")
    finally:
        if child.poll() is None:
            child.kill()  # Only the Popen child created directly above.
            child.wait(timeout=10)
        if reader.ident is not None:
            reader.join(timeout=2)
        writer.close()
        output_pipe.close()


def exercise(output: str) -> dict[str, Any]:
    report = base_report()
    try:
        require_windows_x64()  # Always before path creation or product imports.
        path = checked_output(output, repository=str(SCRIPT.parents[2]), home=str(Path.home()))
        report["state"] = "running"
        with new_stage(path, report) as stage:
            try:
                receipt = {
                    "schema": 1,
                    "run_id": uuid.uuid4().hex,
                    "root_identity": file_identity(stage.identity),
                }
                stage.write(MARKER, encoded(marker_for(receipt)))
                stage.mkdir("锁 合成")
                reply = run_child("files", str(path), receipt)
                for row in reply["checks"]:
                    set_check(
                        report,
                        row["name"],
                        row["state"],
                        **({"error": row["error"]} if "error" in row else {}),
                    )
                try:
                    run_lock_checks(str(path), receipt, report)
                except Exception as error:
                    pending = next(
                        row["name"]
                        for row in report["checks"]
                        if row["name"] in LEASE_CHECKS and row["state"] != "passed"
                    )
                    set_check(report, pending, "failed", error=safe_error(error))
                report["native_windows_verified"] = all(row["state"] == "passed" for row in report["checks"])
                report["state"] = "passed" if report["native_windows_verified"] else "failed"
            except BaseException as error:
                report.update(state="failed", error=safe_error(error), native_windows_verified=False)
            finally:
                # No ordinary-path fallback if controlled report publication fails.
                report["report_persisted"] = True
                try:
                    stage.write("report.json", encoded(pre_close_snapshot(report)))
                except BaseException as error:
                    report.update(
                        state="failed",
                        error=safe_error(error),
                        report_persisted=False,
                        native_windows_verified=False,
                    )
    except BaseException as error:
        report.update(
            state="failed" if report["output_created"] else "refused",
            error=safe_error(error),
            native_windows_verified=False,
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, help="Explicit absolute NEW local directory; parent must exist"
    )
    parser.add_argument("--helper", choices=HELPERS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.addaudithook(no_network)
    if args.helper:
        try:
            require_windows_x64()
            receipt = validate_receipt(decode_message(sys.stdin.buffer.readline(MAX_MESSAGE + 1)))
            output = checked_output(args.output, repository=str(SCRIPT.parents[2]), home=str(Path.home()))
            report = helper(args.helper, str(output), receipt)
        except BaseException as error:
            report = {"state": "refused", "native": False, "error": safe_error(error)}
            sys.stdout.buffer.write(encoded(report))
            return 2
        sys.stdout.buffer.write(encoded(report))
        return 0
    report = exercise(args.output)
    sys.stdout.buffer.write(encoded(report))
    return 0 if report["state"] == "passed" else 2 if report["state"] == "refused" else 1


if __name__ == "__main__":
    raise SystemExit(main())
