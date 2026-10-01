"""Owner-only model setup with explicit fixed secret backend; no inference or secret readback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.library.store import LibraryStore
from collection_context.processing.profiles import ROLES, ModelCatalog


def separate_credentials(workspace: Path, credential_dir: Path) -> None:
    library, private = workspace.resolve(), credential_dir.resolve()
    if library.is_relative_to(private) or private.is_relative_to(library):
        raise ContextError("secret_directory_overlap", "凭据目录不能与资料库重叠，不随资料导出。")


class ModelSetup:
    def __init__(self, store: LibraryStore, secrets: CredentialBackend | None = None):
        if secrets is not None:
            separate_credentials(store.files.root, secrets.files.root)
        self.store, self.secrets = store, secrets
        self.catalog = ModelCatalog(store)

    def settings(self) -> dict[str, Any]:
        state = self.store.snapshot()
        configured = state["settings"].get("model_roles", {})
        roles = {}
        for role in ROLES:
            identity = configured.get(role)
            value = self.catalog.from_state(state, identity) if identity is not None else None
            if value and value["role"] != role:
                raise ContextError("model_config_changed", "默认角色登记不符，未展示错误配置。")
            roles[role] = {
                "profile_id": identity,
                "configured": value is not None,
                "profile": {k: v for k, v in value.items() if k != "credential_ref"} if value else None,
                "credential_state": "registered_not_verified" if value else "not_configured",
            }
        return {
            "roles": roles,
            "configuration_enabled": self.secrets is not None,
            "credential_storage": self.secrets.storage_kind if self.secrets is not None else "not_enabled",
            "model_requests": 0,
        }

    def save(
        self,
        *,
        role: str,
        base_url: str,
        model: str,
        protocol: str,
        parameters: dict[str, Any],
        timeout: float,
        api_key: str,
        expected_profile_id: str | None,
        credential_confirmed: bool,
    ) -> dict[str, Any]:
        if self.secrets is None:
            raise ContextError("model_setup_disabled", "后台尚未单独授权模型配置与库外凭据目录。")
        if credential_confirmed is not True:
            raise ContextError(
                "credential_authorization_required", "请确认接口可信并允许后台将凭据用于此接口。"
            )
        if not isinstance(role, str) or role not in ROLES or not isinstance(api_key, str):
            raise ContextError("invalid_model_config", "模型角色或密钥字段无效，未回显内容。")
        if api_key:
            FileSecrets._key(api_key)
            if api_key in json.dumps(
                {"base_url": base_url, "model": model, "parameters": parameters}, ensure_ascii=False
            ):
                raise ContextError(
                    "invalid_model_config", "密钥不能写入地址、模型名或非秘密参数，未保存或回显。"
                )
        current = self.store.snapshot()["settings"].get("model_roles", {}).get(role)
        if current != expected_profile_id:
            raise ContextError("model_config_conflict", "默认配置已改变，请刷新后确认；未保存新密钥。")
        old = self.catalog.get(current) if current is not None else None
        if old and old["role"] != role:
            raise ContextError("model_config_changed", "默认角色与固定快照不匹配。")
        if not api_key and old is None:
            raise ContextError("credential_required", "首次配置此角色需要密钥；不会从其他项目读取。")
        created: list[str] = []

        def create() -> str:
            assert self.secrets is not None
            ref = self.secrets.put(api_key)
            created.append(ref)
            return ref

        try:
            self.catalog.configure(
                role=role,
                base_url=base_url,
                model=model,
                protocol=protocol,
                parameters=parameters,
                timeout=timeout,
                expected_profile_id=expected_profile_id,
                credential_ref=old["credential_ref"] if not api_key and old else None,
                create_credential=create if api_key else None,
            )
        except ContextError:
            # The store may have committed before reporting a failure. Never delete a key
            # referenced by any immutable profile (including already queued jobs).
            if created:
                try:
                    state = self.store.snapshot()
                    referenced = {p.get("credential_ref") for p in state.get("model_profiles", {}).values()}
                    for ref in created:
                        if ref not in referenced:
                            self.secrets.discard_new(ref)
                except ContextError:
                    raise ContextError(
                        "model_config_outcome_unknown",
                        "配置提交结果尚未确认，私有凭据保留；请刷新核对，不自动重试。",
                    ) from None
            raise
        return self.settings()
