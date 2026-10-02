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


@pytest.fixture
def remaining_helper(monkeypatch):
    monkeypatch.setitem(sys.modules, "cloud_model_acceptance", helper)
    specification = importlib.util.spec_from_file_location(
        "cloud_original_quality", Path(__file__).parents[1] / "tools/cloud_original_quality.py"
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def original_ledger(tmp_path):
    value = {
        "count": 4,
        "requests": [
            {"number": number, "role": role, "state": "response_opened", "http_status": 200}
            for number, role in enumerate(("audio", "vision", "summary", "summary"), start=1)
        ],
    }
    (tmp_path / "requests.json").write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.mark.parametrize("change", ["count", "unknown", "order", "role"])
def test_remaining_acceptance_rejects_changed_or_unknown_baseline(remaining_helper, tmp_path, change):
    value = original_ledger(tmp_path)
    if change == "count":
        value["count"] = 5
    elif change == "unknown":
        value["requests"][2]["state"] = "outcome_unknown"
    elif change == "order":
        value["requests"][0]["number"] = 2
    else:
        value["requests"][0]["role"] = "vision"
    (tmp_path / "requests.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContextError) as caught:
        remaining_helper.baseline(tmp_path)
    assert caught.value.code == "acceptance_baseline_invalid"


def test_remaining_literal_checks_report_missing_without_claiming_accuracy(remaining_helper):
    result = remaining_helper.text_check(
        "React + Tailwind，圆角 16px", ("React", "Tailwind", "16px", "Lucide")
    )
    assert result["missing"] == ["Lucide"]
    assert "accuracy" not in result and "recall" not in result


@pytest.mark.parametrize("bad_citation", [False, True])
def test_remaining_driver_runs_registered_workflows_four_calls_without_secret_storage(
    remaining_helper, tmp_path, monkeypatch, capsys, bad_citation
):
    previous = tmp_path / "previous"
    previous.mkdir()
    original_ledger(previous)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "base_url": "https://api.getwhite.cloud/v1",
                "default_model": "mimo-v2.6-flash",
                "keychain_service": "xingyue-new-api-media-remote",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        remaining_helper, "make_pages", lambda _: [b"first-original-png", b"second-original-png"]
    )
    monkeypatch.setattr(
        remaining_helper.subprocess,
        "run",
        lambda *a, **kw: type("Key", (), {"stdout": "fixture-private-test-key", "returncode": 0})(),
    )
    requests = []

    class Response:
        status = 200
        headers = {}

        def __init__(self, text):
            self.text = text

        def read(self, size):
            return json.dumps(
                {
                    "model": "fixture-response",
                    "choices": [{"message": {"content": self.text}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 1},
                }
            ).encode()

        def close(self):
            pass

    class Opener:
        def open(self, req, timeout):
            payload = json.loads(req.data)
            requests.append(payload)
            parts = payload["messages"][0]["content"]
            if any(part["type"] == "image_url" for part in parts):
                return Response("\n".join(remaining_helper.PAGES[len(requests) - 1]))
            evidence = json.loads(parts[0]["text"].split("资料JSON：\n", 1)[1])["evidence"]
            refs = [
                value["evidence"]["evidence_id"]
                for value in evidence.values()
                if value.get("evidence", {}).get("evidence_id")
            ]
            text = "品牌、用户、功能、视觉；React、Tailwind、390、16、0.45。" + "".join(
                "[" + ref + "]" for ref in refs
            )
            if bad_citation:
                text += "错误的音频引用[a_999999]。"
            if "蓝莓网格390" in parts[0]["text"]:
                text += "人工补充蓝莓网格390。"
            return Response(text)

    monkeypatch.setattr(remaining_helper, "build_opener", lambda *a: Opener())
    output = tmp_path / "original-test-run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance",
            "--allow-remaining-four",
            "--previous-root",
            str(previous),
            "--output",
            str(output),
            "--config",
            str(config),
            "--font",
            str(tmp_path / "unused-font"),
        ],
    )
    assert remaining_helper.main() == (2 if bad_citation else 0)
    report = json.loads((output / "report.json").read_text())
    assert len(requests) == report["new_requests"] == 4 and report["total_requests"] == 8
    assert report["refresh_only_one_call"] and report["source_unchanged_during_refresh"]
    assert report["refresh_checks"]["missing"] == [] and report["secret_matches"] == 0
    assert report["previous_ledger_unchanged"] and not report["real_speech_tested"]
    assert report["refresh_job"]["state"] == ("partial" if bad_citation else "succeeded")
    assert report["quality_gate_passed"] is (not bad_citation)
    assert "fixture-private-test-key" not in capsys.readouterr().out
    # The existing output is a persistent stop boundary; no re-dispatch.
    with pytest.raises(FileExistsError):
        remaining_helper.main()
    assert len(requests) == 4
    different = tmp_path / "other-output-cannot-reuse-authorization"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance",
            "--allow-remaining-four",
            "--previous-root",
            str(previous),
            "--output",
            str(different),
            "--config",
            str(config),
            "--font",
            str(tmp_path / "unused-font"),
        ],
    )
    with pytest.raises(FileExistsError):
        remaining_helper.main()
    assert len(requests) == 4


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
