"""Fixture-quality oracle must not use OCR text or nominal time as visual proof."""

import importlib
import io
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools" / "native_distribution"))
    return importlib.import_module("ocr_pipeline_smoke")


def image(color, size=(30, 20)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def originals(tmp_path, *, identical=False):
    labels = []
    for index, color in enumerate(("white", "white" if identical else "black")):
        name = f"page-{index}.png"
        (tmp_path / name).write_bytes(image(color))
        labels.append(
            {
                "index": index,
                "image": name,
                "start_seconds": index * 20,
                "end_seconds": (index + 1) * 20,
                "expected_token": str(390 + index),
            }
        )
    return labels


def record(index=0, *, selected=True, duplicate_of=None, text="391"):
    return {
        "evidence_id": f"f_{index:06}",
        "nominal_seconds": 19.9,
        "selected": selected,
        "duplicate_of": duplicate_of,
        "lines": [{"text": text}],
    }


def test_pixels_not_nominal_interval_determine_selected_page(helper, tmp_path):
    assessed, matches = helper.assess_pages(
        originals(tmp_path), tmp_path, [record()], {"f_000000": image("black")}
    )
    assert assessed[0]["selected"] is False and assessed[1]["selected"] is True
    assert assessed[1]["ocr_expected_token_found"] is True
    assert matches[0]["authored_page_index"] == 1 and matches[0]["normalized_pixel_distance"] == 0


def test_ocr_text_does_not_fake_visual_selection(helper, tmp_path):
    assessed, _ = helper.assess_pages(
        originals(tmp_path), tmp_path, [record(text="390")], {"f_000000": image("black")}
    )
    assert not assessed[0]["selected"] and assessed[1]["selected"]
    assert not assessed[1]["ocr_expected_token_found"]


@pytest.mark.parametrize("kind", ["tie", "distant", "wrong_size"])
def test_ambiguous_or_distant_matches_do_not_count_as_coverage(helper, tmp_path, kind):
    labels = originals(tmp_path, identical=kind == "tie")
    data = image("white" if kind == "tie" else "red", size=(1, 1) if kind == "wrong_size" else (30, 20))
    assessed, matches = helper.assess_pages(labels, tmp_path, [record()], {"f_000000": data})
    assert not any(p["selected"] for p in assessed) and matches[0]["authored_page_index"] is None


def test_dropped_duplicate_is_matched_to_retained_original(helper, tmp_path):
    assessed, matches = helper.assess_pages(
        originals(tmp_path),
        tmp_path,
        [record(), record(1, selected=False, duplicate_of="f_000000")],
        {"f_000000": image("black")},
    )
    assert assessed[1]["candidate_seen"] and assessed[1]["selected_evidence_ids"] == ["f_000000"]
    assert [p["authored_page_index"] for p in matches] == [1, 1]
