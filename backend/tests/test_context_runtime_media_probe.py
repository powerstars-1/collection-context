"""Owned fixture probe guards and reporting; no actual decoders or model calls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from collection_context.application import runtime_media_probe as probe
from collection_context.application.contracts import ContextError
from collection_context.cli import main


@pytest.mark.parametrize(
    "kind", ["existing", "library", "library_child", "runtime", "runtime_parent", "link", "relative", "home"]
)
def test_unsafe_output_before_receipt_or_execution(tmp_path, monkeypatch, kind):
    root = tmp_path.resolve()
    library, runtime = root / "library", root / "runtime"
    existing = root / "existing"
    existing.mkdir()
    link = root / "link"
    link.symlink_to(existing, target_is_directory=True)
    output = {
        "existing": existing,
        "library": library,
        "library_child": library / "probe",
        "runtime": runtime,
        "runtime_parent": root,
        "link": link,
        "relative": Path("relative"),
        "home": Path.home(),
    }[kind]
    monkeypatch.setattr(probe, "RuntimeDependencies", lambda *a, **k: pytest.fail("receipt read"))
    with pytest.raises(ContextError):
        probe.probe_media_runtime(runtime, library_dir=library, probe_dir=output)
    assert not library.exists() and not runtime.exists()


def test_missing_receipt_does_not_create_output_or_fallback(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: pytest.fail("process"))
    with pytest.raises(ContextError) as caught:
        probe.probe_media_runtime(root / "runtime", library_dir=root / "library", probe_dir=root / "probe")
    assert caught.value.code == "runtime_dependency_missing"
    assert not (root / "probe").exists()


def fake_registry(root, monkeypatch):
    calls = []

    class Registry:
        def __init__(self, runtime, **kwargs):
            assert runtime == root / "runtime" and kwargs == {"library_dir": root / "library"}

        def resolve(self, role):
            calls.append(role)
            return SimpleNamespace(path=root / "runtime" / role, sha256="a" * 64, version="synthetic")

    monkeypatch.setattr(probe, "RuntimeDependencies", Registry)
    return calls


def test_complete_report_after_decoding_closed_and_no_library_created(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    calls = fake_registry(root, monkeypatch)
    events = []

    def encode(command, **kwargs):
        assert command[0] == str(root / "runtime" / "ffmpeg")
        assert kwargs["env"] == {"PATH": "", "LANG": "C", "LC_ALL": "C"}
        assert kwargs["timeout"] == 45 and kwargs["check"] is True
        assert kwargs["stdout"] == kwargs["stderr"] == probe.subprocess.DEVNULL
        Path(command[-1]).write_bytes(b"synthetic mp4")

    frame = SimpleNamespace(
        data=b"\x89PNG\r\n\x1a\nsynthetic", candidate=SimpleNamespace(evidence_id="f_000000")
    )

    class Media:
        info = SimpleNamespace(has_audio=True, width=48, height=32)
        decoder_versions = {"ffmpeg": "synthetic", "ffprobe": "synthetic"}

        def __init__(self, source, **kwargs):
            assert source == b"synthetic mp4"
            assert kwargs["resolve_tool"]("ffprobe") == root / "runtime" / "ffprobe"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("closed")

        def audio_segments(self):
            return iter([SimpleNamespace(end_seconds=3)])

        def scan_frames(self):
            return [frame.candidate], {"sampled_frames": 6}

        def frames(self, candidates, consumer):
            consumer(frame)

    monkeypatch.setattr(probe.subprocess, "run", encode)
    monkeypatch.setattr(probe, "_RuntimeLocalMedia", Media)
    result = probe.probe_media_runtime(
        root / "runtime", library_dir=root / "library", probe_dir=root / "probe"
    )
    assert events == ["closed"] and calls == ["ffmpeg", "ffprobe", "ffmpeg", "ffprobe"]
    assert result["state"] == "verified" and result["functional_verified"] is True
    assert result["model_requests"] == result["platform_requests"] == 0
    assert result["content_quality_verified"] is False
    assert not (root / "library").exists()
    assert (root / "probe" / "report.json").is_file()


def test_encoder_failure_has_safe_failure_report_not_success(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    fake_registry(root, monkeypatch)

    def fail(*args, **kwargs):
        raise OSError("synthetic private diagnostic")

    monkeypatch.setattr(probe.subprocess, "run", fail)
    with pytest.raises(ContextError) as caught:
        probe.probe_media_runtime(root / "runtime", library_dir=root / "library", probe_dir=root / "probe")
    assert caught.value.code == "runtime_media_fixture_failed"
    report = (root / "probe" / "report.json").read_text()
    assert '"state":"failed"' in report and "private diagnostic" not in report


def test_cli_media_probe_requires_explicit_runtime_without_creating_files(tmp_path, capsys):
    assert (
        main(
            [
                "--workspace",
                str(tmp_path / "library"),
                "probe-media-runtime",
                "--probe-dir",
                str(tmp_path / "probe"),
            ]
        )
        == 1
    )
    assert "runtime_install_directory_required" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
