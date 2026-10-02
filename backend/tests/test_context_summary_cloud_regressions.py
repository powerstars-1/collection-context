"""Offline failures observed in the explicitly authorized three-request cloud acceptance."""

import hashlib
import json

import pytest

from collection_context.processing.models import CloudModelClient, ModelProfile
from collection_context.processing.stages import LEGACY_SUMMARY_VERSION, SUMMARY_VERSION, summary_stage


def summarize(text, *, version=SUMMARY_VERSION):
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
        prompt_version=version,
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


def test_v4_prompt_is_byte_identical_for_previously_queued_tasks():
    _, prompt = summarize("保留导航[f_000000]", version=LEGACY_SUMMARY_VERSION)
    assert (
        hashlib.sha256(prompt.encode()).hexdigest()
        == "82120ae55a224f2d3f59659694445f5d177c90564b1acb2021911c91bc2c5031"
    )


def test_v5_lists_actual_screen_refs_without_suggesting_nonexistent_audio():
    _, prompt = summarize("保留导航[f_000000]")
    payload = json.loads(prompt.split("资料JSON：\n", 1)[1])
    assert payload["allowed_citations"] == [{"evidence_id": "f_000000", "kind": None, "status": "ready"}]
    assert "a_000000" not in prompt and "f_000001" not in prompt
    assert "不要因为编号或旧正文提到音频" in prompt


@pytest.mark.parametrize(
    "reply",
    [
        "## 概要\n保留导航[f_000000]，覆盖一张图[source_coverage]。",
        '{"概要":"保留导航[f_000000]","可复用提示词":"保留导航，禁止额外文字。"}',
        "## 可复用提示词\n逐字提示词：保留导航，禁止额外文字。[f_000000][原文]",
        "## 参数\n画布390 x 844、圆角16px[a_000000]。\n## 提示词\n“提示词：保留导航，禁止额外文字。”[原文][a_000000]",
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


def test_registered_text_cannot_reuse_its_embedded_previous_frame_citations():
    prompts = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return json.dumps(
                {
                    "choices": [
                        {"message": {"content": "正文第一页保留导航[f_000000]。"}, "finish_reason": "stop"}
                    ]
                }
            ).encode()

    def transport(request, **kwargs):
        prompts.append(json.loads(request.data)["messages"][0]["content"][0]["text"])
        return Response()

    client = CloudModelClient(
        ModelProfile("https://fixture.invalid/v1", "fixture", "synthetic-not-live-key"),
        transport=transport,
    )
    snapshot_id = "t_" + "1" * 64
    stage = summary_stage(
        "i_text_snapshot",
        {"title": "修正后的正文", "body": ""},
        ("text_screen",),
        client,
        source_coverage={},
        registered_text_refs=True,
    )
    result = stage.invoke(
        {
            "text_screen": {
                "status": "ready",
                "output": {
                    "kind": "screen",
                    "evidence_id": snapshot_id,
                    "text": "## [f_000000] 原图第1页\n\n保留导航。\n人工补充：蓝莓网格390。",
                    "configured_model": "not-content",
                    "finish_reason": "stop",
                },
            }
        }
    )
    assert result.status == "partial" and result.output["warnings"]
    assert result.output["available_evidence_refs"] == [snapshot_id]
    assert result.output["cited_evidence_refs"] == ["f_000000"]
    payload = json.loads(prompts[0].split("资料JSON：\n", 1)[1])
    assert payload["allowed_citations"] == [{"evidence_id": snapshot_id, "kind": "screen", "status": "ready"}]
    source = payload["evidence"]["text_screen"]["evidence"]
    assert "[f_000000]" in source["text"] and "蓝莓网格390" in source["text"]
    assert "configured_model" not in source and "finish_reason" not in source
    assert "本次唯一可用证据编号表" in prompts[0]
