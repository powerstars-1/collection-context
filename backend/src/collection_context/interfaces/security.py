"""Personal HTTP access policy; tokens are hashed, bounded and never logged."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from collection_context.application.contracts import ContextError, valid_id


@dataclass(frozen=True)
class Credential:
    principal: str
    secret_sha256: str = field(repr=False)
    permissions: frozenset[str] = frozenset({"collections:read"})

    def __post_init__(self):
        valid_id(self.principal)
        if not re.fullmatch(r"[a-f0-9]{64}", self.secret_sha256):
            raise ContextError("invalid_access_config", "访问凭据须保存 SHA-256 摘要，不保存明文。")
        if not self.permissions or self.permissions - {
            "collections:read",
            "collections:add",
            "ui:view",
            "ui:manage",
        }:
            raise ContextError("invalid_access_config", "访问规则包含不支持的权限类型。")
        if "collections:add" in self.permissions and "collections:read" not in self.permissions:
            raise ContextError("invalid_access_config", "添加权限需要同时授予读库权限。")
        if "ui:manage" in self.permissions and not {"collections:read", "ui:view"} <= self.permissions:
            raise ContextError("invalid_access_config", "管理权限需要同时授予读库和页面权限。")

    @classmethod
    def from_token(
        cls, principal: str, token: str, *, permissions: frozenset[str] | None = None
    ) -> Credential:
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ContextError("invalid_access_config", "访问令牌须为32～256字符的无空格 ASCII 值。")
        return cls(
            principal,
            hashlib.sha256(token.encode()).hexdigest(),
            permissions or frozenset({"collections:read"}),
        )


@dataclass
class Session:
    principal: str
    expires_at: float
    csrf: str = field(repr=False)


def authenticate_token(credentials: list[Credential], token: str | None) -> Credential:
    """Transport-neutral token comparison; never echoes input or stores the bearer secret."""
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 256
        or not token.isascii()
        or any(c.isspace() for c in token)
    ):
        raise ContextError("authentication_required", "产品访问令牌无效。")
    received = hashlib.sha256(token.encode()).hexdigest()
    selected = None
    for credential in credentials:
        if hmac.compare_digest(received, credential.secret_sha256):
            selected = credential
    if selected is None:
        raise ContextError("authentication_required", "产品访问令牌无效或已撤销。")
    return selected


class AccessPolicy:
    def __init__(
        self,
        origin: str,
        credentials: list[Credential],
        *,
        remote: bool = False,
        local_ui: bool = False,
        requests_per_minute: int = 120,
        session_seconds: int = 3600,
    ):
        try:
            parsed = urlsplit(origin)
            valid_origin = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
                and (parsed.port is not None or remote)
            )
            if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and not remote:
                pass
            elif not remote or parsed.scheme != "https":
                valid_origin = False
        except ValueError:
            valid_origin = False
        if not valid_origin:
            raise ContextError("invalid_access_config", "本机需明确回环地址和端口；远程需显式开启 HTTPS。")
        if type(local_ui) is not bool or local_ui and (remote or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ContextError("invalid_access_config", "免口令管理页仅用于本机回环地址。")
        if (
            (not credentials and not local_ui)
            or len(credentials) > 64
            or len({c.principal for c in credentials}) != len(credentials)
        ):
            raise ContextError("invalid_access_config", "访问凭据数量或身份重复。")
        if len({c.secret_sha256 for c in credentials}) != len(credentials):
            raise ContextError("invalid_access_config", "不能把同一令牌分配给不同身份。")
        if type(requests_per_minute) is not int or not 1 <= requests_per_minute <= 1000:
            raise ContextError("invalid_access_config", "请求上限须在1～1000之间。")
        if type(session_seconds) is not int or not 60 <= session_seconds <= 86400:
            raise ContextError("invalid_access_config", "页面会话期限无效。")
        self.origin, self.authority, self.remote = origin, parsed.netloc, remote
        self.local_ui = local_ui
        # Local page access is not a persistent Key and cannot authenticate a Bearer request.
        self.local_owner = Credential("local_ui_owner", "0" * 64,
            frozenset({"collections:read", "ui:view", "ui:manage"})) if local_ui else None
        self.credentials = {c.principal: c for c in credentials}
        self.limit, self.session_seconds = requests_per_minute, session_seconds
        self.sessions: dict[str, Session] = {}
        self.rates: OrderedDict[str, deque[float]] = OrderedDict()

    def authenticate(self, authorization: str | None) -> Credential:
        if not authorization or not authorization.startswith("Bearer "):
            raise ContextError("authentication_required", "请使用有效的产品访问令牌；不是模型 API Key。")
        return authenticate_token(list(self.credentials.values()), authorization[7:])

    def rate_allowed(self, identity: str, *, now: float | None = None, limit: int | None = None) -> bool:
        now = time.monotonic() if now is None else now
        queue = self.rates.pop(identity, deque())
        while queue and queue[0] <= now - 60:
            queue.popleft()
        allowed = len(queue) < (self.limit if limit is None else limit)
        if allowed:
            queue.append(now)
        self.rates[identity] = queue
        # No unbounded map controlled by remote client addresses.
        while len(self.rates) > 1024:
            self.rates.popitem(last=False)
        return allowed

    def create_session(self, credential: Credential) -> tuple[str, Session]:
        if "ui:view" not in credential.permissions:
            raise ContextError("permission_denied", "此令牌仅供 AI 读库，不能建立页面会话。")
        self._prune_sessions()
        if len(self.sessions) >= 128:
            raise ContextError("session_limit", "页面会话已达上限，请关闭旧会话或稍后重试。")
        token = secrets.token_urlsafe(32)
        session = Session(
            credential.principal, time.monotonic() + self.session_seconds, secrets.token_urlsafe(32)
        )
        self.sessions[token] = session
        return token, session

    def _prune_sessions(self) -> None:
        now = time.monotonic()
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if value.expires_at > now and (value.principal in self.credentials
                or self.local_owner is not None and value.principal == self.local_owner.principal)
        }

    def session(self, token: str | None) -> tuple[Credential, Session]:
        self._prune_sessions()
        value = self.sessions.get(token or "")
        if value is None:
            raise ContextError("authentication_required", "页面会话无效或已过期，请重新登录。")
        credential = self.local_owner if self.local_owner is not None and value.principal == self.local_owner.principal else self.credentials[value.principal]
        return credential, value

    def revoke(self, principal: str) -> None:
        self.credentials.pop(principal, None)
        self._prune_sessions()

    def public_session(self, credential: Credential, session: Session | None = None) -> dict[str, Any]:
        return {
            "principal": credential.principal,
            "permissions": sorted(credential.permissions),
            "csrf_token": session.csrf if session else None,
            "session_seconds": self.session_seconds,
            "local_ui": self.local_owner is not None and credential is self.local_owner,
        }
