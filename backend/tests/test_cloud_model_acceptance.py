"""The paid acceptance helper must fail closed and never auto-retry."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from collection_context.application.contracts import ContextError

spec = importlib.util.spec_from_file_location(
    "cloud_acceptance", Path(__file__).parents[1] / "tools/cloud_model_acceptance.py"
)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def request():
    return Request(
        "https://gateway.invalid/v1/chat/completions",
        data=json.dumps(
            {
                "model": "fixture",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "original fixture"}]}],
            }
        ).encode(),
    )


def test_three_attempt_budget_is_checkpointed_before_dispatch_and_never_retried(tmp_path):
    class Opener:
        count = 0

        def open(self, req, timeout):
            self.count += 1
            audit = json.loads((tmp_path / "requests.json").read_text())
            assert audit["count"] == self.count
            raise HTTPError(req.full_url, 400, "fixture", {}, io.BytesIO(b"not persisted"))

    opener = Opener()
    transport = helper.CountedTransport(tmp_path, opener, api_key="fixture-secret")
    for _ in range(3):
        with pytest.raises(HTTPError):
            transport(request(), timeout=5)
    with pytest.raises(ContextError, match="上限"):
        transport(request(), timeout=5)
    audit = json.loads((tmp_path / "requests.json").read_text())
    assert audit["count"] == opener.count == 3
    assert all(record["state"] == "http_error" for record in audit["requests"])
    assert "fixture-secret" not in json.dumps(audit)
    assert "not persisted" not in json.dumps(audit)


def test_header_secret_echo_is_refused_before_result_storage():
    class Response:
        headers = {}
        closed = False

        def read(self, size):
            return b'{"text":"fixture-secret"}'

        def close(self):
            self.closed = True

    response = Response()
    with pytest.raises(ContextError, match="秘密"):
        with helper.SecretCheckedResponse(response, "fixture-secret") as guarded:
            guarded.read(100)
    assert response.closed


def test_summary_only_fourth_call_cannot_dispatch_audio_or_vision(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cloud_model_acceptance", helper)
    recheck_spec = importlib.util.spec_from_file_location(
        "cloud_recheck", Path(__file__).parents[1] / "tools/cloud_summary_recheck.py"
    )
    assert recheck_spec and recheck_spec.loader
    recheck = importlib.util.module_from_spec(recheck_spec)
    recheck_spec.loader.exec_module(recheck)

    class Opener:
        count = 0

        def open(self, req, timeout):
            self.count += 1
            raise HTTPError(req.full_url, 400, "fixture", {}, io.BytesIO())

    opener = Opener()
    records = [{"number": n, "state": "response_opened"} for n in (1, 2, 3)]
    transport = recheck.SummaryOnlyTransport(
        tmp_path, opener, api_key="fixture-secret", limit=4, existing_records=records
    )
    for kind in ("input_audio", "image_url"):
        media_request = Request(
            "https://gateway.invalid/v1/chat/completions",
            data=json.dumps({"model": "fixture", "messages": [{"content": [{"type": kind}]}]}).encode(),
        )
        with pytest.raises(RuntimeError, match="blocked"):
            transport(media_request, timeout=5)
    assert opener.count == 0
    assert len(transport.records) == 3
    with pytest.raises(HTTPError):
        transport(request(), timeout=5)
    with pytest.raises(ContextError, match="上限"):
        transport(request(), timeout=5)
    assert opener.count == 1 and len(transport.records) == 4
