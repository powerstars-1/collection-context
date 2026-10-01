from types import SimpleNamespace

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.ocr import CpuOcr


def test_cpu_adapter_preserves_text_box_and_confidence():
    output = SimpleNamespace(
        txts=["UI 提示词"], scores=[0.93], boxes=[[[0, 0], [100, 0], [100, 20], [0, 20]]]
    )
    result = CpuOcr(engine=lambda image: output).recognize(b"synthetic-image")
    assert result.state == "ready"
    assert result.text == "UI 提示词"
    assert result.lines[0].confidence == 0.93
    assert result.lines[0].box[2] == [100.0, 20.0]


def test_missing_models_is_explicit_not_silent_empty(tmp_path):
    with pytest.raises(ContextError) as caught:
        CpuOcr(tmp_path)
    assert caught.value.code == "ocr_models_required"


def test_valid_blank_image_is_different_from_engine_failure():
    blank = CpuOcr(engine=lambda image: SimpleNamespace(txts=None, boxes=None, scores=None))
    assert blank.recognize(b"synthetic-image").state == "no_text"

    def failing(image):
        raise RuntimeError("private-image-details")

    with pytest.raises(ContextError) as caught:
        CpuOcr(engine=failing).recognize(b"synthetic-image")
    assert caught.value.code == "ocr_failed"
    assert "private-image-details" not in str(caught.value)


def test_malformed_engine_output_is_not_success():
    malformed = SimpleNamespace(txts=["one", "two"], scores=[0.5], boxes=[[[0, 0]]])
    with pytest.raises(ContextError):
        CpuOcr(engine=lambda image: malformed).recognize(b"synthetic-image")
