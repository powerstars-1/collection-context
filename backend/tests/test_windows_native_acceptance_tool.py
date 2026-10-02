"""Protocol/refusal checks only: NO fake result is Windows native acceptance."""

from __future__ import annotations

import copy
import ctypes
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/windows_native_acceptance.py"


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("windows_native_acceptance_protocol_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def receipt():
    return {"schema": 1, "run_id": "a" * 32, "root_identity": {"volume": 123, "file_id": "b" * 32}}


def file_reply(tool, receipt):
    return {
        "action": "files",
        "run_id": receipt["run_id"],
        "native": True,
        "state": "passed",
        "checks": [{"name": name, "state": "passed"} for name in tool.FILE_CHECKS],
    }


@pytest.mark.skipif(os.name == "nt", reason="Real non-Windows refusal, not a simulated Windows result")
@pytest.mark.parametrize("helper", [None, "files", "lease-hold", "lease-probe", "lease-recover"])
def test_real_nonwindows_process_cannot_make_output_or_claim_native_pass(tmp_path, helper):
    output = tmp_path / "must-not-exist"
    command = [sys.executable, "-I", "-B", str(SCRIPT), "--output", str(output)]
    if helper:
        command += ["--helper", helper]
    child = subprocess.run(command, input=b"{}\n", capture_output=True, timeout=10, check=False)
    assert child.returncode == 2 and not output.exists()
    report = json.loads(child.stdout)
    assert report["state"] == "refused" and report["error"] == "requires_real_windows_x64"
    if helper:
        assert report["native"] is False
    else:
        assert report["native_windows_verified"] is False
        assert report["output_created"] is False and report["report_persisted"] is False
        assert all(row["state"] == "not_verified" for row in report["checks"])
    assert child.stderr == b""


def test_platform_refusal_precedes_product_imports_and_stage_setup(tool, monkeypatch):
    calls = []

    def refuse():
        raise tool.AcceptanceError("requires_real_windows_x64")

    monkeypatch.setattr(tool, "require_windows_x64", refuse)
    monkeypatch.setattr(tool, "load_product", lambda: calls.append("product"))
    monkeypatch.setattr(tool, "new_stage", lambda *_: calls.append("stage"))
    report = tool.exercise(r"C:\Users\Original\test-run")
    assert report["state"] == "refused" and report["native_windows_verified"] is False
    assert not calls


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        "C:relative",
        "C:\\",
        r"C:\root-leaf",
        r"\\server\share\new",
        r"\\?\C:\Users\Original\new",
        r"C:\Users\..\new",
        r"C:\Users\Original\x:ads",
        "C:\\Users\\Original\\trailing. ",
        "C:\\Users\\Original\\bad\x00name",
        r"C:\repo",
        r"c:\REPO\nested\new",
        r"C:\Users\Original",
    ],
)
def test_output_spelling_rejects_broad_or_alias_paths_without_native_probe(tool, path):
    with pytest.raises(tool.AcceptanceError):
        tool.checked_output(path, repository=r"C:\repo", home=r"C:\Users\Original")


def test_new_output_path_is_only_validated_not_created(tool):
    result = tool.checked_output(r"D:\原创 测试\new-001", repository=r"C:\repo", home=r"C:\Users\Original")
    assert str(result) == r"D:\原创 测试\new-001"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 32769,
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b"{}\n{}",
    ],
)
def test_protocol_rejects_overlong_duplicate_and_nonfinite_payloads(tool, raw):
    with pytest.raises(tool.AcceptanceError):
        tool.decode_message(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(schema=True),
        lambda v: v.update(schema=2),
        lambda v: v.update(extra="unexpected"),
        lambda v: v.update(run_id="not-a-run"),
        lambda v: v.update(root_identity=None),
        lambda v: v["root_identity"].update(volume=True),
        lambda v: v["root_identity"].update(volume=-1),
        lambda v: v["root_identity"].update(volume=2**64),
        lambda v: v["root_identity"].update(file_id="0" * 32),
        lambda v: v["root_identity"].update(file_id="Z" * 32),
    ],
)
def test_receipt_binds_schema_run_and_nonzero_native_identity(tool, receipt, mutation):
    mutation(receipt)
    with pytest.raises(tool.AcceptanceError):
        tool.validate_receipt(receipt)


def test_canonical_receipt_roundtrip_and_another_run_is_not_accepted(tool, receipt):
    assert tool.validate_receipt(tool.decode_message(tool.encoded(receipt))) == receipt
    reply = file_reply(tool, receipt)
    reply["run_id"] = "c" * 32
    with pytest.raises(tool.AcceptanceError, match="identity_mismatch"):
        tool.checked_reply(tool.encoded(reply), "files", receipt)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(native=False),
        lambda v: v.update(action="lease-probe"),
        lambda v: v.update(extra=True),
        lambda v: v["checks"].pop(),
        lambda v: v["checks"].reverse(),
        lambda v: v["checks"].append(v["checks"][0]),
        lambda v: v["checks"][0].update(state="not_verified"),
        lambda v: v["checks"][0].update(state="failed", error="test_failure"),
        lambda v: v["checks"][0].update(extra="internal text"),
    ],
)
def test_missing_partial_wrong_or_false_success_file_results_are_rejected(tool, receipt, mutation):
    reply = file_reply(tool, receipt)
    mutation(reply)
    with pytest.raises(tool.AcceptanceError):
        tool.checked_reply(tool.encoded(reply), "files", receipt)


def test_well_formed_synthetic_protocol_payload_does_not_promote_native_report(tool, receipt):
    reply = file_reply(tool, receipt)
    assert tool.checked_reply(tool.encoded(reply), "files", receipt) == reply
    report = tool.base_report()
    assert not report["native_windows_verified"] and report["state"] == "not_run"
    reply["checks"][2].update(state="failed", error="scan_limit")
    reply["state"] = "failed"
    assert tool.checked_reply(tool.encoded(reply), "files", receipt)["state"] == "failed"


@pytest.mark.parametrize("error", ["secret /Users/person/token", "SECRET\nTEXT", "A" * 90, 7])
def test_helper_errors_cannot_smuggle_arbitrary_output(tool, receipt, error):
    reply = file_reply(tool, receipt)
    reply["state"] = "failed"
    reply["checks"][0].update(state="failed", error=error)
    with pytest.raises(tool.AcceptanceError):
        tool.checked_reply(tool.encoded(reply), "files", receipt)


@pytest.mark.parametrize("action", ["lease-hold", "lease-probe", "lease-recover"])
def test_lease_reply_proof_has_exact_fields_and_fresh_identity(tool, receipt, action):
    value = {
        "action": action,
        "run_id": receipt["run_id"],
        "native": True,
        "state": "ready" if action == "lease-hold" else "passed",
        "body_sha256": "d" * 64,
    }
    if action != "lease-probe":
        value["lease_identity"] = copy.deepcopy(receipt["root_identity"])
    assert tool.checked_reply(tool.encoded(value), action, receipt) == value
    value["body_sha256"] = "incomplete"
    with pytest.raises(tool.AcceptanceError):
        tool.checked_reply(tool.encoded(value), action, receipt)


@pytest.mark.parametrize("action", ["shell", "powershell", "delete", "install", "files --other", ""])
def test_helper_command_whitelist_cannot_launch_other_commands(tool, action):
    with pytest.raises(tool.AcceptanceError, match="unknown_helper"):
        tool.child_command(action, r"C:\Users\Original\new")


def test_fixed_child_command_and_minimal_environment_do_not_carry_credentials(tool, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "do-not-copy")
    monkeypatch.setenv("PYTHONPATH", "do-not-import")
    monkeypatch.setenv("HTTPS_PROXY", "do-not-proxy")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    output = r"C:\Users\Original\new"
    command = tool.child_command("files", output)
    assert command == [sys.executable, "-I", "-B", str(SCRIPT), "--helper", "files", "--output", output]
    environment = tool.child_environment(output)
    assert set(environment) <= {"PATH", "TEMP", "TMP", "SystemRoot", "WINDIR"}
    assert environment["PATH"] == "" and environment["TEMP"] == environment["TMP"] == output
    assert "do-not" not in json.dumps(environment)


def test_noninteractive_helper_does_not_echo_failure(tool, receipt, monkeypatch, tmp_path):
    children = original_children(tool, monkeypatch, "import sys; sys.stdout.write('PRIVATE'); sys.exit(1)")
    with pytest.raises(tool.AcceptanceError, match="^owned_helper_failed$"):
        tool.run_child("files", str(tmp_path), receipt)
    assert tool.CHILD_TIMEOUT == 45
    assert_children_closed(children)


@pytest.mark.parametrize("event", ["socket.connect", "socket.bind", "socket.getaddrinfo", "socket.__new__"])
def test_network_is_denied_without_any_connection(tool, event):
    with pytest.raises(tool.AcceptanceError, match="network_forbidden"):
        tool.no_network(event, ("not-a-real-endpoint",))


def test_redacted_error_codes_only_and_no_native_gate_override_in_cli(tool):
    assert tool.safe_error(RuntimeError("SECRET user data")) == "native_check_failed"
    assert tool.safe_error(tool.AcceptanceError("valid_code")) == "valid_code"
    assert tool.safe_error(tool.AcceptanceError("SECRET unsafe words")) == "native_check_failed"
    report = tool.base_report()
    assert report["python_free_distribution_verified"] is False
    assert report["public_windows_backend_enabled"] is False
    child = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "--help"], capture_output=True, timeout=10, check=False
    )
    assert child.returncode == 0
    help_text = child.stdout.decode()
    assert "--force" not in help_text and "--fake" not in help_text and "--helper" not in help_text
    assert "CPython 3.12" in help_text and "NOT the Python-free" in help_text


@pytest.mark.parametrize("state", ["passed", "failed", "running"])
def test_persisted_snapshot_never_claims_final_close_or_native_success(tool, state):
    report = tool.base_report()
    report.update(state=state, native_windows_verified=state == "passed")
    snapshot = tool.pre_close_snapshot(report)
    assert snapshot["native_windows_verified"] is False
    assert snapshot["state"] == ("failed" if state == "failed" else "checks_complete_pending_outer_close")
    assert report["state"] == state


def test_existing_output_creation_collision_has_no_fallback_or_body_read(tool, monkeypatch):
    calls = []

    class Collision(Exception):
        code = "write_conflict"

    class MetadataOnlyParent:
        handle = object()

        def __init__(self, path):
            calls.append(("parent_metadata_only", path))
            self.native = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append(("parent_closed",))

        def create_directory(self, handle, name):
            assert handle is self.handle
            calls.append(("exclusive_create", name))
            raise Collision()

    monkeypatch.setattr(tool, "load_product", lambda: (None, MetadataOnlyParent, None, None))
    report = tool.base_report()
    path = tool.checked_output(
        r"C:\Users\Original\existing", repository=r"C:\repo", home=r"C:\Users\Original"
    )
    with pytest.raises(Collision), tool.new_stage(path, report):
        pytest.fail("An existing output must never yield a usable stage")
    assert report["output_created"] is False
    assert calls == [
        ("parent_metadata_only", r"C:\Users\Original"),
        ("exclusive_create", "existing"),
        ("parent_closed",),
    ]


def test_invalid_receipt_is_rejected_before_helper_product_or_stage_access(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(tool, "load_product", lambda: calls.append("product"))
    with pytest.raises(tool.AcceptanceError):
        tool.helper("files", r"C:\Users\Original\existing", {})
    assert not calls


def test_stage_setup_failure_cannot_return_native_success(tool, monkeypatch):
    # Deliberately raises before any test-body execution; this tests report
    # rejection bookkeeping, not an injected Windows adapter result.
    monkeypatch.setattr(tool, "require_windows_x64", lambda: None)
    monkeypatch.setattr(tool, "checked_output", lambda *_args, **_kwargs: "unused")

    @contextmanager
    def unavailable(_path, report):
        report["output_created"] = True
        raise tool.AcceptanceError("synthetic_stage_failure")
        yield  # pragma: no cover

    monkeypatch.setattr(tool, "new_stage", unavailable)
    report = tool.exercise("unused")
    assert report["state"] == "failed" and report["native_windows_verified"] is False
    assert all(row["state"] == "not_verified" for row in report["checks"])


def test_retained_marker_does_not_expose_reusable_helper_capability(tool, receipt):
    marker = tool.marker_for(receipt)
    assert set(marker) == {"schema", "root_identity", "run_id_sha256"}
    assert receipt["run_id"].encode() not in tool.encoded(marker)
    assert marker["root_identity"] == receipt["root_identity"]
    other = {**receipt, "run_id": "e" * 32}
    assert tool.marker_for(other) != marker


def original_children(tool, monkeypatch, source):
    """Real pipes/processes running only original protocol snippets, not Windows."""
    original = subprocess.Popen
    children = []

    def start(command, **kwargs):
        assert command == [sys.executable, "-I", "-B", "-c", source]
        assert kwargs["bufsize"] == 0 and "shell" not in kwargs
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        child = original(command, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(tool, "child_command", lambda *_: [sys.executable, "-I", "-B", "-c", source])
    monkeypatch.setattr(tool.subprocess, "Popen", start)
    return children


def assert_children_closed(children):
    assert len(children) == 1
    for child in children:
        assert child.poll() is not None
        assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


@pytest.mark.parametrize("stream,limit", [(1, 32768), (2, 8192)])
def test_real_pipe_overflow_is_bounded_and_owned_child_is_terminated(
    tool, receipt, monkeypatch, tmp_path, stream, limit
):
    # Endless original output would not fit memory with capture_output.
    source = f"import os; os.read(0,512)\nwhile True: os.write({stream},b'x'*4096)"
    children = original_children(tool, monkeypatch, source)
    assert (tool.MAX_MESSAGE if stream == 1 else tool.MAX_STDERR) == limit
    label = "stdout" if stream == 1 else "stderr"
    with pytest.raises(tool.AcceptanceError, match=f"^owned_helper_{label}_limit$"):
        tool.run_child("files", str(tmp_path), receipt)
    assert_children_closed(children)


def test_real_helper_timeout_only_ends_its_own_child(tool, receipt, monkeypatch, tmp_path):
    children = original_children(tool, monkeypatch, "import time; time.sleep(30)")
    monkeypatch.setattr(tool, "CHILD_TIMEOUT", 0.2)
    with pytest.raises(tool.AcceptanceError, match="^owned_helper_timed_out$"):
        tool.run_child("files", str(tmp_path), receipt)
    assert_children_closed(children)


def test_real_pipes_accept_exact_caps_and_valid_bounded_reply(tool, receipt, monkeypatch, tmp_path):
    reply = file_reply(tool, receipt)
    body = tool.encoded(reply)
    padded = body + b" " * (tool.MAX_MESSAGE - len(body))
    source = f"import os; os.read(0,512); os.write(1,{padded!r}); os.write(2,b'e'*8192)"
    children = original_children(tool, monkeypatch, source)
    assert tool.run_child("files", str(tmp_path), receipt) == reply
    assert_children_closed(children)
    assert tool.base_report()["native_windows_verified"] is False


@pytest.mark.parametrize("stream,limit", [("stdout", 32768), ("stderr", 8192)])
def test_reader_never_consumes_more_than_cap_plus_one_byte(tool, receipt, monkeypatch, stream, limit):
    class BoundedProbe(io.BytesIO):
        def __init__(self):
            super().__init__()
            self.read_count, self.requests = 0, []

        def read(self, size=-1):
            self.requests.append(size)
            assert 1 <= size <= 4096
            self.read_count += size
            return b"x" * size

    probe = BoundedProbe()
    child = SimpleNamespace(
        stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=0, poll=lambda: 0
    )
    setattr(child, stream, probe)
    monkeypatch.setattr(tool.subprocess, "Popen", lambda *_args, **_kwargs: child)
    with pytest.raises(tool.AcceptanceError, match=f"^owned_helper_{stream}_limit$"):
        tool.run_child("files", "unused", receipt)
    assert probe.read_count == limit + 1 and probe.requests[-1] == 1
    assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


def test_pipe_close_failure_does_not_return_valid_reply_and_other_pipes_close(tool, receipt, monkeypatch):
    class FailingClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("not included in reports")

    child = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=FailingClose(tool.encoded(file_reply(tool, receipt))),
        stderr=io.BytesIO(),
        returncode=0,
        poll=lambda: 0,
    )
    monkeypatch.setattr(tool.subprocess, "Popen", lambda *_args, **_kwargs: child)
    with pytest.raises(OSError):
        tool.run_child("files", "unused", receipt)
    assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


def mock_windows_query(tool, monkeypatch, *, process=0, host=0x8664, result=1, machine="AMD64"):
    from test_context_windows_native import FakeFunction

    calls = []

    def query(handle, process_out, host_out):
        calls.append("query")
        assert handle == 123
        ctypes.cast(process_out, ctypes.POINTER(ctypes.c_uint16)).contents.value = process
        ctypes.cast(host_out, ctypes.POINTER(ctypes.c_uint16)).contents.value = host
        return result

    kernel = SimpleNamespace(GetCurrentProcess=FakeFunction(lambda: 123), IsWow64Process2=FakeFunction(query))

    def load(name, **kwargs):
        calls.append("load")
        assert name == "kernel32.dll" and kwargs == {"use_last_error": True, "winmode": 0x800}
        return kernel

    monkeypatch.setattr(tool, "sys", SimpleNamespace(platform="win32", version_info=(3, 12)))
    monkeypatch.setattr(
        tool,
        "runtime_identity",
        lambda: {"os_name": "nt", "system": "Windows", "machine": machine, "pointer_bits": 64},
    )
    monkeypatch.setattr(tool.ctypes, "WinDLL", load, raising=False)
    return kernel, calls


@pytest.mark.parametrize(
    "process,host,machine,error,queried",
    [
        (0, 0xAA64, "ARM64", "requires_real_windows_x64", False),
        (0, 0xAA64, "AMD64", "requires_native_amd64_not_emulation", True),
        (0x8664, 0xAA64, "AMD64", "requires_native_amd64_not_emulation", True),
        (0x14C, 0x8664, "AMD64", "requires_native_amd64_not_emulation", True),
        (0, 0, "AMD64", "requires_native_amd64_not_emulation", True),
    ],
)
def test_architecture_native_arm64_or_emulation_refused(
    tool, monkeypatch, process, host, machine, error, queried
):
    _, calls = mock_windows_query(tool, monkeypatch, process=process, host=host, machine=machine)
    with pytest.raises(tool.AcceptanceError, match=f"^{error}$"):
        tool.require_windows_x64()
    assert ("query" in calls) == queried


@pytest.mark.parametrize("failure", ["bool_false", "api_missing", "dll_unavailable", "api_raises"])
def test_native_architecture_query_failure_is_never_assumed_x64(tool, monkeypatch, failure):
    from test_context_windows_native import FakeFunction

    kernel, _ = mock_windows_query(tool, monkeypatch, result=0 if failure == "bool_false" else 1)

    def unavailable(*_args, **_kwargs):
        raise OSError("native query unavailable")

    if failure == "api_missing":
        del kernel.IsWow64Process2
    elif failure == "dll_unavailable":
        monkeypatch.setattr(tool.ctypes, "WinDLL", unavailable)
    elif failure == "api_raises":
        kernel.IsWow64Process2 = FakeFunction(unavailable)
    code = "architecture_query_failed" if failure == "bool_false" else "architecture_query_unavailable"
    with pytest.raises(tool.AcceptanceError, match=f"^{code}$"):
        tool.require_windows_x64()


def test_architecture_query_signature_dispatch_only_not_kernel_evidence(tool, monkeypatch):
    kernel, calls = mock_windows_query(tool, monkeypatch)
    tool.require_windows_x64()
    assert calls == ["load", "query"]
    assert kernel.GetCurrentProcess.argtypes == [] and kernel.GetCurrentProcess.restype is ctypes.c_void_p
    assert kernel.IsWow64Process2.restype is ctypes.c_int32
    assert kernel.IsWow64Process2.argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint16),
    ]
    assert tool.base_report()["native_windows_verified"] is False


@pytest.mark.parametrize("phase", ["outer_close", "report_write"])
def test_failure_after_all_check_rows_cannot_leave_final_native_success(tool, monkeypatch, phase):
    monkeypatch.setattr(tool, "require_windows_x64", lambda: None)
    monkeypatch.setattr(tool, "checked_output", lambda *_args, **_kwargs: "unused")
    saved = []

    class Stage:
        identity = SimpleNamespace(volume_serial=123, file_id=b"b" * 16)

        def write(self, name, body):
            if name == "report.json":
                saved.append(json.loads(body))
                if phase == "report_write":
                    raise tool.AcceptanceError("synthetic_write_close_failure")

        def mkdir(self, name):
            assert name == "锁 合成"

    @contextmanager
    def stage_context(_path, report):
        report["output_created"] = True
        tool.set_check(report, "stage_private_acl", "passed")
        yield Stage()
        if phase == "outer_close":
            raise tool.AcceptanceError("synthetic_outer_close_failure")

    def lock_results(_path, _receipt, report):
        for name in tool.LEASE_CHECKS:
            tool.set_check(report, name, "passed")

    monkeypatch.setattr(tool, "new_stage", stage_context)
    monkeypatch.setattr(tool, "run_child", lambda _action, _path, receipt: file_reply(tool, receipt))
    monkeypatch.setattr(tool, "run_lock_checks", lock_results)
    report = tool.exercise("unused")
    assert report["state"] == "failed" and report["native_windows_verified"] is False
    assert report["report_persisted"] is (phase == "outer_close")
    assert len(saved) == 1 and saved[0]["native_windows_verified"] is False
    assert saved[0]["state"] == "checks_complete_pending_outer_close"


@pytest.fixture
def link_kernel():
    from test_context_windows_directory import DirectoryDLLs
    from test_context_windows_native import FakeFunction

    from collection_context.infrastructure import windows_native as native
    from collection_context.infrastructure.windows_files import WindowsFiles

    class LinkDLLs(DirectoryDLLs):
        def __init__(self):
            super().__init__()
            self.link_calls, self.link_status, self.link_io_status = [], 0, 0
            self.link_hook = None
            self.ntdll.NtSetInformationFile = FakeFunction(self.link)

        def link(self, source, io_status, buffer, length, kind):
            assert kind == 11
            raw = ctypes.string_at(buffer, length)
            # Independently decode ABI, not the implementation's ctypes class.
            replace, target, size = struct.unpack_from("<B7xQI", raw)
            name = raw[20 : 20 + size].decode("utf-16-le")
            assert replace == 0 and raw[1:8] == bytes(7)
            assert length >= 24 + size and raw[20 + size :] == bytes(length - 20 - size)
            assert name == "副本.md" and self.files[target]["directory"]
            assert source.value not in self.closed and target not in self.closed
            self.link_calls.append((source.value, target, name))
            if self.link_hook:
                self.link_hook(source.value, target, name)
            if self.link_status:
                return self.link_status
            key = self.name_key(target, name)
            if key in self.names:
                return -1073741771
            self.names[key] = self.files[source.value]
            self.files[source.value]["links"] += 1
            ctypes.cast(
                io_status, ctypes.POINTER(native.IoStatusBlock)
            ).contents.Result.Status = self.link_io_status
            return 0

    dlls = LinkDLLs()
    tree = WindowsFiles("C:\\原创库", _native=dlls.native)
    yield dlls, tree
    tree.close()
    assert sorted(value for value in dlls.closed if value in dlls.files) == list(range(100, dlls.next_handle))


def test_hardlink_uses_owned_file_and_target_handles_and_class_11_only(tool, link_kernel, monkeypatch):
    from collection_context.application.contracts import ContextError
    from collection_context.infrastructure import windows_native as native

    dlls, tree = link_kernel
    monkeypatch.setattr(tool.os, "link", lambda *_: pytest.fail("absolute link fallback forbidden"))
    tool.create_owned_hardlink(tree)
    assert len(dlls.link_calls) == 1
    source, target, _ = dlls.link_calls[0]
    assert dlls.ntdll.NtSetInformationFile.argtypes == [
        native.HANDLE,
        ctypes.POINTER(native.IoStatusBlock),
        native.HANDLE,
        native.U32,
        native.U32,
    ]
    assert dlls.ntdll.NtSetInformationFile.restype is native.I32
    created = [row for row in dlls.opened if row["disposition"] == 2]
    assert [row["name"] for row in created] == ["硬链接 合成", "原件", "副本", "原件.md"]
    assert all(row["root"] is not None and row["shares"] == 3 and row["security"] for row in created)
    assert all(not row["access"] & 0xD0000 for row in created)  # no DELETE/WRITE_DAC/WRITE_OWNER
    assert source in dlls.closed and target in dlls.closed
    for directory, name in (("原件", "原件.md"), ("副本", "副本.md")):
        assert tree.list_directory(f"硬链接 合成/{directory}", max_entries=1) == [
            {"name": name, "kind": "unsafe"}
        ]
        with pytest.raises(ContextError) as error:
            tree.read(f"硬链接 合成/{directory}/{name}")
        assert error.value.code == "forbidden_path"


@pytest.mark.parametrize("exchange", ["source_spelling", "target_spelling", "ancestor_spelling"])
def test_namespace_exchange_cannot_redirect_handle_relative_link(tool, link_kernel, exchange):
    dlls, tree = link_kernel
    external = {"id": b"e" * 16, "links": 1, "body": b"must remain unchanged"}
    replaced = []

    def exchange_name(source, target, _name):
        if exchange == "source_spelling":
            old = dlls.files[source]
        elif exchange == "target_spelling":
            old = dlls.files[target]
        else:
            old = dlls.names[dlls.name_key(tree.handle._value, "硬链接 合成")]
        key = next(key for key, data in dlls.names.items() if data is old)
        replaced.append(key)
        dlls.names[key] = external

    dlls.link_hook = exchange_name
    tool.create_owned_hardlink(tree)
    source, target, name = dlls.link_calls[0]
    assert dlls.names[dlls.name_key(target, name)] is dlls.files[source]
    assert dlls.names[replaced[0]] is external
    assert external == {"id": b"e" * 16, "links": 1, "body": b"must remain unchanged"}
    assert (int.from_bytes(external["id"], "little"), name) not in dlls.names
    # The fake namespace can bypass Windows sharing to exercise anchoring; this
    # asserts object targeting only, not native prevention of rename races.


@pytest.mark.parametrize("fault", ["ntstatus", "io_status", "collision", "close"])
def test_hardlink_errors_never_retry_or_leak_owned_handles(tool, link_kernel, fault):
    from test_context_windows_native import FakeFunction

    from collection_context.application.contracts import ContextError

    dlls, tree = link_kernel
    if fault == "ntstatus":
        dlls.link_status = -1073741790  # STATUS_ACCESS_DENIED
    elif fault == "io_status":
        dlls.link_io_status = -1073741790
    elif fault == "collision":
        dlls.link_hook = lambda _source, target, name: dlls.names.update(
            {dlls.name_key(target, name): {"body": b"do not replace"}}
        )
    else:
        original_close = dlls.close

        def close(handle):
            result = original_close(handle)
            return 0 if dlls.link_calls and handle.value == dlls.link_calls[0][0] else result

        dlls.kernel32.CloseHandle = FakeFunction(close)
    with pytest.raises((tool.AcceptanceError, ContextError)):
        tool.create_owned_hardlink(tree)
    assert len(dlls.link_calls) == 1
    assert set(range(100, dlls.next_handle)) - set(dlls.closed) == {tree.handle._value}
    if fault == "collision":
        _, target, name = dlls.link_calls[0]
        assert dlls.names[dlls.name_key(target, name)] == {"body": b"do not replace"}
