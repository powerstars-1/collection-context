"""Explicit media runtime guards; real decoding is opt-in and uses original fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.media import LocalMedia, MediaPolicy
from collection_context.infrastructure.ocr import OcrResult
from collection_context.infrastructure.runtime_dependencies import RECEIPT_NAME
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import DEFAULT_VIDEO_POLICY, PreparedInputs, _RuntimeLocalMedia


def inject_ocr(monkeypatch):
    # Decoder guards remain unit tested independently of OCR installation/quality.
    monkeypatch.setattr(
        "collection_context.processing.inputs.load_runtime_ocr",
        lambda *args, **kwargs: SimpleNamespace(
            engine_version="original-injected",
            model_hashes={},
            recognize=lambda image: OcrResult([], 0, "no_text", "original-injected"),
        ),
    )


def test_product_default_sampling_is_dense_but_ocr_and_cloud_counts_remain_bounded():
    assert DEFAULT_VIDEO_POLICY.sample_fps == 10
    assert DEFAULT_VIDEO_POLICY.max_sampled_frames == 72_002
    assert DEFAULT_VIDEO_POLICY.max_duration_seconds == 7200
    assert DEFAULT_VIDEO_POLICY.max_ocr_frames == 480
    assert DEFAULT_VIDEO_POLICY.max_selected_frames == 240


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path.resolve() / "原创 媒体库")
    yield value
    value.close()


def add(store):
    return store.upsert(
        {"native_id": "1212", "title": "原创媒体运行夹具"}, kind="saved", scope_id="s_fixture"
    )["item"]


def receipt(runtime, tools):
    architecture = {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    result = {"schema_version": 1, "host": {"system": platform.system(), "arch": architecture}, "tools": {}}
    for role, path in tools.items():
        data = path.read_bytes()
        result["tools"][role] = {
            "relative_path": path.relative_to(runtime).as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "version": "8.1.1-test-fixture",
            "build_version": "original-runtime-test",
            "source_url": "https://ffmpeg.org/download.html",
            "license_id": "GPL-3.0-or-later",
        }
    path = runtime / RECEIPT_NAME
    path.write_text(json.dumps(result), encoding="utf-8")
    path.chmod(0o600)
    return result


def fake_runtime(tmp_path):
    runtime = tmp_path.resolve() / "原创 runtime"
    runtime.mkdir(mode=0o700)
    tools = {}
    for role in ("ffmpeg", "ffprobe"):
        tools[role] = runtime / role
        tools[role].write_bytes(b"original-binary-fixture-" + role.encode())
        tools[role].chmod(0o700)
    receipt(runtime, tools)
    return runtime, tools


def test_each_execution_revalidates_role_and_never_falls_back(monkeypatch):
    calls = []
    executable = Path("/controlled/runtime/ffmpeg")
    media = object.__new__(_RuntimeLocalMedia)
    media._fixed_tools = {executable: "ffmpeg"}
    media._resolve_tool = lambda role: calls.append(role) or executable
    monkeypatch.setattr(LocalMedia, "_run", lambda self, binary, arguments, **kwargs: b"synthetic")
    assert media._run(executable, ["-version"], max_bytes=100) == b"synthetic"
    assert media._run(executable, ["-version"], max_bytes=100) == b"synthetic"
    assert calls == ["ffmpeg", "ffmpeg"]
    media._resolve_tool = lambda role: Path("/another/runtime/ffmpeg")
    with pytest.raises(ContextError) as caught:
        media._run(executable, ["-version"], max_bytes=100)
    assert caught.value.code == "runtime_dependency_changed"


def test_failed_receipt_check_does_not_enter_native_decoder(monkeypatch):
    executable = Path("/controlled/runtime/ffprobe")
    media = object.__new__(_RuntimeLocalMedia)
    media._fixed_tools = {executable: "ffprobe"}

    def fail(role):
        raise ContextError("runtime_dependency_changed", "synthetic changed hash")

    def forbidden(*args, **kwargs):
        raise AssertionError("Decoder execution must not happen after runtime verification fails")

    media._resolve_tool = fail
    monkeypatch.setattr(LocalMedia, "_run", forbidden)
    with pytest.raises(ContextError) as caught:
        media._run(executable, ["-version"], max_bytes=100)
    assert caught.value.code == "runtime_dependency_changed"


@pytest.mark.parametrize("corruption", ["missing", "hash", "unsafe_permissions"])
def test_bad_runtime_is_not_fallback_and_does_not_prepare_input(store, tmp_path, monkeypatch, corruption):
    inject_ocr(monkeypatch)
    runtime, tools = fake_runtime(tmp_path)
    item = add(store)
    before = store.snapshot()
    if corruption == "missing":
        (runtime / RECEIPT_NAME).unlink()
        expected = "runtime_dependency_missing"
    elif corruption == "hash":
        tools["ffprobe"].write_bytes(b"changed bytes")
        expected = "runtime_dependency_integrity"
    else:
        (runtime / RECEIPT_NAME).chmod(0o644)
        expected = "runtime_dependency_unsafe"

    def forbidden(*args, **kwargs):
        raise AssertionError("No native process or PATH fallback permitted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store, runtime_dir=runtime).prepare_video(item["id"], b"synthetic-source")
    assert caught.value.code == expected and store.snapshot() == before
    assert not store.get(item["id"]).get("prepared_input")


@pytest.mark.parametrize("replace_receipt", [False, True])
def test_mid_session_binary_or_coherent_receipt_change_stops_next_execution(
    store, tmp_path, monkeypatch, replace_receipt
):
    inject_ocr(monkeypatch)
    runtime, tools = fake_runtime(tmp_path)
    item = add(store)
    before = store.snapshot()
    calls = []

    def version(self, executable, arguments, **kwargs):
        calls.append(executable.name)
        if len(calls) == 1:
            tools["ffprobe"].write_bytes(b"changed tool bytes")
            if replace_receipt:
                receipt(runtime, tools)
        return (executable.name + " version synthetic\n").encode()

    monkeypatch.setattr(LocalMedia, "_run", version)
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store, runtime_dir=runtime).prepare_video(item["id"], b"synthetic-source")
    assert caught.value.code == (
        "runtime_dependency_changed" if replace_receipt else "runtime_dependency_integrity"
    )
    assert calls == ["ffmpeg"] and store.snapshot() == before
    assert not store.get(item["id"]).get("prepared_input")


def test_missing_fixed_ocr_blocks_before_decoder_or_library_write(store, tmp_path, monkeypatch):
    runtime, _ = fake_runtime(tmp_path)
    item = add(store)
    before = store.snapshot()
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Missing OCR must not enter decoder")
    )
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: pytest.fail("Must not borrow system OCR/media")
    )
    with pytest.raises(ContextError) as caught:
        PreparedInputs(store, runtime_dir=runtime).prepare_video(item["id"], b"original-fixture")
    assert caught.value.code == "runtime_dependency_missing" and store.snapshot() == before


def test_prepared_video_connects_ocr_records_selection_and_originals(store, tmp_path, monkeypatch):
    from test_context_ocr_selection import Engine, page, png, result

    from collection_context.infrastructure.media import PROCESSOR_VERSION

    runtime, _ = fake_runtime(tmp_path)
    item = add(store)
    engine = Engine([result(), ContextError("ocr_failed", "original page failure")])
    frames = [page(0), page(1), page(2, data=png(color=(0, 20, 30, 255))), page(3, reasons=("last",))]
    seen = []

    class Media:
        strategy_hash = "original-source-strategy"
        info = SimpleNamespace(has_audio=False)

        def __init__(self, data, **kwargs):
            seen.append(kwargs["policy"].max_selected_frames)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def audio_segments(self):
            return []

        def scan_frames(self):
            return [f.candidate for f in frames], {"complete": False, "sampled_frames": 4}

        def frames(self, candidates, consumer):
            for value in frames:
                consumer(value)

    monkeypatch.setattr("collection_context.processing.inputs._RuntimeLocalMedia", Media)
    monkeypatch.setattr("collection_context.processing.inputs.load_runtime_ocr", lambda *a, **k: engine)
    identity = PreparedInputs(store, runtime_dir=runtime).prepare_video(
        item["id"], b"original-source-video", policy=MediaPolicy(max_ocr_frames=10)
    )
    payload = PreparedInputs(store).load(identity)
    assert payload["processor_version"] == PROCESSOR_VERSION
    assert payload["originals"][0]["sha256"] == hashlib.sha256(b"original-source-video").hexdigest()
    assert payload["audio"] == [] and seen == [10]
    assert len(payload["frames"]) == 3 and payload["coverage"]["ocr_candidates"] == 4
    assert payload["coverage"]["ocr_duplicate_frames"] == 1
    assert payload["coverage"]["ocr_state"] == "partial"
    assert payload["coverage"]["ocr_failures"] == ["f_000002"]
    assert payload["frames"][1]["candidate"]["reasons"][-1] == "ocr_error"
    assert any("failed" in gap for gap in payload["coverage"]["gaps"])
    assert not store.snapshot()["jobs"]


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_MEDIA_RUNTIME") != "1", reason="Explicit original native media test only"
)
@pytest.mark.parametrize("has_audio", [True, False])
def test_real_original_small_video_uses_receipt_without_path(store, tmp_path, monkeypatch, has_audio):
    inject_ocr(monkeypatch)
    # These copied developer binaries may still link system/Homebrew shared libraries.
    # This verifies receipt execution and decoding, NOT standalone distribution readiness.
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Explicit installed native tools required; no download")
    runtime = tmp_path.resolve() / "真实媒体 runtime"
    runtime.mkdir(mode=0o700)
    tools = {}
    for role, source in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        tools[role] = runtime / role
        shutil.copyfile(Path(source).resolve(), tools[role])
        tools[role].chmod(0o700)
    receipt(runtime, tools)
    video = tmp_path / "原创短视频.mp4"
    subprocess.run(
        [
            str(tools["ffmpeg"]),
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10",
            *(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=16000"] if has_audio else []),
            "-t",
            "1.2",
            "-c:v",
            "mpeg4",
            *(["-c:a", "aac"] if has_audio else []),
            "-threads",
            "1",
            str(video),
        ],
        check=True,
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "", "LANG": "C"},
    )
    item = add(store)
    monkeypatch.setenv("PATH", "")
    original_popen = subprocess.Popen
    spawned = []

    def track(command, **kwargs):
        executable = Path(command[0])
        assert executable.is_absolute() and executable in tools.values()
        assert kwargs.get("shell") is False
        assert set(kwargs["env"]) <= {"PATH", "LANG", "LC_ALL", "SystemRoot"}
        spawned.append(executable.name)
        return original_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", track)
    identity = PreparedInputs(store, runtime_dir=runtime).prepare_video(
        item["id"],
        video.read_bytes(),
        policy=MediaPolicy(command_timeout_seconds=10),
        expected_content_hash=item["content_hash"],
    )
    payload = PreparedInputs(store).load(identity)
    assert payload["originals"] and payload["frames"] and bool(payload["audio"]) is has_audio
    assert payload["coverage"]["has_audio"] is has_audio
    assert store.get(item["id"])["prepared_input"] == identity
    assert "ffprobe" in spawned and spawned.count("ffmpeg") >= (4 if has_audio else 3)
