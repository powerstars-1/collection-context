"""Offline probe boundaries with synthetic engine results, not OCR quality evidence."""

import io
import json
import socket
from types import SimpleNamespace

import pytest
from PIL import Image

from collection_context.application import runtime_ocr_probe as probe
from collection_context.application.contracts import ContextError


def fixtures(root):
    pages, runtime = root / "pages", root / "runtime"
    pages.mkdir()
    runtime.mkdir()
    (runtime / "runtime-dependencies.json").write_bytes(b"synthetic receipt")
    data = io.BytesIO()
    Image.new("RGB", (900, 650), "white").save(data, format="PNG")
    for name in probe.PAGE_NAMES:
        (pages / name).write_bytes(data.getvalue())
    return dict(runtime_dir=runtime, library_dir=root / "library", probe_dir=root / "output", image_dir=pages)


@pytest.mark.parametrize(
    "kind", ["input_in_library", "output_in_runtime", "same", "existing", "linked", "relative"]
)
def test_unsafe_paths_rejected_before_engine(tmp_path, monkeypatch, kind):
    args = fixtures(tmp_path.resolve())
    if kind == "input_in_library":
        args["library_dir"] = args["image_dir"].parent
    elif kind == "output_in_runtime":
        args["probe_dir"] = args["runtime_dir"] / "child"
    elif kind == "same":
        args["probe_dir"] = args["image_dir"]
    elif kind == "existing":
        args["probe_dir"].mkdir()
    elif kind == "linked":
        link = tmp_path / "link"
        link.symlink_to(args["image_dir"], target_is_directory=True)
        args["image_dir"] = link
    else:
        args["image_dir"] = type(tmp_path)("relative")
    monkeypatch.setattr(probe, "load_runtime_ocr", lambda *a, **k: pytest.fail("engine must not run"))
    with pytest.raises(ContextError):
        probe.probe_ocr_runtime(**args)
    assert not args["library_dir"].exists() if kind != "input_in_library" else True


@pytest.mark.parametrize("kind", ["missing", "wrong_size", "invalid_png", "hardlink"])
def test_invalid_images_do_not_create_output(tmp_path, monkeypatch, kind):
    import os

    args = fixtures(tmp_path.resolve())
    path = args["image_dir"] / probe.PAGE_NAMES[0]
    if kind == "missing":
        path.unlink()
    elif kind == "wrong_size":
        Image.new("RGB", (10, 10)).save(path, format="PNG")
    elif kind == "invalid_png":
        path.write_bytes(b"original invalid fixture")
    else:
        os.link(path, tmp_path / "second-link")
    monkeypatch.setattr(probe, "load_runtime_ocr", lambda *a, **k: pytest.fail("engine must not run"))
    with pytest.raises(ContextError):
        probe.probe_ocr_runtime(**args)
    assert not args["probe_dir"].exists()


@pytest.mark.parametrize("kind", ["complete", "missing_text", "changed_receipt", "network", "init_failed"])
def test_probe_truthful_report_and_no_library(tmp_path, monkeypatch, kind):
    args = fixtures(tmp_path.resolve())
    seen = []

    class Engine:
        engine_version = "synthetic"
        model_hashes = {"Det": "synthetic"}

        def recognize(self, data):
            seen.append(data)
            if kind == "changed_receipt":
                (args["runtime_dir"] / "runtime-dependencies.json").write_bytes(b"changed")
            return SimpleNamespace(
                state="ready",
                text="missing" if kind == "missing_text" else " ".join(probe.EXPECTED),
                elapsed_seconds=0.01,
            )

    def load(*a, **k):
        if kind == "network":
            socket.getaddrinfo("example.invalid", 443)
        if kind == "init_failed":
            raise ContextError("ocr_initialization_failed", "original failure")
        return Engine()

    monkeypatch.setattr(probe, "load_runtime_ocr", load)
    if kind == "complete":
        result = probe.probe_ocr_runtime(**args)
        assert result["state"] == "verified" and len(seen) == 6
        assert result["network_attempts"] == 0 and result["content_quality_verified"] is False
    else:
        with pytest.raises(ContextError):
            probe.probe_ocr_runtime(**args)
        result = json.loads((args["probe_dir"] / "report.json").read_bytes())
        assert result["state"] == "failed" and "error_code" in result
        if kind == "network":
            assert result["network_attempts"] == 1
    assert not args["library_dir"].exists()
