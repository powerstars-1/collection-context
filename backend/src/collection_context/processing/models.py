"""Direct cloud HTTP adapter. Never invokes another project's CLI or loads its config."""

from __future__ import annotations

import base64
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collection_context.application.contracts import ContextError

MAX_INPUT_BYTES = 32_000_000
MAX_RESPONSE_BYTES = 4_000_000
COMPATIBLE_APIS = {"openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"}


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original, target = urlsplit(req.full_url), urlsplit(newurl)
        if (original.scheme, original.hostname, original.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            raise ContextError("provider_redirect_blocked", "模型接口跨站跳转被阻止，未转发凭据。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def direct_transport(request: Request, *, timeout: float):
    return build_opener(SameOriginRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True)
class ModelProfile:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    protocol: str = "chat"
    timeout: float = 120
    parameters: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    api: str | None = None

    def __post_init__(self):
        if self.api is not None and (
            not isinstance(self.api, str) or self.api not in COMPATIBLE_APIS or self.protocol != "pi_chat" or self.provider is not None
        ):
            raise ContextError("unsupported_model_protocol", "自定义模型协议无效。")
        if self.provider is not None:
            if not isinstance(self.provider, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", self.provider):
                raise ContextError("invalid_model_config", "供应商标识无效。")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ContextError("invalid_model_config", "模型地址须为不带秘密的 HTTP(S) 基础地址。")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ContextError("invalid_model_config", "非本机模型接口须使用 HTTPS。")
        if (
            not self.model
            or len(self.model) > 200
            or not self.api_key
            or "\n" in self.api_key
            or "\r" in self.api_key
        ):
            raise ContextError("invalid_model_config", "模型名或访问凭据无效。")
        if self.protocol not in {"chat", "pi_chat", "chat_audio", "transcription"}:
            raise ContextError("unsupported_model_protocol", "请选择已实现的聊天、音频聊天或转写协议。")
        if not 1 <= self.timeout <= 600:
            raise ContextError("invalid_model_config", "请求超时须在 1 到 600 秒之间。")
        allowed = {"temperature", "max_tokens", "max_completion_tokens", "thinking", "reasoning_effort"}
        if self.parameters.keys() - allowed:
            raise ContextError("invalid_model_config", "模型参数不在允许范围；不能覆盖模型或消息正文。")
        try:
            json.dumps(self.parameters, allow_nan=False)
        except (TypeError, ValueError):
            raise ContextError("invalid_model_config", "模型参数必须为有效 JSON。") from None


@dataclass(frozen=True)
class ModelResult:
    text: str
    actual_model: str | None
    configured_model: str
    usage: dict[str, Any] | None
    upstream_request_id: str | None
    elapsed_seconds: float
    finish_reason: str | None
    status: str


def _usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    # Keep returned measurements, do not invent a total or equate missing cache with zero.
    allowed = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
        "audio_tokens",
        "image_tokens",
    }

    def safe(v, depth=0):
        if type(v) in {int, float} and math.isfinite(v) and v >= 0:
            return v
        if isinstance(v, dict) and depth < 2:
            return {
                k: cleaned
                for k, sub in v.items()
                if isinstance(k, str) and len(k) <= 64 and (cleaned := safe(sub, depth + 1)) is not None
            }
        return None

    return {k: cleaned for k, v in value.items() if k in allowed and (cleaned := safe(v)) is not None}


class CloudModelClient:
    def __init__(self, profile: ModelProfile, *, transport: Callable = direct_transport):
        self.profile = profile
        self.transport = transport

    def _send(
        self, endpoint: str, data: bytes, content_type: str
    ) -> tuple[dict[str, Any], str | None, float]:
        profile = self.profile
        request = Request(
            profile.base_url.rstrip("/") + "/" + endpoint,
            data=data,
            headers={
                "Authorization": "Bearer " + profile.api_key,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with self.transport(request, timeout=profile.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ContextError(
                        "upstream_response_too_large", "模型返回超过读取上限。", possibly_charged=True
                    )
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise TypeError
                return payload, request_id, time.monotonic() - started
        except HTTPError as error:
            # Never copy provider bodies: they can contain requests, credentials or private text.
            raise ContextError(
                "upstream_http_error",
                f"模型接口返回 HTTP {error.code}；未自动重试。",
                next_action="核对模型能力、地址及上游请求记录。",
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ContextError(
                "upstream_outcome_unknown",
                "请求中断，上游是否处理未知；未自动重试。",
                possibly_charged=True,
                next_action="核对上游用量与请求状态后选择重试。",
            ) from None
        except (UnicodeError, ValueError, TypeError):
            raise ContextError(
                "invalid_model_response", "模型返回格式无效；保留该请求记录。", possibly_charged=True
            ) from None

    def _chat(self, content: list[dict[str, Any]]) -> ModelResult:
        payload = {
            "model": self.profile.model,
            "messages": [{"role": "user", "content": content}],
            **self.profile.parameters,
        }
        result, request_id, elapsed = self._send(
            "chat/completions", json.dumps(payload, allow_nan=False).encode(), "application/json"
        )
        try:
            choice = result["choices"][0]
            value = choice["message"]["content"]
            if isinstance(value, list):
                value = "\n".join(
                    p["text"] for p in value if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ContextError(
                "empty_model_output", "模型未返回可用正文，未标为成功。", possibly_charged=True
            ) from None
        return ModelResult(
            value.strip(),
            result.get("model") if isinstance(result.get("model"), str) else None,
            self.profile.model,
            _usage(result.get("usage")),
            request_id,
            elapsed,
            reason if isinstance(reason, str) else None,
            "partial" if reason in {"length", "max_tokens", "content_filter"} else "ready",
        )

    def text(self, prompt: str) -> ModelResult:
        return self._chat([{"type": "text", "text": prompt}])

    @staticmethod
    def _media(data: bytes) -> str:
        if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
            raise ContextError("media_input_limit", "媒体为空或超过已验证输入大小，未发请求。")
        return base64.b64encode(data).decode("ascii")

    def image(self, data: bytes, prompt: str, *, mime_type: str = "image/jpeg") -> ModelResult:
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ContextError("unsupported_media", "请先准备已支持的静态图片格式。")
        encoded = self._media(data)
        return self._chat(
            [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
            ]
        )

    def audio(self, data: bytes, prompt: str, *, audio_format: str = "wav") -> ModelResult:
        encoded = self._media(data)
        if audio_format not in {"wav", "mp3"}:
            raise ContextError("unsupported_media", "请先把音频转换为 WAV 或 MP3。")
        if self.profile.protocol == "chat_audio":
            return self._chat(
                [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}},
                ]
            )
        if self.profile.protocol != "transcription":
            raise ContextError("audio_capability_required", "当前角色未配置音频协议，未用字幕代替转写。")
        boundary = "context-" + uuid.uuid4().hex
        chunks = []
        for name, value in (("model", self.profile.model), ("prompt", prompt), ("response_format", "json")):
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks += [
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.{audio_format}"\r\nContent-Type: {"audio/wav" if audio_format == "wav" else "audio/mpeg"}\r\n\r\n'.encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        result, request_id, elapsed = self._send(
            "audio/transcriptions", b"".join(chunks), "multipart/form-data; boundary=" + boundary
        )
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ContextError("empty_model_output", "转写接口没有返回可用文本。", possibly_charged=True)
        return ModelResult(
            text.strip(),
            result.get("model") if isinstance(result.get("model"), str) else None,
            self.profile.model,
            _usage(result.get("usage")),
            request_id,
            elapsed,
            None,
            "ready",
        )
