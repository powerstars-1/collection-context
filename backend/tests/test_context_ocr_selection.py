"""Original pixels and injected OCR: selection semantics, not engine-quality claims."""

import hashlib
import io
from dataclasses import replace

import pytest
from PIL import Image

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.media import FrameCandidate, MediaPolicy, PreparedFrame
from collection_context.infrastructure.ocr import OcrLine, OcrResult
from collection_context.processing.ocr_selection import OcrFrameSelection


def png(color=(40, 50, 60, 255), *, size=(40, 30), compression=6):
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG", compress_level=compression)
    return out.getvalue()


def page(index, *, data=None, reasons=("fine_change",), seconds=None):
    data = data if data is not None else png()
    return PreparedFrame(
        FrameCandidate(f"f_{index:06}", index, index if seconds is None else seconds, reasons, 4),
        data,
        hashlib.sha256(data).hexdigest(),
    )


def result(text="原创 React 390×844", state="ready"):
    lines = [OcrLine(text, 0.91, [[1, 2], [35, 2], [35, 20], [1, 20]])] if state != "no_text" else []
    return OcrResult(lines, 0.01, state, "original-injected")


class Engine:
    engine_version = "original-injected"
    model_hashes = {"Det": "original-fixture"}

    def __init__(self, outputs=None):
        self.outputs = iter(outputs or [])
        self.calls = []

    def recognize(self, data):
        self.calls.append(data)
        value = next(self.outputs, result())
        if isinstance(value, Exception):
            raise value
        return value


def select(*, outputs=None, policy=None, max_bytes=1_000_000):
    return OcrFrameSelection(Engine(outputs), policy or MediaPolicy(), max_bytes=max_bytes)


def test_equal_decoded_pixels_with_different_png_encoding_reuse_ocr_and_keep_reference():
    value = select()
    first, second = png(compression=0), png(compression=9)
    assert first != second
    value.feed(page(0, data=first, reasons=("first",)))
    value.feed(page(1, data=second))
    value.feed(page(2, data=second))
    assert len(value.frames) == 1 and len(value.engine.calls) == 1
    coverage = value.coverage()
    assert coverage["ocr_calls"] == 1 and coverage["ocr_duplicate_frames"] == 2
    assert coverage["ocr_records"][2]["duplicate_of"] == "f_000000"
    assert coverage["ocr_records"][1]["image_sha256"] != coverage["ocr_records"][0]["image_sha256"]
    assert coverage["ocr_records"][1]["ocr_reused"] is True
    assert coverage["complete"] is False


@pytest.mark.parametrize("reason", ["first", "last", "coverage_anchor"])
def test_explicit_anchors_never_removed(reason):
    value = select()
    value.feed(page(0))
    value.feed(page(1, reasons=(reason,)))
    assert len(value.frames) == 2 and len(value.engine.calls) == 1


def test_time_anchor_and_last_reference_are_preserved():
    value = select(policy=MediaPolicy(anchor_seconds=10))
    for index, seconds in enumerate((0, 1, 10, 11)):
        value.feed(page(index, seconds=seconds))
    assert [p.candidate.evidence_id for p in value.frames] == ["f_000000", "f_000002"]
    assert value.records[3]["duplicate_of"] == "f_000002"


@pytest.mark.parametrize(
    "changed",
    [png(color=(41, 50, 60, 255)), png(color=(40, 50, 60, 128)), png(size=(20, 60))],
)
def test_equal_ocr_text_does_not_remove_changed_color_alpha_or_dimensions(changed):
    value = select(outputs=[result(), result()])
    value.feed(page(0))
    value.feed(page(1, data=changed))
    assert len(value.frames) == 2 and len(value.engine.calls) == 2
    assert value.records[0]["pixel_sha256"] != value.records[1]["pixel_sha256"]


def test_same_words_but_changed_diagram_pixels_are_retained():
    image = Image.open(io.BytesIO(png())).copy()
    image.putpixel((20, 20), (255, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    value = select(outputs=[result(), result()])
    value.feed(page(0))
    value.feed(page(1, data=out.getvalue()))
    assert len(value.frames) == 2


def test_valid_no_text_is_distinct_from_failure_and_can_reuse():
    value = select(outputs=[result(state="no_text")])
    value.feed(page(0))
    value.feed(page(1))
    assert value.records[0]["state"] == "no_text" and value.records[0]["lines"] == []
    assert len(value.frames) == 1 and value.coverage()["ocr_state"] == "ready"


def test_failure_keeps_original_and_retries_only_local_ocr_on_next_candidate():
    value = select(outputs=[ContextError("ocr_failed", "original failure"), result()])
    value.feed(page(0))
    value.feed(page(1))
    assert len(value.frames) == 2 and len(value.engine.calls) == 2
    assert value.frames[0].candidate.reasons[-1] == "ocr_error"
    assert value.coverage()["ocr_state"] == "partial" and value.coverage()["ocr_failures"] == ["f_000000"]
    assert value.records[0]["state"] == "failed" and value.records[0]["error_code"] == "ocr_failed"
    assert value.records[1]["ocr_reused"] is False


@pytest.mark.parametrize("bad", [result(state="failed"), result("x" * 20_001), result("x\n" * 10_001)])
def test_invalid_or_oversized_result_retains_frame_with_failure(bad):
    value = select(outputs=[bad])
    value.feed(page(0))
    assert len(value.frames) == 1 and value.records[0]["error_code"] == "ocr_result_limit"


def test_too_many_lines_are_not_truncated_into_success():
    value = select(outputs=[replace(result(), lines=result().lines * 501)])
    value.feed(page(0))
    assert value.records[0]["state"] == "failed" and value.records[0]["lines"] == []


def test_text_boxes_confidence_and_nominal_time_are_recorded_without_inference():
    value = select()
    value.feed(page(0, seconds=20.5))
    line = value.coverage()["ocr_records"][0]["lines"][0]
    assert line == {
        "text": "原创 React 390×844",
        "confidence": 0.91,
        "box": [[1, 2], [35, 2], [35, 20], [1, 20]],
    }
    assert value.records[0]["nominal_seconds"] == 20.5
    assert value.coverage()["near_duplicate_filter"] == "not_enabled_without_quality_validation"


def test_candidate_limit_prevents_another_ocr_call():
    value = select(policy=MediaPolicy(max_ocr_frames=1))
    value.feed(page(0))
    with pytest.raises(ContextError, match="OCR") as caught:
        value.feed(page(1))
    assert caught.value.code == "ocr_candidate_limit" and len(value.engine.calls) == 1


@pytest.mark.parametrize("limit", ["frames", "bytes"])
def test_retention_limit_blocks_instead_of_silently_dropping_distinct_page(limit):
    value = select(
        policy=MediaPolicy(max_selected_frames=1) if limit == "frames" else None,
        max_bytes=1_000_000 if limit == "frames" else len(png()),
    )
    value.feed(page(0))
    with pytest.raises(ContextError) as caught:
        value.feed(page(1, data=png(color=(0, 10, 20, 255))))
    assert caught.value.code == ("frame_limit" if limit == "frames" else "media_input_limit")
    assert len(value.frames) == 1


@pytest.mark.parametrize("bad", ["checksum", "format", "pixels"])
def test_invalid_image_blocks_before_ocr(bad):
    value = select(policy=MediaPolicy(max_pixels=100) if bad == "pixels" else None)
    frame = page(0, data=b"not-an-image" if bad == "format" else None)
    if bad == "checksum":
        frame = replace(frame, sha256="0" * 64)
    with pytest.raises(ContextError) as caught:
        value.feed(frame)
    assert caught.value.code == ("media_changed" if bad == "checksum" else "ocr_image_invalid")
    assert value.engine.calls == [] and value.frames == []


def test_empty_selection_does_not_claim_ocr_success():
    value = select()
    assert value.coverage()["ocr_state"] == "not_applied"
    assert value.coverage()["ocr_calls"] == 0 and value.coverage()["complete"] is False
