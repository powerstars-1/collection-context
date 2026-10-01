"""Original local media preparation, including limits and actual subprocess boundaries."""

import base64
import dataclasses
import hashlib
import io
import json
import math
import sys
import wave
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.media import (
    THUMB_BYTES,
    AudioSegment,
    FrameCandidate,
    FrameScanner,
    LocalMedia,
    MediaInfo,
    MediaPolicy,
    PngFrames,
    segment_ranges,
)


def fake_media(monkeypatch, **kwargs):
    monkeypatch.setattr(
        LocalMedia, "_decoder_versions", lambda self: {"ffmpeg": "test decoder", "ffprobe": "test probe"}
    )
    monkeypatch.setattr(LocalMedia, "_probe", lambda self: MediaInfo(5, 320, 240, True, 0, 1))
    return LocalMedia(
        b"original-fixture", ffmpeg=Path(sys.executable), ffprobe=Path(sys.executable), **kwargs
    )


def test_segment_coverage_and_explicit_overlap():
    policy = MediaPolicy(audio_segment_seconds=3, audio_overlap_seconds=0.5)
    assert segment_ranges(8, policy) == [(0.0, 3.0), (2.5, 5.5), (5.0, 8)]
    assert segment_ranges(0.2, policy) == [(0.0, 0.2)]
    assert segment_ranges(3, policy) == [(0.0, 3)]


@pytest.mark.parametrize("duration", [0, -1, math.inf, math.nan, True, "5"])
def test_invalid_duration_is_not_silently_accepted(duration):
    with pytest.raises(ContextError, match="时长"):
        segment_ranges(duration, MediaPolicy())


def test_audio_segment_limit_never_truncates():
    with pytest.raises(ContextError) as caught:
        segment_ranges(20, MediaPolicy(audio_segment_seconds=2, max_audio_segments=2))
    assert caught.value.code == "audio_segment_limit"


@pytest.mark.parametrize(
    "values",
    [
        {"sample_fps": True},
        {"max_selected_frames": 0},
        {"audio_segment_seconds": 0},
        {"audio_overlap_seconds": 240},
        {"max_duration_seconds": math.nan},
        {"audio_segment_seconds": 900},
        {"command_timeout_seconds": -1},
    ],
)
def test_policy_rejects_invalid_or_unbounded_limits(values):
    with pytest.raises(ContextError) as caught:
        MediaPolicy(**values)
    assert caught.value.code == "invalid_media_policy"


def test_streamed_scan_preserves_local_changes_even_at_bottom():
    base = bytes([200]) * THUMB_BYTES
    changed = bytearray(base)
    # Small localized page text at the bottom, not suppressed as a subtitle region.
    for y in range(77, 86):
        for x in range(10, 30):
            changed[y * 160 + x] = 0
    scanner = FrameScanner(MediaPolicy())
    data = base * 2 + bytes(changed) * 2 + base * 2
    for start in range(0, len(data), 11_137):
        scanner.feed(data[start : start + 11_137])
    selected = scanner.finish(3)
    assert [value.sample_index for value in selected] == [0, 2, 4, 5]
    assert "local_change" in selected[1].reasons
    assert selected[1].nominal_seconds == 1
    assert selected[-1].reasons == ("last",)


def test_static_video_has_first_anchor_and_last_not_every_sample():
    scanner = FrameScanner(MediaPolicy(anchor_seconds=2))
    scanner.feed(bytes([50]) * THUMB_BYTES * 10)
    assert [frame.sample_index for frame in scanner.finish(5)] == [0, 4, 8, 9]


def test_small_number_change_is_not_lost_in_average_threshold():
    baseline = bytes([220]) * THUMB_BYTES
    changed = bytearray(baseline)
    changed[37 * 160 + 23] = 212
    scanner = FrameScanner(MediaPolicy())
    scanner.feed(baseline + bytes(changed) + baseline)
    assert [frame.sample_index for frame in scanner.finish(1.5)] == [0, 1, 2]
    assert "fine_detail_change" in scanner.selected[1].reasons
    average, local, maximum = FrameScanner.changes(baseline, bytes(changed))
    assert average < 0.018 and local < 0.055 and maximum == 8


def test_change_scores_are_normalized_with_unequal_tile_widths():
    average, local, maximum = FrameScanner.changes(bytes([0]) * THUMB_BYTES, bytes([255]) * THUMB_BYTES)
    assert (average, local, maximum) == (1, 1, 255)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"streams": [1], "format": {"duration": 5}},
        {
            "streams": [
                {"codec_type": "video", "index": 0, "width": 100, "height": 100},
                {"codec_type": "audio", "index": None},
            ],
            "format": {"duration": 5},
        },
        {
            "streams": [{"codec_type": "video", "index": 0, "width": True, "height": 100}],
            "format": {"duration": 5},
        },
        {
            "streams": [{"codec_type": "video", "index": 0, "width": 100, "height": 100}],
            "format": {"duration": "NaN"},
        },
    ],
)
def test_invalid_probe_does_not_accept_malformed_streams(monkeypatch, payload):
    monkeypatch.setattr(
        LocalMedia, "_decoder_versions", lambda self: {"ffmpeg": "test decoder", "ffprobe": "test probe"}
    )
    monkeypatch.setattr(LocalMedia, "_run", lambda *args, **kwargs: json.dumps(payload).encode())
    with pytest.raises(ContextError) as caught:
        LocalMedia(b"synthetic", ffmpeg=Path(sys.executable), ffprobe=Path(sys.executable))
    assert caught.value.code == "invalid_media"


def test_decoder_version_changes_preparation_strategy(monkeypatch):
    with fake_media(monkeypatch) as first:
        old_hash = first.strategy_hash
    monkeypatch.setattr(
        LocalMedia, "_decoder_versions", lambda self: {"ffmpeg": "new test decoder", "ffprobe": "test probe"}
    )
    with LocalMedia(b"original-fixture", ffmpeg=Path(sys.executable), ffprobe=Path(sys.executable)) as second:
        assert second.strategy_hash != old_hash


def test_frame_and_sample_caps_block_instead_of_returning_truncated_success():
    scanner = FrameScanner(MediaPolicy(max_selected_frames=2))
    with pytest.raises(ContextError) as caught:
        scanner.feed(bytes([0]) * THUMB_BYTES + bytes([255]) * THUMB_BYTES + bytes([0]) * THUMB_BYTES)
    assert caught.value.code == "frame_limit"
    scanner = FrameScanner(MediaPolicy(max_sampled_frames=1))
    with pytest.raises(ContextError) as caught:
        scanner.feed(bytes([0]) * THUMB_BYTES * 2)
    assert caught.value.code == "sample_limit"


@pytest.mark.parametrize(
    "data,duration,code",
    [
        (b"", 1, "invalid_media_decode"),
        (b"a", 1, "invalid_media_decode"),
        (bytes([0]) * THUMB_BYTES, 20, "media_decode_incomplete"),
    ],
)
def test_incomplete_scan_is_explicit(data, duration, code):
    scanner = FrameScanner(MediaPolicy())
    scanner.feed(data)
    with pytest.raises(ContextError) as caught:
        scanner.finish(duration)
    assert caught.value.code == code


def test_source_snapshot_lifetime_and_hash(monkeypatch):
    with fake_media(monkeypatch) as media:
        root = media.root
        assert media.source_path.read_bytes() == b"original-fixture"
        assert media.source_sha256 == hashlib.sha256(b"original-fixture").hexdigest()
        assert media.source_path.stat().st_mode & 0o777 == 0o600
    assert not root.exists()
    with pytest.raises(ContextError) as caught:
        media._run(Path(sys.executable), ["-c", "print(1)"], max_bytes=10)
    assert caught.value.code == "media_session_closed"


def test_missing_dependencies_and_source_limits_are_explicit(tmp_path):
    with pytest.raises(ContextError) as caught:
        LocalMedia(b"x", ffmpeg=tmp_path / "not-installed", ffprobe=tmp_path / "not-installed")
    assert caught.value.code == "media_dependency_missing"
    with pytest.raises(ContextError) as caught:
        LocalMedia(b"", ffmpeg=Path(sys.executable), ffprobe=Path(sys.executable))
    assert caught.value.code == "media_input_limit"


def test_probe_and_decoder_subprocess_failures_are_sanitized(monkeypatch):
    with fake_media(monkeypatch) as media:
        with pytest.raises(ContextError) as caught:
            media._run(
                Path(sys.executable),
                ["-c", "import sys; print('private-key', file=sys.stderr); sys.exit(4)"],
                max_bytes=100,
            )
        assert caught.value.code == "media_decode_failed"
        assert "private-key" not in str(caught.value)


def test_real_subprocess_has_bounded_output_no_model_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-secret")
    monkeypatch.setenv("FFREPORT", "file=unexpected-report.log")
    with fake_media(monkeypatch) as media:
        value = media._run(
            Path(sys.executable),
            ["-c", "import os,json; print(json.dumps(dict(os.environ)))"],
            max_bytes=10_000,
        )
        environment = json.loads(value)
        assert "DASHSCOPE_API_KEY" not in environment
        assert "FFREPORT" not in environment
        with pytest.raises(ContextError) as caught:
            media._run(
                Path(sys.executable),
                ["-c", "import sys; sys.stdout.buffer.write(b'x'*1000000)"],
                max_bytes=1000,
            )
        assert caught.value.code == "media_output_limit"


def test_real_subprocess_timeout_stops_own_process(monkeypatch):
    with fake_media(monkeypatch, policy=MediaPolicy(command_timeout_seconds=0.1)) as media:
        with pytest.raises(ContextError) as caught:
            media._run(Path(sys.executable), ["-c", "import time; time.sleep(10)"], max_bytes=100)
        assert caught.value.code == "media_timeout"


def test_audio_is_wav_with_explicit_ranges_and_no_word_timestamps(monkeypatch):
    with fake_media(
        monkeypatch, policy=MediaPolicy(audio_segment_seconds=3, audio_overlap_seconds=0.5)
    ) as media:
        commands = []

        def execute(executable, arguments, **kwargs):
            commands.append(arguments)
            duration = float(arguments[arguments.index("-t") + 1])
            return b"\0\0" * round(duration * 16000)

        monkeypatch.setattr(media, "_run", execute)
        segments = list(media.audio_segments())
        assert all(isinstance(segment, AudioSegment) for segment in segments)
        assert [(segment.start_seconds, segment.end_seconds) for segment in segments] == [(0, 3), (2.5, 5)]
        assert segments[1].overlaps_previous
        with wave.open(io.BytesIO(segments[0].data)) as wav:
            assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getnframes()) == (
                1,
                16000,
                2,
                48000,
            )
        assert "-protocol_whitelist" in commands[0]
        assert "file,pipe" in commands[0]
        assert "-enable_drefs" in commands[0]


def test_no_audio_returns_no_segments_not_empty_successful_transcript(monkeypatch):
    with fake_media(monkeypatch) as media:
        media.info = dataclasses.replace(media.info, has_audio=False, audio_stream=None)
        monkeypatch.setattr(media, "_run", lambda *args, **kwargs: pytest.fail("No decoder call expected"))
        assert list(media.audio_segments()) == []


def test_short_audio_and_forged_frame_are_not_saved(monkeypatch):
    with fake_media(monkeypatch) as media:
        monkeypatch.setattr(media, "_run", lambda *args, **kwargs: b"\0\0")
        with pytest.raises(ContextError) as caught:
            list(media.audio_segments())
        assert caught.value.code == "audio_decode_incomplete"
        for candidate in [FrameCandidate("f_999999", 0, 0, (), 0), FrameCandidate("f_000000", 0, 4, (), 0)]:
            with pytest.raises(ContextError) as caught:
                media.frame(candidate)
            assert caught.value.code == "invalid_frame_reference"


def test_scan_report_never_claims_ocr_or_exact_timestamps(monkeypatch):
    with fake_media(monkeypatch) as media:

        def execute(executable, arguments, *, consumer, **kwargs):
            consumer(bytes([50]) * THUMB_BYTES * 10)
            return b""

        monkeypatch.setattr(media, "_run", execute)
        candidates, coverage = media.scan_frames()
        assert len(candidates) == 2
        assert coverage["complete"] is False
        assert coverage["ocr_state"] == "not_applied"
        assert coverage["exact_presentation_timestamps"] is False
        assert coverage["critical_page_recall"] is None


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1kAAAAASUVORK5CYII="
)


def test_png_frames_deliver_matching_candidate_order_in_bounded_chunks():
    candidates = [FrameCandidate(f"f_{index:06}", index, index / 2, (), 0) for index in (0, 2)]
    output = []
    parser = PngFrames(candidates, 1000, output.append)
    raw = PNG * 2
    for offset in range(0, len(raw), 3):
        parser.feed(raw[offset : offset + 3])
    parser.finish()
    assert [frame.candidate.sample_index for frame in output] == [0, 2]
    assert all(frame.data == PNG and frame.sha256 == hashlib.sha256(PNG).hexdigest() for frame in output)


def test_png_stream_rejects_incomplete_and_oversized_frames():
    candidate = FrameCandidate("f_000000", 0, 0, (), 0)
    parser = PngFrames([candidate], 1000, lambda frame: None)
    parser.feed(PNG[:-8])
    with pytest.raises(ContextError) as caught:
        parser.finish()
    assert caught.value.code == "media_decode_incomplete"
    parser = PngFrames([candidate], 1000, lambda frame: None)
    with pytest.raises(ContextError) as caught:
        parser.feed(b"\x89PNG\r\n\x1a\n" + (2000).to_bytes(4, "big") + b"IDAT")
    assert caught.value.code == "media_output_limit"


def test_evidence_replays_scan_filter_and_index_instead_of_independent_seek(monkeypatch):
    with fake_media(monkeypatch) as media:
        commands = []

        def execute(executable, arguments, *, consumer, **kwargs):
            commands.append(arguments)
            consumer(PNG)
            return b""

        monkeypatch.setattr(media, "_run", execute)
        result = media.frame(FrameCandidate("f_000003", 3, 1.5, (), 0))
        assert result.data == PNG
        assert "-ss" not in commands[0]
        filter_string = commands[0][commands[0].index("-vf") + 1]
        assert media._sample_filter() in filter_string
        assert "eq(n,3)" in filter_string
        assert "vfr" in commands[0]
