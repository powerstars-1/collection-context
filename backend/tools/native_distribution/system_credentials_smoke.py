"""Explicit frozen-console macOS credential smoke with original loopback fixtures.

This tool creates real, uniquely scoped SYSTEM credential items containing invalid
synthetic values. Run only with authorization. It never reads an existing key or
uses a cloud/platform endpoint. Host code prepares data and checks confirmed native
metadata; native configure/prepare/submit/worker perform product operations.
Cleanup deletes ONLY exact slots proven committed by successful native configure.
Unknown outcomes are preserved, never enumerated or guessed. Raw child output and
exception details stay in bounded process memory and never enter the report.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import platform
import pty
import re
import select
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import SYSTEM_SECRET_MANIFEST
from collection_context.library.store import LibraryStore
from collection_context.processing.profiles import ModelCatalog

MAX_OUTPUT = 262_144
MAX_SCAN_BYTES = 64_000_000
MAX_SCAN_FILES = 512
PROMPT = "模型API Key（不回显）：".encode()
MODELS = {"vision": "original-native-vision", "summary": "original-native-summary"}
SAFE_CHILD_CODES = frozenset(
    {
        "credential_locked",
        "credential_denied",
        "credential_unavailable",
        "credential_missing",
        "credential_conflict",
        "invalid_credential_reference",
        "invalid_credential",
        "system_secret_outcome_unknown",
        "system_secret_unsupported",
        "system_secret_closed",
        "system_secret_directory_invalid",
        "system_secret_directory_changed",
        "system_secret_metadata_invalid",
        "invalid_argument",
        "invalid_model_config",
        "interactive_credential_required",
        "secret_directory_overlap",
        "unsafe_secret_permissions",
        "secret_directory_exists",
        "storage_unavailable",
        "unsupported_platform",
        "model_config_missing",
        "model_capability_required",
        "credential_backend_conflict",
        "credential_backend_unsupported",
    }
)


def unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError
        value[name] = item
    return value


def reject_constant(_):
    raise ValueError


def safe_child_code(output: bytes, keys: dict[str, str]) -> str | None:
    """Only a strict error envelope's finite allowlisted CODE survives parsing.

    Messages, data, references, request IDs and raw output are never returned.
    Duplicate keys, nonfinite JSON, malformed UTF-8 and key-bearing output fail
    closed. This parsing does not establish a successful or reversible mutation.
    """
    try:
        if len(output) > MAX_OUTPUT or any(key.encode() in output for key in keys.values()):
            return None
        candidates = [line[line.find("{") :] for line in output.decode("utf-8").splitlines() if "{" in line]
        if len(candidates) != 1:
            return None
        value = json.loads(candidates[0], object_pairs_hook=unique_object, parse_constant=reject_constant)
        if (
            not isinstance(value, dict)
            or set(value) != {"ok", "data", "error", "meta"}
            or value["ok"] is not False
            or value["data"] is not None
            or not isinstance(value["meta"], dict)
            or type(value["meta"].get("schema_version")) is not int
            or value["meta"]["schema_version"] != 1
            or not isinstance(value["error"], dict)
        ):
            return None
        code = value["error"].get("code")
        return code if isinstance(code, str) and code in SAFE_CHILD_CODES else None
    except (ValueError, UnicodeError, TypeError):
        return None


def fail() -> None:
    raise RuntimeError("Bounded native credential acceptance did not complete")


def checked_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or path in {Path(path.anchor), Path.home()}:
        fail()
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            fail()
    return path


def binary_hash(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK) or info.st_size > 1_073_741_824:
        fail()
    result = hashlib.sha256()
    with path.open("rb") as stream:
        magic = stream.read(4)
        if magic not in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
            fail()
        result.update(magic)
        while block := stream.read(1_048_576):
            result.update(block)
    return result.hexdigest()


def decode_result(output: bytes, keys: dict[str, str]) -> dict[str, Any]:
    if len(output) > MAX_OUTPUT or any(value.encode() in output for value in keys.values()):
        fail()
    lines = output.decode("utf-8").replace("\r\n", "\n").splitlines()
    candidates = [line[line.find("{") :] for line in lines if "{" in line]
    if not candidates:
        fail()
    result = json.loads(candidates[-1])
    if (
        not isinstance(result, dict)
        or result.get("ok") is not True
        or not isinstance(result.get("data"), dict)
    ):
        fail()
    return result["data"]


def child(
    binary: Path,
    arguments: list[str],
    stage: Path,
    env: dict[str, str],
    keys: dict[str, str],
    *,
    prompt_key: str | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bounded PTY; input is never an argument, environment value or transcript file."""
    interactive = prompt_key is not None
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic.update(
        transport="pty" if interactive else "pipe",
        transport_state="not_opened",
        child_status="not_started",
        prompt_seen=False,
        input_sent=False,
        result="started",
        context_error_code=None,
        failure_label=None,
        cleanup_status="not_needed",
    )
    master, slave = pty.openpty() if interactive else (-1, -1)
    process = None
    captured = bytearray()
    sent = prompt_key is None
    if interactive:
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attributes)

    def controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        if interactive:
            process = subprocess.Popen(
                [str(binary), "cli", *arguments],
                cwd=stage,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=controlling_terminal,
            )
            os.close(slave)
            slave = -1
        else:
            # No preexec/fork callback while the loopback fixture has threads.
            process = subprocess.Popen(
                [str(binary), "cli", *arguments],
                cwd=stage,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            master = process.stdout.fileno()
        diagnostic.update(transport_state="opened", child_status="running")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    block = os.read(master, 8192)
                except OSError as error:
                    if interactive and error.errno == errno.EIO:
                        diagnostic["transport_state"] = "pty_eio_eof"
                        break
                    if process.poll() is not None:
                        diagnostic["transport_state"] = "eof_after_exit"
                        break
                    diagnostic["failure_label"] = "transport_read_failed"
                    fail()
                if not block:
                    diagnostic["transport_state"] = "eof"
                    break
                captured.extend(block)
                if len(captured) > MAX_OUTPUT:
                    diagnostic["failure_label"] = "bounded_output_exceeded"
                    fail()
                if interactive and PROMPT in captured:
                    diagnostic["prompt_seen"] = True
                if not sent and PROMPT in captured:
                    assert prompt_key is not None
                    raw = (prompt_key + "\n").encode()
                    if os.write(master, raw) != len(raw):
                        diagnostic["failure_label"] = "pty_input_write_failed"
                        fail()
                    sent = True
                    diagnostic["input_sent"] = True
            if process.poll() is not None and not ready:
                break
        if process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic.update(child_status="timed_out", failure_label="child_timeout")
                fail()
            process.wait(timeout=remaining)
        diagnostic["child_status"] = "exited_zero" if process.returncode == 0 else "exited_nonzero"
        if process.returncode != 0:
            code = safe_child_code(bytes(captured), keys)
            diagnostic.update(
                context_error_code=code,
                failure_label="child_context_error" if code else "child_nonzero_unclassified",
            )
            fail()
        if not sent:
            diagnostic["failure_label"] = "pty_prompt_or_input_not_completed"
            fail()
        diagnostic["failure_label"] = "success_envelope_not_verified"
        value = decode_result(bytes(captured), keys)
        diagnostic.update(result="success", failure_label=None)
        return value
    except subprocess.TimeoutExpired:
        diagnostic.update(result="failed", child_status="timed_out", failure_label="child_timeout")
        fail()
    except BaseException:
        diagnostic["result"] = "failed"
        if diagnostic["failure_label"] is None:
            diagnostic["failure_label"] = "child_transport_or_start_failure"
        raise
    finally:
        if slave >= 0:
            os.close(slave)
        if interactive and master >= 0:
            os.close(master)
        elif process is not None and process.stdout is not None:
            process.stdout.close()
        if process is not None and process.poll() is None:
            diagnostic["cleanup_status"] = "terminated"
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                diagnostic["cleanup_status"] = "killed"
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        captured.clear()


def original_png() -> bytes:
    def chunk(name: bytes, value: bytes) -> bytes:
        return struct.pack("!I", len(value)) + name + value + struct.pack("!I", zlib.crc32(name + value))

    pixels = b"".join(b"\x00" + bytes((227, 222, 211)) * 64 for _ in range(64))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def confirmed_slot(
    workspace: Path, private: Path, profile_id: str, role: str, endpoint: str
) -> tuple[str, str]:
    """Read only the profile/ref returned by this successful native operation."""
    store = LibraryStore(workspace)
    try:
        profile = ModelCatalog(store).get(profile_id)
        if store.snapshot()["settings"]["model_roles"].get(role) != profile_id:
            fail()
    finally:
        store.close()
    if profile["role"] != role or profile["model"] != MODELS[role] or profile["base_url"] != endpoint:
        fail()
    ref = profile["credential_ref"]
    if not re.fullmatch(r"k_[0-9a-f]{32}", ref):
        fail()
    with SafeFiles(private) as files:
        manifest = json.loads(files.read(SYSTEM_SECRET_MANIFEST, max_bytes=4096, private=True))
        confirmed = json.loads(files.read(ref, max_bytes=4096, private=True))
    if (
        set(manifest) != {"schema_version", "backend", "namespace"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["backend"] != "macos_keychain"
        or not isinstance(manifest["namespace"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", manifest["namespace"])
        or confirmed != {**manifest, "ref": ref, "confirmed": True}
        or type(confirmed.get("confirmed")) is not bool
    ):
        fail()
    return "org.collection-context.credentials." + manifest["namespace"], ref


def no_plaintext(root: Path, keys: dict[str, str]) -> bool:
    total, count = 0, 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            count += 1
            if count > MAX_SCAN_FILES:
                fail()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                fail()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > MAX_SCAN_BYTES:
                    fail()
                value = path.read_bytes()
                if any(key.encode() in value for key in keys.values()):
                    return False
            else:
                fail()
    return True


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *_):
        self.failed = True  # Never emit request body or raw exceptions.


def exercise(
    binary: Path, stage: Path, digest: str, *, use_login_keychain_context: bool = False
) -> tuple[bool, Path]:
    keys = {role: "original-native-invalid-" + uuid.uuid4().hex for role in MODELS}
    requests: list[dict[str, Any]] = []
    slots: list[tuple[str, str]] = []
    unknown = False
    cleaned = 0
    passed = False
    fixture = None
    fixture_thread = None
    report: dict[str, Any] = {
        "passed": False,
        "binary_sha256": digest,
        "platform_requests": 0,
        "cloud_requests": 0,
        "phase": "prepare_environment",
        "steps": [],
        "not_verified": [
            "desktop GUI",
            "Windows",
            "Linux",
            "source login",
            "audio",
            "native signing",
            "recognition quality",
        ],
    }
    home = stage / "fresh-home"
    home.mkdir(mode=0o700)
    workspace, private = stage / "原创 资料库", stage / "原创 系统凭据元数据"
    # A fresh HOME has no login Keychain and must fail rather than initialize or
    # unlock one. An explicit separate acceptance may use the logged-in user's
    # OS context; all product paths remain fixed in this new stage. Never query
    # existing items, read project defaults, migrate keys or authorize OS dialogs.
    env = {
        "PATH": "",
        "HOME": str(Path.home() if use_login_keychain_context else home),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }

    def native(phase: str, arguments: list[str], *, prompt_key: str | None = None):
        report["phase"] = phase
        step: dict[str, Any] = {"phase": phase}
        report["steps"].append(step)
        return child(binary, arguments, stage, env, keys, prompt_key=prompt_key, diagnostic=step)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.connection.settimeout(5)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_OUTPUT:
                    fail()
                body = json.loads(self.rfile.read(length))
                model = body.get("model")
                role = next((role for role, name in MODELS.items() if model == name), None)
                matched = role is not None and hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + keys[role]
                )
                requests.append({"model": model if role else "unexpected", "authorization_matched": matched})
                if not matched or self.path != "/v1/chat/completions" or len(requests) > 2:
                    fail()
                content = (
                    "原创画面：画布390×844。"
                    if role == "vision"
                    else "## 阅读稿\n原创画面使用画布390×844。[f_000000]\n\n## 内容总结\n这是原创协议验证样例。[f_000000]"
                )
                payload = json.dumps(
                    {
                        "model": model,
                        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                        "usage": {"total_tokens": 7},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except BaseException:
                self.server.failed = True
                self.send_response(400)
                self.end_headers()

    try:
        fixture = Fixture(("127.0.0.1", 0), Handler)
        fixture.failed = False
        endpoint = f"http://127.0.0.1:{fixture.server_port}/v1"
        base = ["--workspace", str(workspace)]
        native("native_init", [*base, "init"])
        report["phase"] = "prepare_original_fixture"
        store = LibraryStore(workspace)
        try:
            item = store.upsert(
                {
                    "native_id": "990701",
                    "media_type": "image",
                    "title": "原创原生系统凭据夹具",
                    "body": "只测试协议与凭据接线，不是识别质量样本。",
                },
                kind="saved",
                scope_id="s_saved",
            )["item"]
        finally:
            store.close()
        image = stage / "original.png"
        image.write_bytes(original_png())
        # Configure before starting HTTP threads: PTY child needs a controlling tty.
        for role in MODELS:
            unknown = True  # A timed-out/failed configure is not cleanup authority.
            data = native(
                "configure_vision" if role == "vision" else "configure_summary",
                [
                    *base,
                    "configure-model",
                    "--role",
                    role,
                    "--base-url",
                    endpoint,
                    "--model",
                    MODELS[role],
                    "--protocol",
                    "chat",
                    "--credential-dir",
                    str(private),
                    "--credential-backend",
                    "system",
                    "--prompt-key",
                ],
                prompt_key=keys[role],
            )
            if data.get("model_requests") != 0:
                fail()
            report["phase"] = "confirm_vision_metadata" if role == "vision" else "confirm_summary_metadata"
            slots.append(confirmed_slot(workspace, private, data["profile_id"], role, endpoint))
            unknown = False
        prepared = native(
            "native_prepare_images", [*base, "prepare-images", "--ref", item["id"], "--image", str(image)]
        )
        queued = native(
            "native_submit_extraction",
            [
                *base,
                "submit-extraction",
                "--input-id",
                prepared["input_id"],
                "--idempotency-key",
                "original-native-system-once",
                "--max-calls",
                "2",
            ],
        )
        fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        fixture_thread.start()
        executed = native(
            "native_worker_once",
            [
                *base,
                "worker",
                "--credential-dir",
                str(private),
                "--credential-backend",
                "system",
                "--allow-model-calls",
                "--once",
                "--max-jobs",
                "1",
            ],
        )
        status = native("native_job_status", [*base, "job-status", "--job-id", queued["id"]])
        report["phase"] = "verify_two_authorized_requests"
        if (
            executed.get("handled") != 1
            or status.get("state") != "succeeded"
            or len(requests) != 2
            or fixture.failed
        ):
            fail()
        if [value["model"] for value in requests] != list(MODELS.values()) or not all(
            value["authorization_matched"] for value in requests
        ):
            fail()
        report["phase"] = "verify_no_plaintext"
        report["no_plaintext_in_library_and_metadata"] = no_plaintext(stage, keys)
        if not report["no_plaintext_in_library_and_metadata"]:
            fail()
        report["phase"] = "verify_binary_unchanged"
        if binary_hash(binary) != digest:
            fail()
        passed = True
    except BaseException:
        report["failure"] = "native_system_credentials_acceptance_incomplete"
    finally:
        if fixture_thread is not None and fixture is not None:
            fixture.shutdown()
            fixture_thread.join(timeout=5)
        if fixture is not None:
            fixture.server_close()
        if slots:
            try:
                from collection_context.infrastructure.macos_credentials import MacOSKeychain

                cleanup = MacOSKeychain()
                for service, account in slots:
                    try:
                        cleanup.delete(service, account)  # Exact known successes only; never read/enumerate.
                        cleaned += 1
                    except BaseException:
                        pass
            except BaseException:
                pass
        report.update(
            passed=passed and not unknown and cleaned == 2,
            native_configured_roles=len(slots),
            native_model_requests=len(requests),
            request_checks=requests,
            cleanup_confirmed_count=cleaned,
            cleanup_unknown=unknown,
            cleanup_incomplete=cleaned != len(slots),
            child_path_empty=True,
            isolated_home=not use_login_keychain_context,
            keychain_context="logged_in_user" if use_login_keychain_context else "fresh_home",
            real_existing_key_reads=False,
        )
        if report["passed"]:
            report["phase"] = "completed"
        destination = stage / "report.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        destination.chmod(0o600)
    return report["passed"], destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--use-login-keychain-context",
        action="store_true",
        help="Explicitly use the current user's OS HOME; still only new synthetic exact items and fixed product paths",
    )
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin":
            fail()
        binary, stage = checked_path(args.binary), checked_path(args.output)
        repository = Path(__file__).resolve().parents[3]
        if stage.exists() or stage.is_relative_to(repository) or repository.is_relative_to(stage):
            fail()
        digest = binary_hash(binary)
        stage.mkdir(mode=0o700)  # Existing parent required, never recursive broad creation.
        passed, report = exercise(
            binary, stage, digest, use_login_keychain_context=args.use_login_keychain_context
        )
        print(json.dumps({"passed": passed, "report": str(report)}, ensure_ascii=False))
        return 0 if passed else 1
    except BaseException:
        print(json.dumps({"passed": False, "failure": "native_system_credentials_precheck_or_report_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
