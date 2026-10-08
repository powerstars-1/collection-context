"""Remember the connected account until logout or observed login failure."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from collection_context.application.contracts import ContextError, digest, utc_now, valid_id, validate_time
from collection_context.library.store import LibraryStore
from collection_context.sources.account import Account
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.collections import FolderBatch

# Folder discovery is optional account metadata, not proof of whether login succeeded.
FOLDER_ERRORS = frozenset({
    "source_site_unavailable", "source_access_required", "source_response_rejected",
    "source_shape_changed", "source_limit", "source_cursor_invalid",
    "source_cursor_mismatch", "source_cursor_stalled", "source_snapshot_changed",
})
LOGIN_FAILURES = frozenset({"source_login_required", "source_account_changed"})


class ConnectionCatalog:
    def __init__(self, store: LibraryStore):
        self.store = store

    @staticmethod
    def record_from_state(state: dict[str, Any]) -> dict | None:
        value = state.get("source_connection")
        if value is None:
            return None
        try:
            if not isinstance(value, dict) or set(value) != {
                "version",
                "account",
                "observed_at",
                "folders",
                "folders_observed",
                "complete",
                "error_code",
            }:
                raise ValueError
            body = {k: v for k, v in value.items() if k != "version"}
            if value["version"] != "n_" + digest(body):
                raise ValueError
            if (
                validate_time(value["observed_at"]) != value["observed_at"]
                or type(value["folders_observed"]) is not bool
                or type(value["complete"]) is not bool
            ):
                raise ValueError
            account = value["account"]
            if account is not None:
                if (
                    not isinstance(account, dict)
                    or set(account) != {"state", "account_ref", "display_name"}
                    or account["state"] != "authenticated"
                    or not valid_id(account["account_ref"]).startswith("s_")
                    or not isinstance(account["display_name"], str)
                    or not 1 <= len(account["display_name"]) <= 500
                    or "\x00" in account["display_name"]
                    or value["error_code"] is not None and value["error_code"] not in FOLDER_ERRORS
                ):
                    raise ValueError
            elif (
                value["folders"]
                or value["folders_observed"]
                or value["complete"]
                or not valid_id(value["error_code"])
            ):
                raise ValueError
            rows = value["folders"]
            if not isinstance(rows, list) or len(rows) > 100 or not value["folders_observed"] and rows:
                raise ValueError
            import re

            seen = set()
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"collection_id", "name"}
                    or not isinstance(row["collection_id"], str)
                    or not re.fullmatch(r"[0-9]{1,32}", row["collection_id"])
                    or row["collection_id"] in seen
                    or not isinstance(row["name"], str)
                    or not 1 <= len(row["name"]) <= 500
                    or "\x00" in row["name"]
                ):
                    raise ValueError
                seen.add(row["collection_id"])
            if value["complete"] and not value["folders_observed"]:
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, ContextError):
            raise ContextError("source_connection_corrupt", "连接证明损坏，未猜测允许本人来源。") from None

    @classmethod
    def status_from_state(cls, state: dict[str, Any], *, now: str | None = None) -> dict:
        value = cls.record_from_state(state)
        if value is None:
            return {
                "state": "not_connected",
                "version": None,
                "display_name": None,
                "observed_at": None,
                "expires_at": None,
                "folders": [],
                "folders_observed": False,
                "complete": False,
                "error_code": None,
                "model_requests": 0,
            }
        stamp = datetime.fromisoformat(value["observed_at"])
        current = datetime.fromisoformat(validate_time(now or utc_now()))
        # This is remembered connection state, not a 15-minute login lease.
        # Actual synchronization checks the browser account again before reading.
        fresh = current >= stamp
        account = value["account"]
        return {
            "state": "verified" if account and fresh else "stale" if account else "not_connected" if value["error_code"] == "source_logged_out" else "unverified",
            "version": value["version"],
            "display_name": account["display_name"] if account else None,
            "observed_at": value["observed_at"],
            "expires_at": None,
            "folders": value["folders"] if account and fresh else [],
            "folders_observed": bool(account and fresh and value["folders_observed"]),
            "complete": bool(account and fresh and value["complete"]),
            "error_code": value["error_code"],
            "model_requests": 0,
        }

    def status(self) -> dict:
        return self.status_from_state(self.store.snapshot())

    @classmethod
    def invalidate_in_state(cls, state: dict, account_ref: str, error_code: str) -> None:
        """A real sync login failure invalidates only the account it actually checked."""
        current = cls.record_from_state(state)
        if error_code not in LOGIN_FAILURES or not current or not current["account"] or current["account"]["account_ref"] != account_ref:
            return
        value = {"account": None, "observed_at": utc_now(), "folders": [],
                 "folders_observed": False, "complete": False, "error_code": error_code}
        state["source_connection"] = {"version": "n_" + digest(value), **value}
        for entry in state["settings"].get("sync_schedules", {}).values():
            entry["enabled"] = False
        state["settings"]["auto_sync"] = False

    def record(
        self,
        account: Account | None,
        *,
        expected_version: str | None,
        folders: FolderBatch | None = None,
        error_code: str | None = None,
    ) -> dict:
        if account is None and (not error_code or folders is not None):
            raise ContextError("invalid_argument", "未验证账号不能登记收藏夹。")
        if folders is not None and (not isinstance(folders, FolderBatch) or folders.pages < 1):
            raise ContextError("source_folder_unverified", "未观察到收藏夹页面，不登记空列表。")
        value = {
            "account": account.public() if account else None,
            "observed_at": datetime.now(UTC).isoformat(),
            "folders": list(folders.folders.values()) if folders else [],
            "folders_observed": folders is not None,
            "complete": folders.complete if folders else False,
            "error_code": error_code,
        }
        value = {"version": "n_" + digest(value), **value}
        self.record_from_state({"source_connection": value})

        def change(state):
            current = self.record_from_state(state)
            if (current["version"] if current else None) != expected_version:
                raise ContextError(
                    "source_connection_changed", "连接状态已改变，请刷新；未覆盖新的账号证明。"
                )
            state["source_connection"] = value
            if error_code == "source_logged_out" or error_code in LOGIN_FAILURES:
                for entry in state["settings"].get("sync_schedules", {}).values():
                    entry["enabled"] = False
                state["settings"]["auto_sync"] = False

        self.store.transact(change)
        return self.status()

    @classmethod
    def authorize(cls, state: dict, version: str, kind: str, collection_id: str | None) -> str:
        public = cls.status_from_state(state)
        value = cls.record_from_state(state)
        if public["state"] != "verified" or value is None:
            raise ContextError("source_login_required", "请先连接抖音账号。")
        if public["version"] != version:
            raise ContextError("source_connection_changed", "账号或收藏夹观察已更新，请刷新后确认。")
        if kind not in {"liked", "saved", "collection"}:
            raise ContextError("invalid_source_scope", "本人范围只支持喜欢、收藏或指定收藏夹。")
        if kind == "collection":
            if not public["folders_observed"] or collection_id not in {
                f["collection_id"] for f in public["folders"]
            }:
                raise ContextError(
                    "source_folder_unverified", "此收藏夹不在当前账号已观察列表中，未按手填身份登记。"
                )
        elif collection_id is not None:
            raise ContextError("invalid_source_scope", "喜欢和全收藏不能混入收藏夹身份。")
        return value["account"]["account_ref"]


class ConnectionWorkflow:
    def __init__(self, store: LibraryStore, source: DouyinBrowserSource):
        self.catalog, self.source = ConnectionCatalog(store), source

    def observe(
        self,
        *,
        interactive: bool = False,
        timeout: float = 15,
        discover: bool = False,
        limit: int = 100,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        if (
            type(interactive) is not bool
            or type(discover) is not bool
            or type(timeout) not in {int, float}
            or not 1 <= timeout <= 300
        ):
            raise ContextError("invalid_argument", "连接观察方式或等待时间无效。")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContextError("invalid_source_limit", "收藏夹发现上限为1至100个。")
        before = self.catalog.status()["version"]
        account = None
        try:
            with self.source.connection_observation():
                if cancelled is None:
                    account = self.source.account(interactive=interactive, timeout=timeout)
                else:
                    account = self.source.account(
                        interactive=interactive, timeout=timeout, cancelled=cancelled
                    )
                # Publish the actual account result immediately. A slow/failed optional
                # folder page must not hide an already successful login from the UI.
                before = self.catalog.record(account, expected_version=before)["version"]
                folders = None
                if discover:
                    if cancelled is None:
                        folders = self.source.fetch_collections(
                            expected_account_ref=account.public()["account_ref"], limit=limit
                        )
                    else:
                        folders = self.source.fetch_collections(
                            expected_account_ref=account.public()["account_ref"],
                            limit=limit,
                            cancelled=cancelled,
                        )
                DouyinBrowserSource._cancel(cancelled)
        except ContextError as error:
            if discover and account is not None and error.code in FOLDER_ERRORS:
                return self.catalog.record(account, expected_version=before, error_code=error.code)
            if error.code in LOGIN_FAILURES:
                self.catalog.record(None, expected_version=before, error_code=error.code)
            raise
        return self.catalog.record(account, expected_version=before, folders=folders)
