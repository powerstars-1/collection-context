"""Immutable, non-secret role profiles; defaults are NOT the configuration of an existing job."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from typing import Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id
from collection_context.library.store import LibraryStore
from collection_context.processing.models import CloudModelClient, ModelProfile

ROLES = {"audio": {"chat_audio", "transcription"}, "vision": {"chat"}, "summary": {"chat"}}
FIELDS = {
    "schema_version",
    "role",
    "base_url",
    "model",
    "protocol",
    "parameters",
    "timeout",
    "credential_ref",
}
_UNSET = object()


def validated(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FIELDS or value.get("schema_version") != 1:
        raise ContextError("invalid_model_config", "模型角色配置结构或版本不兼容。")
    role = value["role"]
    if (
        not isinstance(role, str)
        or role not in ROLES
        or not isinstance(value["protocol"], str)
        or value["protocol"] not in ROLES[role]
    ):
        raise ContextError("model_capability_required", "模型角色和输入协议不匹配。")
    ref = valid_id(value["credential_ref"])
    if not ref.startswith("k_") or len(ref) != 34:
        raise ContextError("invalid_credential_reference", "请使用独立凭据引用，不能把 Key 放入配置。")
    parameters = value["parameters"]
    if not isinstance(parameters, dict):
        raise ContextError("invalid_model_config", "模型参数须为受控 JSON。")
    for name, setting in parameters.items():
        good = False
        if name == "temperature":
            good = type(setting) in {int, float} and math.isfinite(setting) and 0 <= setting <= 2
        elif name in {"max_tokens", "max_completion_tokens"}:
            good = type(setting) is int and 1 <= setting <= 1_000_000
        elif name == "reasoning_effort":
            good = isinstance(setting, str) and setting in {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            }
        elif name == "thinking":
            good = (
                isinstance(setting, dict)
                and set(setting) == {"type"}
                and isinstance(setting["type"], str)
                and setting["type"] in {"enabled", "disabled"}
            )
        if not good:
            raise ContextError("invalid_model_config", "模型参数超出受控范围，未保存。")
    if (
        not isinstance(value["base_url"], str)
        or not isinstance(value["model"], str)
        or not isinstance(value["protocol"], str)
    ):
        raise ContextError("invalid_model_config", "模型地址、名称或协议无效。")
    if type(value["timeout"]) not in {int, float} or not math.isfinite(value["timeout"]):
        raise ContextError("invalid_model_config", "模型超时无效。")
    try:
        ModelProfile(
            value["base_url"],
            value["model"],
            "validation-only",
            protocol=value["protocol"],
            parameters=parameters,
            timeout=value["timeout"],
        )
    except (TypeError, ValueError):
        raise ContextError("invalid_model_config", "模型地址或参数无效；未回显配置内容。") from None
    return copy.deepcopy(value)


class ModelCatalog:
    def __init__(self, store: LibraryStore):
        self.store = store

    def configure(
        self,
        *,
        role: str,
        base_url: str,
        model: str,
        credential_ref: str | None,
        protocol: str = "chat",
        parameters: dict[str, Any] | None = None,
        timeout: float = 120,
        expected_profile_id: str | None | object = _UNSET,
        create_credential: Callable[[], str] | None = None,
    ) -> str:
        if (credential_ref is None) != (create_credential is not None):
            raise ContextError("invalid_credential_reference", "只接受已有凭据引用或明确的新凭据保存动作。")
        if expected_profile_id is not _UNSET and expected_profile_id is not None:
            if not isinstance(expected_profile_id, str) or not valid_id(expected_profile_id).startswith("p_"):
                raise ContextError("invalid_model_config", "当前模型配置版本无效。")
        profile = validated(
            {
                "schema_version": 1,
                "role": role,
                "base_url": base_url,
                "model": model,
                "credential_ref": credential_ref if credential_ref is not None else "k_" + "0" * 32,
                "protocol": protocol,
                "parameters": {} if parameters is None else parameters,
                "timeout": timeout,
            }
        )

        def save(state):
            current = state["settings"].get("model_roles", {}).get(role)
            if expected_profile_id is not _UNSET and current != expected_profile_id:
                raise ContextError(
                    "model_config_conflict", "模型默认配置已改变，请刷新后确认；未保存新密钥。"
                )
            saved = profile
            if create_credential is not None:
                saved = validated({**profile, "credential_ref": create_credential()})
            identity = "p_" + digest(saved)
            state.setdefault("model_profiles", {})[identity] = saved
            state["settings"].setdefault("model_roles", {})[role] = identity
            state["settings"]["model_updated_at"] = utc_now()
            return identity

        return self.store.transact(save)

    def get(self, identity: str) -> dict[str, Any]:
        return self.from_state(self.store.snapshot(), identity)

    @staticmethod
    def from_state(state: dict[str, Any], identity: str) -> dict[str, Any]:
        valid_id(identity)
        value = state.get("model_profiles", {}).get(identity)
        if value is None:
            raise ContextError("model_config_missing", "任务固定的模型配置不存在，未替换为当前默认模型。")
        profile = validated(value)
        if identity != "p_" + digest(profile):
            raise ContextError("model_config_changed", "模型配置快照已改变，未发请求。")
        return profile

    def pin(self, roles: tuple[str, ...]) -> dict[str, str]:
        configured = self.store.snapshot()["settings"].get("model_roles", {})
        selected = {}
        for role in roles:
            if role not in ROLES or role not in configured:
                raise ContextError("model_config_missing", "处理所需的模型角色尚未配置。")
            identity = configured[role]
            if self.get(identity)["role"] != role:
                raise ContextError("model_config_changed", "模型角色引用不匹配。")
            selected[role] = identity
        return selected

    def client(self, identity: str, resolve_secret: Callable[[str], str]) -> CloudModelClient:
        profile = self.get(identity)
        # Secrets are resolved only from an explicitly supplied backend, never from environment/legacy config.
        return CloudModelClient(
            ModelProfile(
                profile["base_url"],
                profile["model"],
                resolve_secret(profile["credential_ref"]),
                protocol=profile["protocol"],
                parameters=profile["parameters"],
                timeout=profile["timeout"],
            )
        )

    def public_settings(self) -> dict[str, Any]:
        roles = self.store.snapshot()["settings"].get("model_roles", {})
        return {
            role: {key: value for key, value in self.get(identity).items() if key != "credential_ref"}
            for role, identity in roles.items()
        }
