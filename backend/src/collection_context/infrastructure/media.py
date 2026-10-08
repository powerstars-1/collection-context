"""Own bounded, local-only media preparation. FFmpeg is a decoder, not an external workflow."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
import zlib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from collection_context.application.contracts import ContextError, digest
from collection_context.infrastructure.download_limits import MAX_SOURCE_BYTES

THUMB_WIDTH, THUMB_HEIGHT = 160, 90
THUMB_BYTES = THUMB_WIDTH * THUMB_HEIGHT
PROCESSOR_VERSION = "local_media_v5"
FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm"
MAX_RASTER_PIXELS = 33_177_600
MAX_RASTER_BYTES = 32_000_000


@dataclass(frozen=True)
class RasterInfo:
    mime_type: str
    width: int
    height: int
    pixel_sha256: str | None = None


def _png_integrity(data: bytes) -> None:
    """Pillow does not verify the final IEND checksum or reject trailing chunks."""
    offset, first, seen_data = 8, True, False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + length + 12
        if end > len(data):
            break
        kind = data[offset + 4 : offset + 8]
        if first and (kind != b"IHDR" or length != 13):
            break
        if not first and kind == b"IHDR":
            break
        if zlib.crc32(data[offset + 4 : end - 4]) != int.from_bytes(data[end - 4 : end], "big"):
            break
        if kind == b"IEND":
            if length == 0 and end == len(data) and seen_data:
                return
            break
        seen_data = seen_data or kind == b"IDAT"
        offset, first = end, False
    raise ValueError("incomplete PNG")


def validate_raster(
    data: bytes,
    *,
    expected_mime: str | None = None,
    max_bytes: int = MAX_RASTER_BYTES,
    max_pixels: int = MAX_RASTER_PIXELS,
    pixel_hash: bool = False,
) -> RasterInfo:
    """Fully decode one bounded static raster, without OCR/model calls or global decoder changes.

    Imported only on an actual media operation: ordinary text/library reads do not
    require Pillow. Reject animations rather than treating only their first page as
    the complete original or using it to deduplicate different moving images.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= max_bytes:
        raise ContextError("media_input_limit", "图片为空或超过本次读取上限，未截断图片。")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, format_name = "image/png", "PNG"
    elif data.startswith(b"\xff\xd8\xff"):
        mime, format_name = "image/jpeg", "JPEG"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime, format_name = "image/webp", "WEBP"
    else:
        raise ContextError("unsupported_media", "画面不是已支持的PNG、JPEG或WebP栅格图片。")
    if expected_mime is not None and mime != expected_mime:
        raise ContextError("unsupported_media", "图片实际格式与登记类型不一致。")
    try:
        from PIL import Image
    except ImportError:
        raise ContextError("media_dependency_missing", "图片完整性检查依赖尚未就绪，未跳过检查。") from None
    try:
        if format_name == "PNG":
            _png_integrity(data)
        elif format_name == "JPEG" and not data.endswith(b"\xff\xd9"):
            raise ValueError("incomplete JPEG")
        elif format_name == "WEBP" and int.from_bytes(data[4:8], "little") + 8 != len(data):
            raise ValueError("incomplete WebP")
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise ContextError("media_pixel_limit", "图片像素超过上限，未缩小原图后当作完整内容。")
            if image.format != format_name or getattr(image, "is_animated", False):
                raise ContextError("unsupported_media", "图片格式不一致或包含动画，未只取首帧当完整原图。")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            pixels = (
                hashlib.sha256(str(image.size).encode() + image.convert("RGBA").tobytes()).hexdigest()
                if pixel_hash
                else None
            )
        return RasterInfo(mime, width, height, pixels)
    except ContextError:
        raise
    except Exception:
        raise ContextError("media_image_invalid", "图片内容损坏或未完整解码，未作为有效画面使用。") from None


@dataclass(frozen=True)
class MediaPolicy:
    adaptive_selection: bool = False
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_duration_seconds: float = 7200
    max_pixels: int = 8_294_400
    sample_fps: int = 2
    max_sampled_frames: int = 14_402
    max_selected_frames: int = 240
    max_ocr_frames: int = 480
    anchor_seconds: float = 30
    audio_segment_seconds: float = 240
    audio_overlap_seconds: float = 1
    max_audio_segments: int = 64
    max_audio_bytes: int = 16_000_000
    max_frame_bytes: int = 32_000_000
    max_frame_edge: int = 2560
    fine_change_threshold: int = 4
    command_timeout_seconds: float = 120

    def __post_init__(self) -> None:
        if type(self.adaptive_selection) is not bool:
            raise ContextError("invalid_media_policy", "自适应选帧开关无效。")
        integer_bounds = {
            "max_source_bytes": (1, MAX_SOURCE_BYTES),
            "max_pixels": (1, 33_177_600),
            "sample_fps": (1, 10),
            "max_sampled_frames": (1, 144_002),
            "max_selected_frames": (1, 2400),
            "max_ocr_frames": (1, 2400),
            "max_audio_segments": (1, 1000),
            "max_audio_bytes": (1000, 32_000_000),
            "max_frame_bytes": (1000, 32_000_000),
            "max_frame_edge": (128, 4096),
            "fine_change_threshold": (1, 255),
        }
        for name, (low, high) in integer_bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ContextError("invalid_media_policy", "媒体处理策略包含无效上限。")
        for name in (
            "max_duration_seconds",
            "anchor_seconds",
            "audio_segment_seconds",
            "audio_overlap_seconds",
            "command_timeout_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ContextError("invalid_media_policy", "媒体处理策略包含无效时间。")
        if not (
            0 < self.max_duration_seconds <= 72_000
            and 0 < self.anchor_seconds <= 300
            and 0 <= self.audio_overlap_seconds < self.audio_segment_seconds <= 900
            and 0 < self.command_timeout_seconds <= 600
        ):
            raise ContextError("invalid_media_policy", "媒体处理时间或重叠范围无效。")
        if self.audio_segment_seconds * 32_000 + 44 > self.max_audio_bytes:
            raise ContextError("invalid_media_policy", "音频分段长度超过配置的文件大小上限。")


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    video_stream: int
    audio_stream: int | None


@dataclass(frozen=True)
class AudioSegment:
    evidence_id: str
    start_seconds: float
    end_seconds: float
    decoded_seconds: float
    data: bytes
    sha256: str
    overlaps_previous: bool


@dataclass(frozen=True)
class FrameCandidate:
    evidence_id: str
    sample_index: int
    nominal_seconds: float
    reasons: tuple[str, ...]
    change_score: float


@dataclass(frozen=True)
class PreparedFrame:
    candidate: FrameCandidate
    data: bytes
    sha256: str
    mime_type: str = "image/png"
    page_index: int | None = None


class PngFrames:
    """Bounded parser for a decoder's PNG stream; frame delivery never uses a second seek rule."""

    def __init__(
        self,
        candidates: list[FrameCandidate],
        max_bytes: int,
        consumer: Callable[[PreparedFrame], None],
        *,
        max_pixels: int = MAX_RASTER_PIXELS,
    ):
        self.candidates, self.max_bytes, self.consumer = candidates, max_bytes, consumer
        self.max_pixels = max_pixels
        self.buffer = bytearray()
        self.offset = 0
        self.frame_count = 0

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while True:
            if self.offset == 0:
                if len(self.buffer) < 8:
                    break
                if self.buffer[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ContextError("media_decode_failed", "解码器输出不是有效 PNG 帧流。")
                self.offset = 8
            if len(self.buffer) < self.offset + 8:
                break
            length = int.from_bytes(self.buffer[self.offset : self.offset + 4], "big")
            end = self.offset + length + 12
            if end > self.max_bytes:
                raise ContextError("media_output_limit", "单张候选帧超过大小上限。")
            if len(self.buffer) < end:
                break
            kind = self.buffer[self.offset + 4 : self.offset + 8]
            self.offset = end
            if kind == b"IEND":
                if length != 0 or self.frame_count >= len(self.candidates):
                    raise ContextError("media_decode_failed", "PNG 帧结束或数量与候选清单不符。")
                frame = bytes(self.buffer[:end])
                validate_raster(
                    frame, expected_mime="image/png", max_bytes=self.max_bytes, max_pixels=self.max_pixels
                )
                self.consumer(
                    PreparedFrame(self.candidates[self.frame_count], frame, hashlib.sha256(frame).hexdigest())
                )
                self.frame_count += 1
                del self.buffer[:end]
                self.offset = 0

    def finish(self) -> None:
        if self.buffer or self.frame_count != len(self.candidates):
            raise ContextError("media_decode_incomplete", "候选原图未完整解码；未作为完整画面输入。")


def segment_ranges(duration: float, policy: MediaPolicy) -> list[tuple[float, float]]:
    if type(duration) not in {int, float} or not math.isfinite(duration) or duration <= 0:
        raise ContextError("invalid_media", "音频时长无效。")
    result = []
    start = 0.0
    while start < duration:
        end = min(duration, start + policy.audio_segment_seconds)
        result.append((start, end))
        if len(result) > policy.max_audio_segments:
            raise ContextError("audio_segment_limit", "分段数量超过上限，未静默截断音频。")
        if end >= duration:
            break
        start = end - policy.audio_overlap_seconds
    return result


class FrameScanner:
    """Streaming visual-change candidates; no OCR claim and no suppressed subtitle region."""

    def __init__(self, policy: MediaPolicy):
        self.policy = policy
        self.buffer = bytearray()
        self.sample_count = 0
        self.selected: list[FrameCandidate] = []
        self.reference: bytes | None = None
        self.last: bytes | None = None
        self.candidate_count = 0
        self.compactions = 0

    @staticmethod
    def changes(first: bytes, second: bytes) -> tuple[float, float, int]:
        if first == second:
            return 0, 0, 0
        # Local tile change catches small text changes lost in a whole-image average.
        sums = [0] * 36
        counts = [0] * 36
        total = 0
        maximum = 0
        for index, (left, right) in enumerate(zip(first, second)):
            difference = abs(left - right)
            total += difference
            maximum = max(maximum, difference)
            row, column = divmod(index, THUMB_WIDTH)
            tile = (row // 15) * 6 + min(5, column * 6 // THUMB_WIDTH)
            sums[tile] += difference
            counts[tile] += 1
        return total / (THUMB_BYTES * 255), max(s / (n * 255) for s, n in zip(sums, counts)), maximum

    def _select(self, index: int, reasons: tuple[str, ...], score: float, frame: bytes) -> None:
        self.candidate_count += 1
        if len(self.selected) >= self.policy.max_selected_frames:
            if not self.policy.adaptive_selection:
                raise ContextError("frame_limit", "画面候选超过上限；请分段或显式调整策略，未删帧后标为完整。")
            # Compact adjacent time ranges, not just the video's head. Keep a
            # representative in each range and reserve room for later content.
            if len(self.selected) > 2:
                middle = self.selected[1:-1]
                self.selected = [self.selected[0], *[
                    max(middle[i:i + 2], key=lambda c: c.change_score)
                    for i in range(0, len(middle), 2)
                ], self.selected[-1]]
            else:
                self.selected = self.selected[:1] if self.policy.max_selected_frames > 1 else []
            self.selected = self.selected[:max(0, self.policy.max_selected_frames - 1)]
            self.compactions += 1
        self.selected.append(
            FrameCandidate("f_" + str(index).zfill(6), index, index / self.policy.sample_fps, reasons, score)
        )
        self.reference = frame

    def feed(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while len(self.buffer) >= THUMB_BYTES:
            frame = bytes(self.buffer[:THUMB_BYTES])
            del self.buffer[:THUMB_BYTES]
            index = self.sample_count
            self.sample_count += 1
            if self.sample_count > self.policy.max_sampled_frames:
                raise ContextError("sample_limit", "候选采样超过上限，未静默结束扫描。")
            if self.reference is None:
                self._select(index, ("first",), 0, frame)
            else:
                average, local, maximum = self.changes(self.reference, frame)
                reasons = []
                if average >= 0.018:
                    reasons.append("visual_change")
                if local >= 0.055:
                    reasons.append("local_change")
                fine_changed = maximum >= self.policy.fine_change_threshold
                if self.policy.adaptive_selection:
                    # Compression noise or a lone changed pixel is not a new page.
                    fine_changed = maximum >= 32 and sum(
                        abs(a - b) >= 16 for a, b in zip(self.reference, frame)
                    ) / THUMB_BYTES >= 0.004
                if fine_changed:
                    reasons.append("fine_detail_change")
                if (
                    index - self.selected[-1].sample_index
                ) / self.policy.sample_fps >= self.policy.anchor_seconds:
                    reasons.append("coverage_anchor")
                if self.policy.adaptive_selection and reasons:
                    elapsed = (index - self.selected[-1].sample_index) / self.policy.sample_fps
                    # Full-frame motion is sampled at 1 Hz; local changes can
                    # retain short-lived UI pages at up to 5 Hz.
                    local_page = local >= 0.10 and average < 0.04
                    if elapsed < (0.2 if local_page else 1.0):
                        reasons = []
                if reasons:
                    self._select(index, tuple(reasons), max(average, local, maximum / 255), frame)
            self.last = frame

    def finish(self, duration: float) -> list[FrameCandidate]:
        if self.buffer or not self.sample_count or self.last is None:
            raise ContextError("invalid_media_decode", "画面扫描为空或帧不完整。")
        if self.sample_count < max(1, math.floor(duration * self.policy.sample_fps) - 1):
            raise ContextError("media_decode_incomplete", "解码提前结束；未将未覆盖的尾部标为成功。")
        if self.selected[-1].sample_index != self.sample_count - 1:
            self._select(self.sample_count - 1, ("last",), 0, self.last)
        return self.selected


class LocalMedia:
    """Source bytes are an explicit snapshot. Lifetime is scoped and no library is mutated."""

    def __init__(
        self,
        source: bytes,
        *,
        policy: MediaPolicy | None = None,
        ffmpeg: Path | None = None,
        ffprobe: Path | None = None,
    ):
        self.policy = policy or MediaPolicy()
        if not isinstance(source, bytes) or not source or len(source) > self.policy.max_source_bytes:
            raise ContextError("media_input_limit", "视频为空或超过本地准备阶段大小上限。")
        self.ffmpeg = self._binary(ffmpeg, "ffmpeg")
        self.ffprobe = self._binary(ffprobe, "ffprobe")
        self.source_sha256 = hashlib.sha256(source).hexdigest()
        self.temporary = tempfile.TemporaryDirectory(prefix="collection-media-")
        self.root = Path(self.temporary.name)
        self.closed = False
        try:
            self.source_path = self.root / "input.media"
            with self.source_path.open("xb") as stream:
                os.chmod(self.source_path, 0o600)
                stream.write(source)
            self.decoder_versions = self._decoder_versions()
            self.strategy_hash = digest(
                {
                    "processor": PROCESSOR_VERSION,
                    "policy": asdict(self.policy),
                    "decoders": self.decoder_versions,
                }
            )
            self.info = self._probe()
            if (
                math.ceil(self.info.duration_seconds * self.policy.sample_fps) + 1
                > self.policy.max_sampled_frames
            ):
                raise ContextError("sample_limit", "视频长度与采样率超过候选数量上限。")
        except OSError:
            self.close()
            raise ContextError(
                "media_storage_unavailable", "无法准备临时媒体快照，请检查空间和权限。"
            ) from None
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _binary(configured: Path | None, name: str) -> Path:
        path = configured if configured is not None else shutil.which(name)
        try:
            if path is None:
                raise OSError
            result = Path(path).resolve(strict=True)
            if not result.is_file() or not os.access(result, os.X_OK):
                raise OSError
            return result
        except OSError:
            raise ContextError(
                "media_dependency_missing", "缺少可执行的 FFmpeg/FFprobe；不自动下载。"
            ) from None

    def close(self) -> None:
        if not self.closed:
            self.temporary.cleanup()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _run(
        self,
        executable: Path,
        arguments: list[str],
        *,
        max_bytes: int,
        consumer: Callable[[bytes], None] | None = None,
    ) -> bytes:
        if self.closed:
            raise ContextError("media_session_closed", "媒体准备会话已结束。")
        # Exclude model/platform credentials and FFREPORT from decoder subprocess environments.
        environment = {"PATH": str(executable.parent), "LANG": "C", "LC_ALL": "C"}
        if os.name == "nt" and "SystemRoot" in os.environ:
            environment["SystemRoot"] = os.environ["SystemRoot"]
        try:
            process = subprocess.Popen(
                [str(executable), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
                env=environment,
                shell=False,
            )
        except OSError:
            raise ContextError("media_dependency_failed", "无法启动媒体解码器。") from None
        assert process.stdout is not None
        stdout = process.stdout
        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        stop = threading.Event()

        def read_output() -> None:
            try:
                while not stop.is_set():
                    chunk = stdout.read(65_536)
                    while not stop.is_set():
                        try:
                            chunks.put(chunk or None, timeout=0.1)
                            break
                        except queue.Full:
                            pass
                    if not chunk:
                        break
            finally:
                stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + self.policy.command_timeout_seconds
        # Local consumers (notably OCR) backpressure the decoder intentionally.
        # Do not count that work as a hung decoder. Still bound the complete pass.
        total_deadline = time.monotonic() + max(self.policy.command_timeout_seconds, 600 if consumer else 0)
        output = bytearray()
        total = 0
        try:
            while True:
                if time.monotonic() >= min(deadline, total_deadline):
                    raise ContextError("media_timeout", "本地解码超时；没有发出云请求。", retryable=True)
                try:
                    chunk = chunks.get(timeout=0.1)
                except queue.Empty:
                    if not reader.is_alive():
                        raise ContextError("media_decode_failed", "解码输出中断。")
                    continue
                if chunk is None:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ContextError("media_output_limit", "本地解码输出超过上限，未作为完整结果保存。")
                if consumer is not None:
                    consumption_started = time.monotonic()
                    consumer(chunk)
                    deadline = min(total_deadline, deadline + time.monotonic() - consumption_started)
                else:
                    output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.01, min(deadline, total_deadline) - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ContextError("media_timeout", "解码进程未及时结束。", retryable=True) from None
            if code:
                raise ContextError("media_decode_failed", "媒体格式无效或解码失败；未回显原始解码日志。")
            return bytes(output)
        finally:
            stop.set()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            reader.join(timeout=2)

    def _input_options(self) -> list[str]:
        return [
            "-v",
            "error",
            "-max_alloc",
            "67108864",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            FORMATS,
            "-enable_drefs",
            "0",
            "-use_absolute_path",
            "0",
        ]

    def _decoder_versions(self) -> dict[str, str]:
        result = {}
        for name, executable in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)):
            text = self._run(executable, ["-version"], max_bytes=65_536).decode("utf-8", errors="replace")
            first_line = text.splitlines()[0] if text else ""
            if not first_line.startswith(name + " version ") or len(first_line) > 300:
                raise ContextError("media_dependency_failed", "可执行文件版本与所需媒体依赖不符。")
            result[name] = first_line
        return result

    def _probe(self) -> MediaInfo:
        result = self._run(
            self.ffprobe,
            [
                *self._input_options(),
                "-show_entries",
                "format=duration:stream=index,codec_type,width,height,duration",
                "-of",
                "json",
                str(self.source_path),
            ],
            max_bytes=1_000_000,
        )
        try:
            payload = json.loads(result)
            streams = payload["streams"]
            if (
                not isinstance(streams, list)
                or len(streams) > 16
                or any(not isinstance(stream, dict) for stream in streams)
            ):
                raise ValueError
            video = next(s for s in streams if s.get("codec_type") == "video")
            audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
            duration = float(payload["format"]["duration"])
            width, height, video_index = video["width"], video["height"], video["index"]
            audio_index = audio["index"] if audio is not None else None
            if (
                not math.isfinite(duration)
                or duration <= 0
                or duration > self.policy.max_duration_seconds
                or type(width) is not int
                or type(height) is not int
                or min(width, height) <= 0
                or width * height > self.policy.max_pixels
                or type(video_index) is not int
                or not 0 <= video_index <= 15
                or (audio is not None and (type(audio_index) is not int or not 0 <= audio_index <= 15))
            ):
                raise ValueError
            return MediaInfo(duration, width, height, audio is not None, video_index, audio_index)
        except (KeyError, TypeError, ValueError, OverflowError, StopIteration):
            raise ContextError("invalid_media", "视频探测不完整或尺寸/时长超过策略上限。") from None

    def _decode_options(self) -> list[str]:
        return [*self._input_options(), "-nostdin", "-threads", "1", "-hwaccel", "none"]

    def _sample_filter(self) -> str:
        # The same sample-index sequence is used by scanning and evidence decoding.
        return f"fps=fps={self.policy.sample_fps}:start_time=0:round=up"

    def audio_segments(self) -> Iterator[AudioSegment]:
        if not self.info.has_audio:
            return
        ranges = segment_ranges(self.info.duration_seconds, self.policy)
        for index, (start, end) in enumerate(ranges):
            raw = self._run(
                self.ffmpeg,
                [
                    *self._decode_options(),
                    "-ss",
                    f"{start:.6f}",
                    "-i",
                    str(self.source_path),
                    "-t",
                    f"{end - start:.6f}",
                    "-map",
                    f"0:{self.info.audio_stream}",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                max_bytes=self.policy.max_audio_bytes - 44,
            )
            actual = len(raw) / 32_000
            if not raw or len(raw) % 2 or actual < (end - start) - 0.1:
                raise ContextError("audio_decode_incomplete", "音轨为空或覆盖不足，未标为完整转写输入。")
            target = io.BytesIO()
            with wave.open(target, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16_000)
                wav.writeframes(raw)
            data = target.getvalue()
            yield AudioSegment(
                "a_" + str(index).zfill(6),
                start,
                end,
                actual,
                data,
                hashlib.sha256(data).hexdigest(),
                index > 0,
            )

    def scan_frames(self) -> tuple[list[FrameCandidate], dict]:
        scanner = FrameScanner(self.policy)
        self._run(
            self.ffmpeg,
            [
                *self._decode_options(),
                "-i",
                str(self.source_path),
                "-map",
                f"0:{self.info.video_stream}",
                "-an",
                "-sn",
                "-dn",
                "-filter_threads",
                "1",
                "-vf",
                f"{self._sample_filter()},scale={THUMB_WIDTH}:{THUMB_HEIGHT},format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            max_bytes=self.policy.max_sampled_frames * THUMB_BYTES,
            consumer=scanner.feed,
        )
        candidates = scanner.finish(self.info.duration_seconds)
        return candidates, {
            "sampled_frames": scanner.sample_count,
            "selected_frames": len(candidates),
            "candidate_events": scanner.candidate_count,
            "candidate_compactions": scanner.compactions,
            "selection_policy": "adaptive_timeline_v1" if self.policy.adaptive_selection else "strict_changes",
            "sample_fps": self.policy.sample_fps,
            "sample_interval_seconds": 1 / self.policy.sample_fps,
            "sample_rounding": "up",
            "duration_seconds": self.info.duration_seconds,
            "scan_state": "ready",
            "time_basis": "nominal_sample_grid",
            "exact_presentation_timestamps": False,
            "ocr_state": "not_applied",
            "critical_page_recall": None,
            "complete": False,
            "gaps": [
                "Sub-sample flashes and details lost when downscaling can be missed.",
                "Candidate budget reduction is recorded explicitly; selected frames are not exhaustive coverage.",
            ],
            "strategy_hash": self.strategy_hash,
        }

    def _validate_candidate(self, candidate: FrameCandidate) -> None:
        if not isinstance(candidate, FrameCandidate) or not (
            type(candidate.sample_index) is int
            and 0 <= candidate.sample_index < self.policy.max_sampled_frames
            and candidate.evidence_id == "f_" + str(candidate.sample_index).zfill(6)
            and candidate.nominal_seconds == candidate.sample_index / self.policy.sample_fps
            and 0 <= candidate.nominal_seconds < self.info.duration_seconds
        ):
            raise ContextError("invalid_frame_reference", "候选帧身份或时间无效。")

    def frames(self, candidates: list[FrameCandidate], consumer: Callable[[PreparedFrame], None]) -> None:
        """One decoder pass, local consumer only. Cloud dispatch belongs to the durable executor."""
        if (
            not isinstance(candidates, list)
            or not candidates
            or len(candidates) > self.policy.max_selected_frames
        ):
            raise ContextError("frame_limit", "候选原图数量为空或超过上限。")
        for candidate in candidates:
            self._validate_candidate(candidate)
        if [candidate.sample_index for candidate in candidates] != sorted(
            {candidate.sample_index for candidate in candidates}
        ):
            raise ContextError("invalid_frame_reference", "候选帧必须按顺序排列且不能重复。")
        edge = self.policy.max_frame_edge
        # FFmpeg's expression parser has a recursion ceiling. A linear sum of
        # hundreds of candidates fails before yielding any frame; balance it.
        terms = [f"eq(n,{candidate.sample_index})" for candidate in candidates]
        while len(terms) > 1:
            terms = ["(" + "+".join(terms[i:i + 2]) + ")" for i in range(0, len(terms), 2)]
        select = terms[0]
        parser = PngFrames(
            candidates, self.policy.max_frame_bytes, consumer, max_pixels=self.policy.max_pixels
        )
        self._run(
            self.ffmpeg,
            [
                *self._decode_options(),
                "-i",
                str(self.source_path),
                "-map",
                f"0:{self.info.video_stream}",
                "-an",
                "-sn",
                "-dn",
                "-filter_threads",
                "1",
                "-vf",
                f"{self._sample_filter()},select='{select}',scale=w='min(iw,{edge})':h='min(ih,{edge})':force_original_aspect_ratio=decrease",
                "-fps_mode",
                "vfr",
                "-frames:v",
                str(len(candidates)),
                "-c:v",
                "png",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            max_bytes=self.policy.max_frame_bytes * len(candidates),
            consumer=parser.feed,
        )
        parser.finish()

    def frame(self, candidate: FrameCandidate) -> PreparedFrame:
        result: list[PreparedFrame] = []
        self.frames([candidate], result.append)
        return result[0]
