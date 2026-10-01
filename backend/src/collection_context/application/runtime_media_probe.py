"""Explicit original-fixture probe of receipt-installed media tools, not content quality."""

from __future__ import annotations

import io
import math
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.media import MediaPolicy, PreparedFrame
from collection_context.infrastructure.runtime_dependencies import RuntimeDependencies, _absolute
from collection_context.processing.inputs import _RuntimeLocalMedia


def probe_media_runtime(runtime_dir: Path, *, library_dir: Path, probe_dir: Path) -> dict[str, Any]:
    """Never borrow a library, source account, PATH executable or model credential.

    The output must be new and disjoint from runtime and library, including a
    not-yet-created library. Each decoder execution revalidates its receipt.
    Only this fixed three-second RGB/tone fixture is generated; no input URL.
    """
    output, library, runtime = map(_absolute, (probe_dir, library_dir, runtime_dir))
    for other in (library, runtime):
        if output == other or output in other.parents or other in output.parents:
            raise ContextError("runtime_probe_unsafe", "媒体探测目录须与资料库及运行目录分开。")
    if output.exists():
        raise ContextError("runtime_probe_output_exists", "媒体探测须使用不存在的新目录，不覆盖已有文件。")
    registry = RuntimeDependencies(runtime, library_dir=library)
    tools = {role: registry.resolve(role) for role in ("ffmpeg", "ffprobe")}
    output.mkdir(mode=0o700)
    report: dict[str, Any] = {
        "state": "failed",
        "verification_scope": "receipt_installed_original_local_media_fixture_only",
        "platform_requests": 0,
        "model_requests": 0,
        "content_quality_verified": False,
        "tools": {role: {"sha256": item.sha256, "version": item.version} for role, item in tools.items()},
    }
    with SafeFiles(output) as files:
        try:
            raw = b"".join(
                bytes(color) * (48 * 32) * 2 for color in ((20, 60, 100), (200, 30, 20), (30, 210, 50))
            )
            audio = io.BytesIO()
            with wave.open(audio, "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16000)
                target.writeframes(
                    b"".join(
                        struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / 16000)))
                        for i in range(48000)
                    )
                )
            files.write("original-rgb24.raw", raw)
            files.write("original-tone.wav", audio.getvalue())
            # The encoder has fixed original inputs, no stdin/network, no inherited
            # environment or diagnostic output, and a fixed time/output-read bound.
            encoder = registry.resolve("ffmpeg")
            if encoder.path != tools["ffmpeg"].path:
                raise ContextError("runtime_dependency_changed", "媒体组件在探测期间发生变化。")
            files.check_root()
            try:
                subprocess.run(
                    [
                        str(encoder.path),
                        "-nostdin",
                        "-n",
                        "-v",
                        "error",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "-video_size",
                        "48x32",
                        "-framerate",
                        "2",
                        "-i",
                        str(output / "original-rgb24.raw"),
                        "-i",
                        str(output / "original-tone.wav"),
                        "-t",
                        "3",
                        "-c:v",
                        "mpeg4",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-threads",
                        "1",
                        str(output / "original.mp4"),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=output,
                    env={"PATH": "", "LANG": "C", "LC_ALL": "C"},
                    check=True,
                    timeout=45,
                )
            except (OSError, subprocess.SubprocessError):
                raise ContextError("runtime_media_fixture_failed", "媒体工具未能创建原创探测样例。") from None
            files.check_root()
            with _RuntimeLocalMedia(
                files.read("original.mp4", max_bytes=1_000_000),
                ffmpeg=tools["ffmpeg"].path,
                ffprobe=tools["ffprobe"].path,
                resolve_tool=lambda role: registry.resolve(role).path,
                policy=MediaPolicy(audio_segment_seconds=2, audio_overlap_seconds=0.25),
            ) as media:
                segments = list(media.audio_segments())
                candidates, coverage = media.scan_frames()
                frames: list[PreparedFrame] = []
                media.frames(candidates, frames.append)
                if not (
                    media.info.has_audio
                    and media.info.width == 48
                    and media.info.height == 32
                    and segments
                    and segments[-1].end_seconds >= 2.9
                    and candidates
                    and len(frames) == len(candidates)
                    and all(frame.data.startswith(b"\x89PNG\r\n\x1a\n") for frame in frames)
                ):
                    raise ContextError("runtime_media_probe_failed", "原创媒体样例没有完成音频及画面解码。")
                for frame in frames:
                    files.write(frame.candidate.evidence_id + ".png", frame.data)
                report.update(
                    state="verified",
                    functional_verified=True,
                    decoder_versions=media.decoder_versions,
                    audio_segments=len(segments),
                    selected_frames=len(frames),
                    sampled_frames=coverage["sampled_frames"],
                    png_encoder_verified=True,
                    audio_decode_verified=True,
                )
        except ContextError as error:
            report["error_code"] = error.code
            raise
        finally:
            files.write("report.json", canonical_bytes(report))
    return report
