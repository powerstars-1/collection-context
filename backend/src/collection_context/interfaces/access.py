"""Local-owner credential provisioning; only hashes persist in the private workspace."""

from __future__ import annotations

import json
import secrets
import uuid

from collection_context.application.contracts import ContextError, canonical_bytes, utc_now, valid_id
from collection_context.interfaces.security import AccessPolicy, Credential, authenticate_token
from collection_context.library.store import LibraryStore

ACCESS_FILE = ".context/访问规则.json"


class AccessRegistry:
    def __init__(self, store: LibraryStore):
        self.store = store

    def records(self) -> list[dict]:
        try:
            body = self.store.files.read(ACCESS_FILE, max_bytes=65_536)
        except ContextError as error:
            if error.code == "not_found":
                return []
            raise
        try:
            value = json.loads(body)
            if value["schema_version"] != 1 or value["workspace_id"] != self.store.workspace_id:
                raise ValueError
            records = value["credentials"]
            if not isinstance(records, list) or len(records) > 64:
                raise ValueError
            for record in records:
                if set(record) != {"principal", "label", "secret_sha256", "permissions", "created_at"}:
                    raise ValueError
                Credential(record["principal"], record["secret_sha256"], frozenset(record["permissions"]))
            return records
        except (ValueError, TypeError, KeyError, ContextError):
            raise ContextError("invalid_access_config", "访问规则损坏或不兼容；拒绝沿用旧权限。") from None

    def credentials(self) -> list[Credential]:
        return [
            Credential(r["principal"], r["secret_sha256"], frozenset(r["permissions"]))
            for r in self.records()
        ]

    def _save(self, records: list[dict]) -> None:
        self.store.files.write(
            ACCESS_FILE,
            canonical_bytes(
                {"schema_version": 1, "workspace_id": self.store.workspace_id, "credentials": records}
            ),
            replace=True,
        )

    def create(self, label: str, *, ui: bool = False, manage: bool = False, add: bool = False) -> dict:
        if type(ui) is not bool or type(manage) is not bool or type(add) is not bool or manage and not ui:
            raise ContextError("invalid_argument", "管理口令需要显式同时开启页面权限。")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 for c in label):
            raise ContextError("invalid_argument", "访问口令名称须为1～80字符。")
        with self.store.writer():
            records = self.records()
            if len(records) >= 64:
                raise ContextError("credential_limit", "最多64个访问身份；请先撤销不再使用的口令。")
            if any(r["label"] == label.strip() for r in records):
                raise ContextError("credential_conflict", "此名称已存在，请换名称或撤销原口令。")
            token = "scc_" + secrets.token_urlsafe(32)
            credential = Credential.from_token(
                "p_" + uuid.uuid4().hex,
                token,
                permissions=frozenset(
                    {"collections:read", "ui:view", "ui:manage"}
                    if manage
                    else {"collections:read", "ui:view"}
                    if ui
                    else {"collections:read"}
                )
                | (frozenset({"collections:add"}) if add else frozenset()),
            )
            record = {
                "principal": credential.principal,
                "label": label.strip(),
                "secret_sha256": credential.secret_sha256,
                "permissions": sorted(credential.permissions),
                "created_at": utc_now(),
            }
            self._save([*records, record])
        return {
            "principal": credential.principal,
            "label": label.strip(),
            "permissions": record["permissions"],
            "token": token,
            "warning": "此访问口令只显示一次。请自行保存，不要粘贴到聊天或公共仓库。",
        }

    def authenticate(self, token: str | None) -> Credential:
        return authenticate_token(self.credentials(), token)

    def authorize_add(self, principal: str) -> None:
        valid_id(principal)
        if not any(
            c.principal == principal and "collections:add" in c.permissions for c in self.credentials()
        ):
            raise ContextError("permission_denied", "添加授权不存在或已撤销；未访问平台或调用模型。")

    def revoke(self, principal: str) -> dict:
        valid_id(principal)
        with self.store.writer():
            records = self.records()
            remaining = [r for r in records if r["principal"] != principal]
            if len(records) == len(remaining):
                raise ContextError("not_found", "此访问身份不存在。")
            self._save(remaining)
        return {"principal": principal, "revoked": True}

    def refresh(self, policy: AccessPolicy) -> None:
        credentials = self.credentials()
        if len({c.principal for c in credentials}) != len(credentials) or len(
            {c.secret_sha256 for c in credentials}
        ) != len(credentials):
            raise ContextError("invalid_access_config", "访问规则存在重复身份或令牌。")
        policy.credentials = {c.principal: c for c in credentials}
        policy._prune_sessions()
