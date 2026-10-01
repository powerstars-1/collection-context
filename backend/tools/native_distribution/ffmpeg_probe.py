"""Explicit media-pair functional probe using only original local fixtures.

No source login, user library, external media, model calls or runtime install.
The explicit output must be new. Tests software capability, not content quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import wave
from pathlib import Path

from collection_context.infrastructure.media import LocalMedia, MediaPolicy


def exercise(ffmpeg: Path, ffprobe: Path, output: Path) -> dict:
    for tool in (ffmpeg, ffprobe):
        if not tool.is_absolute() or not tool.is_file() or any(p.is_symlink() for p in (tool, *tool.parents)):
            raise ValueError("Expected explicit ordinary media tool paths")
    repository = Path(__file__).resolve().parents[3]
    if (
        not output.is_absolute()
        or output.exists()
        or ".." in output.parts
        or any(p.is_symlink() for p in (output, *output.parents))
        or output in {Path(output.anchor), Path.home(), repository}
        or repository in output.parents
    ):
        raise ValueError("Expected a new specific repository-external probe directory")
    output.mkdir(mode=0o700)
    raw = output / "original-rgb24.raw"
    raw.write_bytes(
        b"".join(bytes(color) * (48 * 32) * 2 for color in ((20, 60, 100), (200, 30, 20), (30, 210, 50)))
    )
    audio = output / "original-tone.wav"
    with wave.open(str(audio), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(48000)
            )
        )
    video = output / "original.mp4"
    report = {
        "state": "running",
        "scope": "original_local_software_capability_fixture_only",
        "platform_requests": 0,
        "model_requests": 0,
        "product_runtime_installed": False,
        "tools": {
            name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
        },
    }
    environment = {"PATH": "", "HOME": str(output), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    try:
        subprocess.run(
            [
                str(ffmpeg),
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
                str(raw),
                "-i",
                str(audio),
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
                str(video),
            ],
            env=environment,
            capture_output=True,
            check=True,
            timeout=45,
        )
        with LocalMedia(
            video.read_bytes(),
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            policy=MediaPolicy(audio_segment_seconds=2, audio_overlap_seconds=0.25),
        ) as media:
            assert media.info.has_audio and media.info.width == 48 and media.info.height == 32
            segments = list(media.audio_segments())
            assert segments and segments[-1].end_seconds >= 2.9
            candidates, coverage = media.scan_frames()
            assert candidates and coverage["sampled_frames"] > 0
            frames = []
            media.frames(candidates, frames.append)
            assert len(frames) == len(candidates) and all(
                frame.data.startswith(b"\x89PNG\r\n\x1a\n") for frame in frames
            )
            for frame in frames:
                (output / (frame.candidate.evidence_id + ".png")).write_bytes(frame.data)
            report.update(
                {
                    "decoder_versions": media.decoder_versions,
                    "audio_segments": len(segments),
                    "selected_frames": len(frames),
                    "sampled_frames": coverage["sampled_frames"],
                    "png_encoder_verified": True,
                    "audio_decode_verified": True,
                    "state": "passed",
                    "content_quality_verified": False,
                }
            )
        for name, tool in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            dependencies = subprocess.check_output(
                ["/usr/bin/otool", "-L", str(tool)], env=environment, text=True, timeout=20
            )
            lines = [line.strip().split(" (", 1)[0] for line in dependencies.splitlines()[1:] if line.strip()]
            report["tools"][name]["dynamic_dependencies"] = lines
            assert lines and all(line.startswith(("/usr/lib/", "/System/Library/")) for line in lines)
        report["system_dynamic_dependencies_only"] = True
    except BaseException:
        report["state"] = "failed"
        raise
    finally:
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit original-fixture media pair probe")
    for name in ("ffmpeg", "ffprobe", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.ffmpeg, args.ffprobe, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
