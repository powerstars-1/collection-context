import io
import json
from urllib.error import HTTPError, URLError

import pytest

from collection_context.application.contracts import ContextError
from collection_context.processing.models import CloudModelClient, ModelProfile


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"x-request-id": "provider-request-1"}


def client(result, *, protocol="chat"):
    captured = []

    def transport(request, timeout):
        captured.append(request)
        return Response(json.dumps(result).encode())

    model = CloudModelClient(
        ModelProfile("https://models.example.test/v1", "user-model", "synthetic-test-key", protocol=protocol),
        transport=transport,
    )
    return model, captured


def response(text="原始转写", reason="stop", **extra):
    return {"choices": [{"message": {"content": text}, "finish_reason": reason}], **extra}


def test_direct_request_usage_cache_and_model_are_returned():
    model, requests = client(
        response(
            model="actual-model",
            usage={
                "prompt_tokens": 21,
                "prompt_tokens_details": {"cached_tokens": 8},
                "secret": "not forwarded",
            },
        )
    )
    result = model.text("合成示例")
    assert requests[0].full_url == "https://models.example.test/v1/chat/completions"
    assert result.actual_model == "actual-model"
    assert result.usage == {"prompt_tokens": 21, "prompt_tokens_details": {"cached_tokens": 8}}
    assert result.upstream_request_id == "provider-request-1"
    assert result.elapsed_seconds >= 0


def test_missing_usage_and_actual_model_are_unknown_not_fabricated():
    model, _ = client(response())
    result = model.text("示例")
    assert result.usage is None
    assert result.actual_model is None
    assert result.configured_model == "user-model"


def test_truncated_result_is_partial():
    model, _ = client(response(reason="length"))
    assert model.text("示例").status == "partial"


def test_audio_chat_uses_direct_bytes_no_cli_or_public_upload():
    model, requests = client(response(), protocol="chat_audio")
    model.audio(b"synthetic-audio", "逐字转写")
    payload = json.loads(requests[0].data)
    assert payload["messages"][0]["content"][1]["type"] == "input_audio"
    assert "url" not in payload["messages"][0]["content"][1]["input_audio"]


def test_audio_transcription_uses_own_multipart_adapter():
    model, requests = client({"text": "逐字转写"}, protocol="transcription")
    assert model.audio(b"synthetic-audio", "专有名词").text == "逐字转写"
    assert requests[0].full_url.endswith("/v1/audio/transcriptions")
    assert b'filename="audio.wav"' in requests[0].data
    assert b"synthetic-audio" in requests[0].data


def test_missing_audio_role_does_not_make_request():
    model, requests = client(response())
    with pytest.raises(ContextError, match="音频协议"):
        model.audio(b"synthetic-audio", "转写")
    assert not requests


def test_image_adapter_sends_selected_original():
    model, requests = client(response())
    model.image(b"synthetic-image", "提取原文", mime_type="image/png")
    image = json.loads(requests[0].data)["messages"][0]["content"][1]
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("result", [{"choices": []}, response(text=""), {"error": "upstream private body"}])
def test_empty_or_invalid_response_does_not_copy_private_body(result):
    model, _ = client(result)
    with pytest.raises(ContextError) as caught:
        model.text("示例")
    assert caught.value.code == "empty_model_output"
    assert caught.value.possibly_charged
    assert "private body" not in str(caught.value)


def test_ambiguous_network_outcome_is_never_retried():
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise URLError("private-network-body")

    model = CloudModelClient(
        ModelProfile("https://models.example.test/v1", "model", "synthetic"), transport=fail
    )
    with pytest.raises(ContextError) as caught:
        model.text("示例")
    assert caught.value.code == "upstream_outcome_unknown"
    assert caught.value.possibly_charged
    assert len(calls) == 1
    assert "private-network-body" not in str(caught.value)


def test_http_error_is_sanitized():
    def fail(request, timeout):
        raise HTTPError(request.full_url, 401, "private", {}, io.BytesIO(b"secret-and-private-text"))

    model = CloudModelClient(
        ModelProfile("https://models.example.test/v1", "model", "synthetic"), transport=fail
    )
    with pytest.raises(ContextError) as caught:
        model.text("示例")
    assert "401" in str(caught.value)
    assert "secret-and-private-text" not in str(caught.value)


def test_credentials_not_in_profile_repr_and_messages_cannot_be_overridden():
    profile = ModelProfile("https://models.example.test/v1", "model", "synthetic-secret")
    assert "synthetic-secret" not in repr(profile)
    with pytest.raises(ContextError):
        ModelProfile("https://models.example.test/v1", "model", "synthetic", parameters={"messages": []})


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example.test/v1",
        "https://user:password@example.test/v1",
        "https://example.test/v1?key=secret",
    ],
)
def test_model_base_url_has_explicit_security_boundary(url):
    with pytest.raises(ContextError):
        ModelProfile(url, "model", "synthetic")


def test_provider_redirect_cannot_forward_credentials_cross_origin():
    from urllib.request import Request

    from collection_context.processing.models import SameOriginRedirect

    request = Request(
        "https://models.example.test/v1/chat/completions", headers={"Authorization": "Bearer synthetic"}
    )
    with pytest.raises(ContextError) as caught:
        SameOriginRedirect().redirect_request(
            request, None, 302, "redirect", {}, "https://other.example.test/collect"
        )
    assert caught.value.code == "provider_redirect_blocked"
