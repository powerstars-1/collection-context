"""Positive account proof from the platform's normal self-profile response, not cookie guesses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from collection_context.application.contracts import ContextError, digest


@dataclass(frozen=True)
class Account:
    sec_uid: str = field(repr=False)
    uid: str = field(repr=False)
    display_name: str

    def public(self) -> dict[str, str]:
        return {
            "state": "authenticated",
            "account_ref": "s_" + digest(["douyin_account", self.uid])[:32],
            "display_name": self.display_name,
        }


def self_account(payload: Any) -> Account:
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise ContextError("source_shape_changed", "账号状态响应变化，未推断已登录。")
    if payload["status_code"] != 0:
        raise ContextError("source_login_required", "平台未确认账号登录，请在独立浏览器完成登录或验证。")
    user = payload.get("user")
    if not isinstance(user, dict):
        raise ContextError("source_login_required", "平台未返回本人账号，未推断已登录。")
    uid, sec_uid, name = user.get("uid"), user.get("sec_uid"), user.get("nickname")
    if (
        not isinstance(uid, str)
        or not re.fullmatch(r"[1-9][0-9]{0,31}", uid)
        or not isinstance(sec_uid, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", sec_uid)
        or not isinstance(name, str)
        or not 1 <= len(name) <= 500
        or "\x00" in name
    ):
        raise ContextError("source_login_required", "本人账号身份不完整，未推断已登录。")
    return Account(sec_uid, uid, name)
