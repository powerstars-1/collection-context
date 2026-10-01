"""Offline failures observed in the explicitly authorized three-request cloud acceptance."""

import json

import pytest

from collection_context.processing.models import CloudModelClient, ModelProfile
from collection_context.processing.stages import summary_stage


def summarize(text):
    captured = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return json.dumps(
                {"model": "fixture", "choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
            ).encode()

    def transport(request, *, timeout):
        captured.append(json.loads(request.data)["messages"][0]["content"][0]["text"])
        return Response()

    client = CloudModelClient(
        ModelProfile("https://fixture.invalid/v1", "fixture", "synthetic-not-live-key"), transport=transport
    )
    original = {
        "title": "原创界面参数测试",
        "body": "独立原创截图与本机合成语音。不是私人收藏；仅验收本轮指定片段。",
    }
    stage = summary_stage(
        "i_original_fixture",
        original,
        ("screen_f_000000",),
        client,
        source_coverage={"complete": False, "scope": "one original still page"},
    )
    result = stage.invoke(
        {
            "screen_f_000000": {
                "status": "ready",
                "output": {"evidence_id": "f_000000", "text": "提示词：保留导航，禁止额外文字。"},
            }
        }
    )
    return result, captured[0]


@pytest.mark.parametrize(
    "reply",
    [
        "## 概要\n保留导航[f_000000]，覆盖一张图[source_coverage]。",
        '{"概要":"保留导航[f_000000]","可复用提示词":"保留导航，禁止额外文字。"}',
        "## 可复用提示词\n逐字提示词：保留导航，禁止额外文字。[f_000000][原文]",
    ],
)
def test_observed_false_citation_and_json_reading_draft_are_not_ready(reply):
    result, _ = summarize(reply)
    assert result.status == "partial"
    assert result.output["warnings"]


def test_markdown_with_only_supported_screen_citation_can_be_ready():
    result, prompt = summarize(
        "## 概要\n原图包含界面提示词[f_000000]。\n\n## 可复用提示词\n逐字提示词：保留导航，禁止额外文字。[f_000000]\n\n## 缺口\n只覆盖一张截图，未验证完整视频。"
    )
    assert result.status == "ready"
    assert result.output["warnings"] == []
    assert "Markdown" in prompt


def test_verbatim_original_quote_remains_supported():
    result, _ = summarize(
        '## 概要\n原文声明："不是私人收藏"[原文]。\n\n## 可复用提示词\n保留导航，禁止额外文字。[f_000000]\n\n## 缺口\n只覆盖一张截图。'
    )
    assert result.status == "ready"
    assert result.output["warnings"] == []
