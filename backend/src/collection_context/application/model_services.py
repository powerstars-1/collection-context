"""Service-level secret references and atomic role assignment for the owner UI."""
from __future__ import annotations

import copy
import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from collection_context.application.contracts import ContextError, digest, valid_id
from collection_context.application.model_setup import ModelSetup
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.processing.models import COMPATIBLE_APIS, ModelProfile, direct_transport
from collection_context.processing.pi_client import invoke
from collection_context.processing.profiles import ModelCatalog, validated

# Display labels only. Provider membership, endpoints and chat models come from Pi.
LABELS = {"xiaomi-token-plan-cn": "小米 Token Plan · 中国区", "xiaomi": "小米 MiMo · 按量",
    "xiaomi-token-plan-ams": "小米 Token Plan · 欧洲区", "xiaomi-token-plan-sgp": "小米 Token Plan · 新加坡",
    "qwen-token-plan-cn": "阿里千问 Token Plan · 中国区", "qwen-token-plan": "阿里千问 Token Plan · 国际区",
    "qwen-token-plan-individual": "阿里千问 Token Plan · 个人版", "google": "Google Gemini", "anthropic": "Anthropic / Claude",
    "minimax-cn": "MiniMax · 中国区", "moonshotai-cn": "Moonshot / Kimi · 中国区",
    "zai-coding-cn": "智谱 Coding Plan · 中国区"}
ALIASES = {"xiaomi_plan": "xiaomi-token-plan-cn", "ali_plan": "qwen-token-plan-cn"}
COMPATIBLE = {"newapi": {"name": "New API", "base_url": ""},
    "ali": {"name": "阿里百炼 · 按量", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "custom": {"name": "自定义兼容接口", "base_url": ""}}


def service_url(value: str, api: str) -> str:
    """Remove a complete endpoint, but preserve a user's explicit gateway prefix."""
    value = value.strip().rstrip("/")
    suffix = {"openai-completions": "/chat/completions", "openai-responses": "/responses",
        "anthropic-messages": "/messages"}.get(api)
    if suffix and value.endswith(suffix):
        value = value[:-len(suffix)]
    if not urlsplit(value).path:
        value += "/v1beta" if api == "google-generative-ai" else "/v1" if api.startswith("openai-") else ""
    # Anthropic's SDK appends /v1/messages itself; retain nonstandard proxy prefixes.
    if api == "anthropic-messages" and value.endswith("/v1"):
        value = value[:-len("/v1")]
    return value


@lru_cache(maxsize=1)
def provider_catalog() -> list[dict]:
    return invoke({"action": "catalog"})


def providers() -> list[dict]:
    catalog = [{**entry, "name": LABELS.get(entry["id"], entry["name"])} for entry in provider_catalog()]
    for entry in catalog:
        identity = entry["id"]
        # Pi's chat catalog is not an ASR catalog. Keep the existing audio adapter explicit.
        entry["audio_protocols"] = ["chat_audio"] if identity.startswith("xiaomi") else ["transcription"] if identity in {"openai", "groq"} else []
        entry["audio_models"] = ([{"id": "whisper-1", "name": "Whisper"}, {"id": "gpt-4o-mini-transcribe", "name": "GPT-4o mini Transcribe"}] if identity == "openai" else
            [{"id": "whisper-large-v3-turbo", "name": "Whisper Large v3 Turbo"}, {"id": "whisper-large-v3", "name": "Whisper Large v3"}] if identity == "groq" else
            [model for model in entry["models"] if model["id"] in {"mimo-v2.5", "mimo-v2.6-flash"}] if identity.startswith("xiaomi") else [])
    return catalog + [{"id": key, **entry, "source": "compatible", "configurable": True,
        "apis": sorted(COMPATIBLE_APIS) if key == "custom" else ["openai-completions"],
        "models": [], "audio_models": [], "audio_protocols": ["transcription", "chat_audio"]} for key, entry in COMPATIBLE.items()]


class ModelServices:
    def __init__(self, setup: ModelSetup):
        self.setup, self.store = setup, setup.store

    @staticmethod
    def _services(state: dict) -> dict:
        services = copy.deepcopy(state["settings"].get("model_services", {}))
        for profile_id in state["settings"].get("model_roles", {}).values():
            profile = ModelCatalog.from_state(state, profile_id)
            if any(s["base_url"] == profile["base_url"] and s["credential_ref"] == profile["credential_ref"] for s in services.values()):
                continue
            identity = "svc_" + digest({"url": profile["base_url"], "key": profile["credential_ref"]})[:32]
            services[identity] = {"id": identity, "name": "已配置服务", "provider": "custom",
                "base_url": profile["base_url"], "credential_ref": profile["credential_ref"], "revision": 1,
                "api": profile.get("api", "openai-completions")}
        return services

    def settings(self) -> dict:
        state = self.store.snapshot()
        services = self._services(state)
        try:
            catalogs = providers()
            catalog_error = None
        except ContextError:
            catalogs = [{"id": key, **entry, "source": "compatible", "configurable": True,
                "apis": sorted(COMPATIBLE_APIS) if key == "custom" else ["openai-completions"],
                "models": [], "audio_models": [], "audio_protocols": ["transcription", "chat_audio"]} for key, entry in COMPATIBLE.items()]
            catalog_error = "供应商目录加载失败，请刷新重试。"
        assignments = {}
        for role, entry in self.setup.settings()["roles"].items():
            profile = entry["profile"]
            private = ModelCatalog.from_state(state, entry["profile_id"]) if profile else None
            service = next((s for s in services.values() if private and s["credential_ref"] == private["credential_ref"] and s["base_url"] == private["base_url"]), None)
            assignments[role] = {"service_id": service["id"] if service else "", "model": profile["model"] if profile else "",
                "protocol": profile["protocol"] if profile else "transcription" if role == "audio" else "pi_chat",
                "parameters": profile["parameters"] if profile else {}, "timeout": profile["timeout"] if profile else 120,
                "profile_id": entry["profile_id"]}
        worker = self.store.storage.observe_worker(self.store.files)
        return {"services": [{**{k: v for k, v in s.items() if k != "credential_ref"},
            "provider": ALIASES.get(s["provider"], s["provider"])} for s in services.values()],
            "providers": catalogs, "assignments": assignments, "configuration_enabled": self.setup.secrets is not None,
            "model_execution_enabled": worker["online"] is True and worker.get("capabilities", {}).get("model_calls") is True,
            "catalog_error": catalog_error, "model_requests": 0}

    def save(self, *, service_id: str | None, name: str, provider: str, base_url: str,
             api_key: str, expected_revision: int | None, api: str | None = None) -> dict:
        secrets = self.setup.secrets
        if secrets is None:
            raise ContextError("model_setup_disabled", "模型配置暂不可用，请检查运行环境。")
        provider = ALIASES.get(provider, provider)
        selected = next((entry for entry in providers() if entry["id"] == provider), None)
        if selected is None or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ContextError("invalid_model_config", "请选择服务并填写名称。")
        if not selected.get("configurable", True):
            raise ContextError("provider_setup_required", selected["unavailable_reason"])
        if not isinstance(api_key, str):
            raise ContextError("invalid_model_config", "密钥格式无效。")
        if not isinstance(base_url, str):
            raise ContextError("invalid_model_config", "请填写服务地址。")
        base_url = (base_url.strip() or selected["base_url"]).rstrip("/")
        native = selected["source"] == "sdk"
        if not native:
            api = api or "openai-completions"
            if not isinstance(api, str) or api not in selected["apis"]:
                raise ContextError("unsupported_model_protocol", "请选择此服务支持的接口协议。")
            base_url = service_url(base_url, api)
        elif api is not None:
            raise ContextError("unsupported_model_protocol", "内置供应商的协议由模型组件自动配置。")
        ModelProfile(base_url, "validate", "placeholder")
        if api_key:
            FileSecrets._key(api_key)
            if api_key in json.dumps([name, provider, base_url]):
                raise ContextError("invalid_model_config", "密钥只能填写在 API Key 栏。")
        identity = valid_id(service_id) if service_id else "svc_" + uuid.uuid4().hex
        created = []

        def change(state):
            services = self._services(state)
            old = services.get(identity)
            if (old or {}).get("revision") != expected_revision:
                raise ContextError("model_config_conflict", "服务已变化，请刷新后再保存。")
            if not old and not api_key:
                raise ContextError("credential_required", "请填写 API Key。")
            if old and not native and api != "openai-completions" and any(
                ModelCatalog.from_state(state, profile_id)["credential_ref"] == old["credential_ref"]
                for role, profile_id in state["settings"].get("model_roles", {}).items() if role == "audio"
            ):
                raise ContextError("model_service_in_use", "此服务用于音频转写，请先更换音频模型后再切换协议。")
            ref = old["credential_ref"] if not api_key else secrets.put(api_key)
            if api_key:
                created.append(ref)
            services[identity] = {"id": identity, "name": name.strip(), "provider": provider,
                "base_url": base_url, "credential_ref": ref, "revision": (old or {}).get("revision", 0) + 1}
            if not native:
                services[identity]["api"] = api
            state["settings"]["model_services"] = services
            # Rotation updates new-work defaults only; queued immutable profiles remain valid.
            for role, profile_id in list(state["settings"].get("model_roles", {}).items()):
                profile = ModelCatalog.from_state(state, profile_id)
                if old and profile["credential_ref"] == old["credential_ref"] and profile["base_url"] == old["base_url"]:
                    changed = {**profile, "base_url": base_url, "credential_ref": ref}
                    changed.pop("provider", None)
                    changed.pop("api", None)
                    if changed["protocol"] == "pi_chat" and selected["source"] == "sdk":
                        changed["provider"] = provider
                    elif changed["protocol"] == "pi_chat":
                        changed["api"] = api
                    updated = validated(changed)
                    next_id = "p_" + digest(updated)
                    state["model_profiles"][next_id] = updated
                    state["settings"]["model_roles"][role] = next_id
            return identity

        try:
            self.store.transact(change)
        except ContextError:
            # Keep credentials if persistence is uncertain, never delete a referenced key.
            if created:
                state = self.store.snapshot()
                refs = {s["credential_ref"] for s in self._services(state).values()} | {p["credential_ref"] for p in state.get("model_profiles", {}).values()}
                for ref in created:
                    if ref not in refs:
                        secrets.discard_new(ref)
            raise
        return self.settings()

    def assign(self, *, assignments: dict[str, dict], expected_profiles: dict) -> dict:
        if not isinstance(assignments, dict) or set(assignments) - {"audio", "vision", "summary"} or not assignments:
            raise ContextError("invalid_model_config", "请选择至少一种用途模型。")
        catalogs = {entry["id"]: entry for entry in providers()}
        def change(state):
            roles = state["settings"].setdefault("model_roles", {})
            services = self._services(state)
            if expected_profiles != {role: roles.get(role) for role in ("audio", "vision", "summary")}:
                raise ContextError("model_config_conflict", "模型设置已变化，请刷新后重试。")
            profiles = {}
            for role, choice in assignments.items():
                if not isinstance(choice, dict) or set(choice) != {"service_id", "model", "protocol", "timeout", "parameters"}:
                    raise ContextError("invalid_model_config", "用途设置格式无效。")
                service = services.get(valid_id(choice["service_id"]))
                if service is None:
                    raise ContextError("model_config_missing", "所选模型服务不存在。")
                profile = {"schema_version": 1, "role": role,
                    "base_url": service["base_url"], "credential_ref": service["credential_ref"],
                    **{key: choice[key] for key in ("model", "protocol", "timeout", "parameters")}}
                provider_id = ALIASES.get(service["provider"], service["provider"])
                selected = catalogs.get(provider_id)
                if selected and selected["source"] == "sdk":
                    if not selected.get("configurable", True):
                        raise ContextError("provider_setup_required", selected["unavailable_reason"])
                    if role == "audio":
                        if choice["protocol"] not in selected["audio_protocols"]:
                            raise ContextError("model_capability_required", "该供应商未接入音频转写，请选择支持音频的服务。")
                    else:
                        model = next((m for m in selected["models"] if m["id"] == choice["model"]), None)
                        if model is None:
                            raise ContextError("model_not_in_catalog", "此模型不在供应商目录中，请刷新目录或使用自定义兼容接口。")
                        if role == "vision" and "image" not in model.get("input", []):
                            raise ContextError("model_capability_required", "该模型不支持画面识别。")
                        profile["protocol"] = "pi_chat"
                        profile["provider"] = provider_id
                elif profile["protocol"] == "pi_chat":
                    profile["api"] = service.get("api", "openai-completions")
                if role == "audio" and service.get("api", "openai-completions") != "openai-completions":
                    raise ContextError("model_capability_required", "此自定义协议未接入音频转写，请选择音频服务。")
                profiles[role] = validated(profile)
            for role, profile in profiles.items():
                identity = "p_" + digest(profile)
                state.setdefault("model_profiles", {})[identity] = profile
                roles[role] = identity
            state["settings"]["model_services"] = services
        self.store.transact(change)
        return self.settings()

    def remove(self, *, service_id: str) -> dict:
        def change(state):
            services = self._services(state)
            service = services.get(valid_id(service_id))
            if service is None:
                raise ContextError("not_found", "服务不存在。")
            if any(ModelCatalog.from_state(state, identity)["credential_ref"] == service["credential_ref"] for identity in state["settings"].get("model_roles", {}).values()):
                raise ContextError("model_service_in_use", "该服务仍用于提取，请先更换用途模型。")
            del services[service_id]
            state["settings"]["model_services"] = services
        self.store.transact(change)
        return self.settings()

    def model_list(self, *, service_id: str) -> dict:
        service = self._services(self.store.snapshot()).get(valid_id(service_id))
        if service is None or self.setup.secrets is None:
            raise ContextError("model_config_missing", "模型服务不存在。")
        provider_id = ALIASES.get(service["provider"], service["provider"])
        selected = next((entry for entry in providers() if entry["id"] == provider_id), None)
        if selected and selected["source"] == "sdk":
            return {"models": selected["models"], "notice": "已读取 SDK 模型目录。", "model_requests": 0}
        if service.get("api", "openai-completions") != "openai-completions":
            return {"models": [], "notice": "此自定义协议请填写供应商提供的模型名称。", "model_requests": 0}
        ModelProfile(service["base_url"], "validate", "placeholder")
        request = Request(service["base_url"].rstrip("/") + "/models", headers={
            "Authorization": "Bearer " + self.setup.secrets.get(service["credential_ref"]), "Accept": "application/json"})
        try:
            with direct_transport(request, timeout=15) as response:
                raw = response.read(256_001)
            if len(raw) > 256_000:
                raise ValueError
            payload = json.loads(raw)
            values = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(values, list) or len(values) > 2000:
                raise ValueError
            models = [{"id": row["id"], "name": row["id"]} for row in values
                if isinstance(row, dict) and isinstance(row.get("id"), str) and 1 <= len(row["id"]) <= 200]
            return {"models": models, "model_requests": 0}
        except HTTPError as error:
            if error.code in {404, 405}:
                provider = next((p for p in self.settings()["providers"] if p["id"] == service["provider"]), {})
                return {"models": provider.get("models", []), "notice": "此服务不提供模型列表，可使用内置目录或填写模型名称。", "model_requests": 0}
            raise ContextError("model_list_failed", f"获取模型列表失败（HTTP {error.code}），请检查 Key 和服务地址。") from None
        except (ValueError, TypeError, URLError, TimeoutError, OSError):
            raise ContextError("model_list_failed", "模型列表无法读取，请检查服务地址和网络。") from None
