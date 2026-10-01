"""Observe only the requested work's normal web response in our own browser profile."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from collection_context.application.contracts import ContextError
from collection_context.sources.account import Account, self_account
from collection_context.sources.collections import FolderBatch, folder_page
from collection_context.sources.douyin import ObservedItem, normalize_item
from collection_context.sources.links import parse_link
from collection_context.sources.pages import CreatorBatch, creator_page, work_page

if TYPE_CHECKING:
    from collection_context.infrastructure.browser import BrowserSession


class DouyinBrowserSource:
    def __init__(self, browser: BrowserSession):
        self.browser = browser

    @staticmethod
    def _json(response) -> dict[str, Any]:
        if response.status != 200:
            raise ContextError("source_response_rejected", "平台未返回数据，请在独立浏览器检查登录或验证。")
        declared = response.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > 8_000_000):
            raise ContextError("source_limit", "平台响应超过上限，未解析部分数据。")
        body = response.body()
        if len(body) > 8_000_000:
            raise ContextError("source_limit", "平台响应超过上限，未解析部分数据。")
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            raise ContextError(
                "source_shape_changed",
                "平台没有返回预期JSON，未当作空列表。",
                next_action="在独立浏览器检查登录或平台验证；完成后手动重试，持续异常时检查来源兼容性。",
            ) from None
        if not isinstance(data, dict):
            raise ContextError("source_shape_changed", "平台响应结构变化，未按猜测字段解析。")
        return data

    @staticmethod
    def _cancel(cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise ContextError("connection_cancelled", "已取消本次连接观察，未启动同步或模型。")

    def account(
        self, *, interactive: bool = False, timeout: float = 15, cancelled: Callable[[], bool] | None = None
    ) -> Account:
        if type(interactive) is not bool or type(timeout) not in {int, float} or not 1 <= timeout <= 300:
            raise ContextError("invalid_source_timeout", "登录等待时间或方式无效。")
        if self.browser.context is None:
            raise ContextError("browser_not_running", "请先启动自己的独立登录浏览器。")
        if interactive and self.browser.headless:
            raise ContextError("desktop_login_required", "此登录入口需要可见浏览器；无桌面登录路径仍需验证。")
        self._cancel(cancelled)
        page = self.browser.context.new_page()
        accounts: list[Account] = []
        failures: list[ContextError] = []

        def observed(response):
            parts = urlsplit(response.url)
            if parts.hostname != "www.douyin.com" or parts.path != "/aweme/v1/web/user/profile/self/":
                return
            try:
                accounts.append(self_account(self._json(response)))
            except ContextError as error:
                failures.append(error)
            except Exception:
                failures.append(ContextError("source_shape_changed", "本人账号响应未能解析。"))

        page.on("response", observed)
        page.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type == "media" else route.fallback(),
        )
        deadline = time.monotonic() + timeout
        opened = False
        try:
            page.goto(
                "https://www.douyin.com/user/self",
                wait_until="commit",
                timeout=min(30_000, timeout * 1000),
            )
            while time.monotonic() < deadline:
                self._cancel(cancelled)
                if accounts:
                    return accounts[-1]
                if interactive and not opened:
                    buttons = page.get_by_role("button", name="登录", exact=True)
                    if buttons.count():
                        try:
                            buttons.first.click(timeout=3000)
                        except Exception:
                            if page.is_closed():
                                raise ContextError(
                                    "source_login_cancelled", "独立登录窗口已关闭，未确认登录成功。"
                                ) from None
                            # The platform may already have opened its own login overlay, covering
                            # the header button. Leave the normal window available for the user;
                            # do not close it or click other controls to bypass an overlay.
                        opened = True
                if not interactive and failures:
                    raise failures[-1]
                page.wait_for_timeout(200)
            if failures:
                raise failures[-1]
            if page.get_by_text("登录", exact=True).count():
                raise ContextError(
                    "source_login_required",
                    "请在本产品独立浏览器完成抖音登录。",
                    next_action="运行连接入口完成登录；不会借用其他项目账号。",
                )
            raise ContextError("source_login_unknown", "平台未返回本人账号证明，当前登录状态未知。")
        except ContextError:
            raise
        except Exception:
            raise ContextError(
                "source_login_unavailable",
                "登录页面加载或交互失败；请检查独立浏览器，不绕过验证。",
                retryable=True,
            ) from None
        finally:
            if not page.is_closed():
                page.close()

    def fetch_collections(
        self,
        *,
        expected_account_ref: str,
        limit: int = 100,
        timeout: float = 60,
        cancelled: Callable[[], bool] | None = None,
    ) -> FolderBatch:
        """Observe normal self-page folder responses; never forge signed/private requests."""
        batch = FolderBatch(limit)
        if type(timeout) not in {int, float} or not 1 <= timeout <= 120:
            raise ContextError("invalid_source_timeout", "收藏夹等待时间无效。")
        self._cancel(cancelled)
        account = self.account(cancelled=cancelled) if cancelled is not None else self.account()
        if account.public()["account_ref"] != expected_account_ref:
            raise ContextError("source_account_changed", "账号与待发现范围不同，未读取收藏夹。")
        assert self.browser.context is not None
        page = self.browser.context.new_page()
        failures: list[ContextError] = []
        seen: set[str] = set()

        def observed(response):
            parts = urlsplit(response.url)
            if (
                parts.hostname != "www.douyin.com"
                or parts.path != "/aweme/v1/web/collects/list/"
                or batch.done
                or failures
            ):
                return
            params = parse_qs(parts.query, keep_blank_values=True)
            if (
                "sec_user_id" in params
                and params["sec_user_id"] != [account.sec_uid]
                or "user_id" in params
                and params["user_id"] != [account.uid]
            ):
                failures.append(ContextError("source_scope_mismatch", "收藏夹请求不是已确认账号。"))
                return
            cursors = params.get("cursor", [])
            if len(cursors) != 1 or not re.fullmatch(r"[0-9]{1,32}", cursors[0]):
                failures.append(ContextError("source_cursor_invalid", "收藏夹请求缺少明确游标。"))
                return
            if cursors[0] in seen:
                return
            try:
                batch.accept(folder_page(self._json(response)), cursors[0])
                seen.add(cursors[0])
            except ContextError as error:
                failures.append(error)
            except Exception:
                failures.append(ContextError("source_shape_changed", "收藏夹响应未能解析，未保存空成功。"))

        page.on("response", observed)
        page.route("**/*", lambda r: r.abort() if r.request.resource_type == "media" else r.fallback())
        deadline = time.monotonic() + timeout
        last_pages, scrolls = 0, 0
        try:
            page.goto(
                "https://www.douyin.com/user/self?showTab=favorite_collection",
                wait_until="domcontentloaded",
                timeout=min(30_000, timeout * 1000),
            )
            resolved = urlsplit(page.url)
            if (
                resolved.scheme != "https"
                or resolved.hostname != "www.douyin.com"
                or resolved.path != "/user/self"
            ):
                raise ContextError("source_scope_mismatch", "收藏夹页面跳到其他范围，未保存。")
            while time.monotonic() < deadline:
                self._cancel(cancelled)
                if failures:
                    raise failures[0]
                if batch.done:
                    break
                if batch.pages > last_pages and scrolls < 5:
                    last_pages = batch.pages
                    page.mouse.move(800, 700)
                    page.mouse.wheel(0, 1800)
                    scrolls += 1
                page.wait_for_timeout(200)
            if not batch.pages:
                raise ContextError(
                    "source_access_required",
                    "没有观察到收藏夹列表，不能判为0个；请检查独立登录或页面兼容性。",
                )
        except ContextError:
            raise
        except Exception:
            raise ContextError(
                "source_site_unavailable", "收藏夹页面未完成，未绕过平台验证。", retryable=True
            ) from None
        finally:
            page.close()
        self._cancel(cancelled)
        current = self.account(cancelled=cancelled) if cancelled is not None else self.account()
        if current.public()["account_ref"] != expected_account_ref:
            raise ContextError("source_account_changed", "发现期间账号改变，未登记这些收藏夹。")
        return batch

    def fetch_item(
        self, value: str, *, timeout: float = 40, cancelled: Callable[[], bool] | None = None
    ) -> ObservedItem:
        self._cancel(cancelled)
        link = parse_link(value)
        if link.kind == "creator":
            raise ContextError("source_scope_mismatch", "这是博主主页，请使用博主作品同步入口。")
        if not 1 <= timeout <= 60:
            raise ContextError("invalid_source_timeout", "页面等待时间无效。")
        if self.browser.context is None:
            raise ContextError("browser_not_running", "请先启动自己的独立登录浏览器。")
        page = self.browser.context.new_page()
        observed: list[dict[str, Any]] = []
        errors: list[ContextError] = []
        navigation_errors: list[ContextError] = []

        def route_request(route):
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                try:
                    parse_link(request.url)
                except ContextError:
                    navigation_errors.append(
                        ContextError("unsafe_source_redirect", "作品跳转到不支持的页面，已停止。")
                    )
                    route.abort()
                    return
            # The source metadata workflow does not need autoplayed video/audio downloads.
            if request.resource_type == "media":
                route.abort()
            else:
                route.fallback()

        def response_observed(response):
            parts = urlsplit(response.url)
            if parts.hostname != "www.douyin.com" or parts.path != "/aweme/v1/web/aweme/detail/":
                return
            if len(observed) >= 8 or errors:
                return
            try:
                data = self._json(response)
                if type(data.get("status_code")) is not int or data["status_code"] != 0:
                    raise ContextError(
                        "source_access_required", "平台未允许读取；请登录或完成页面验证后再试。"
                    )
                item = data.get("aweme_detail")
                if isinstance(item, dict):
                    observed.append(item)
            except ContextError as error:
                errors.append(error)
            except Exception:
                errors.append(ContextError("source_shape_changed", "作品响应未按预期解析，未存空正文。"))

        page.route("**/*", route_request)
        page.on("response", response_observed)
        deadline = time.monotonic() + timeout
        try:
            page.goto(link.url, wait_until="domcontentloaded", timeout=min(30_000, timeout * 1000))
            resolved = parse_link(page.url)
            if resolved.kind != "item":
                raise ContextError("source_link_unresolved", "短链接尚未定位到具体作品，请提供完整作品链接。")
            if link.identity is not None and resolved.identity != link.identity:
                raise ContextError("source_identity_mismatch", "作品页面身份发生变化，未同步错误内容。")
            while time.monotonic() < deadline:
                self._cancel(cancelled)
                for raw in observed:
                    if raw.get("aweme_id") == resolved.identity:
                        return normalize_item(raw, expected_id=resolved.identity)
                if navigation_errors:
                    raise navigation_errors[0]
                if errors:
                    raise errors[0]
                page.wait_for_timeout(100)
            raise ContextError(
                "source_access_required",
                "未取得指定作品详情；请在独立浏览器检查登录、验证或作品可见性。",
                next_action="检查自己的抖音登录页面后手动重试，不导入其他项目账户。",
            )
        except ContextError:
            raise
        except Exception:
            if navigation_errors:
                raise navigation_errors[0] from None
            raise ContextError(
                "source_site_unavailable", "作品页面加载失败；未绕过平台验证。", retryable=True
            ) from None
        finally:
            page.close()

    def fetch_creator(self, value: str, *, limit: int = 5, timeout: float = 60) -> CreatorBatch:
        link = parse_link(value)
        if link.kind != "creator" or link.identity is None:
            raise ContextError(
                "source_scope_mismatch", "博主作品同步需要完整博主主页，不能从单作品自动扩展。"
            )
        creator_id = link.identity
        batch = CreatorBatch(limit)
        if type(timeout) not in {int, float} or not 1 <= timeout <= 120:
            raise ContextError("invalid_source_timeout", "作品列表等待时间无效。")
        if self.browser.context is None:
            raise ContextError("browser_not_running", "请先启动自己的独立登录浏览器。")
        page = self.browser.context.new_page()
        failures: list[ContextError] = []
        seen_requests: set[str] = set()

        def observed(response):
            parts = urlsplit(response.url)
            if (
                parts.hostname != "www.douyin.com"
                or parts.path != "/aweme/v1/web/aweme/post/"
                or batch.done
                or failures
            ):
                return
            params = parse_qs(parts.query)
            if params.get("sec_user_id") != [creator_id]:
                failures.append(
                    ContextError("source_scope_mismatch", "网页请求不是指定博主，未混入推荐内容。")
                )
                return
            cursors = params.get("max_cursor", [])
            if len(cursors) != 1 or not cursors[0].isdigit():
                failures.append(ContextError("source_cursor_invalid", "网页未给出有效分页起点。"))
                return
            if cursors[0] in seen_requests:
                return
            try:
                normalized = creator_page(self._json(response), creator_id)
                batch.accept(normalized, cursors[0])
                seen_requests.add(cursors[0])
            except ContextError as error:
                failures.append(error)
            except Exception:
                failures.append(ContextError("source_shape_changed", "作品列表未能解析，未作为空列表提交。"))

        page.on("response", observed)
        page.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type == "media" else route.fallback(),
        )
        deadline = time.monotonic() + timeout
        scrolls = 0
        last_pages = 0
        try:
            page.goto(link.url, wait_until="domcontentloaded", timeout=min(30_000, timeout * 1000))
            resolved = parse_link(page.url)
            if resolved.kind != "creator" or resolved.identity != link.identity:
                raise ContextError("source_scope_mismatch", "主页身份变化，未同步其他博主。")
            while time.monotonic() < deadline:
                if failures:
                    raise failures[0]
                if batch.done:
                    return batch
                if batch.pages > last_pages and scrolls < 5:
                    last_pages = batch.pages
                    page.mouse.move(800, 700)
                    page.mouse.wheel(0, 1800)
                    scrolls += 1
                page.wait_for_timeout(250)
            if batch.pages:
                # Successful pages may still be useful, but partial coverage is explicit.
                return batch
            raise ContextError(
                "source_access_required", "未取得作品列表，不能判为0作品；请检查独立浏览器登录或验证。"
            )
        except ContextError:
            raise
        except Exception:
            raise ContextError(
                "source_site_unavailable", "博主主页加载失败；没有继续猜测分页或绕过验证。", retryable=True
            ) from None
        finally:
            page.close()

    def fetch_self(
        self,
        kind: str,
        *,
        expected_account_ref: str,
        collection_id: str | None = None,
        limit: int = 5,
        timeout: float = 60,
    ) -> CreatorBatch:
        """Observe normal self-page requests, bound to a positively verified account.

        Endpoint/query contracts are compatibility candidates, not real-login proof.
        No signed requests, cookie import, hidden account switching or caller-provided API URL.
        """
        if kind not in {"liked", "saved", "collection"}:
            raise ContextError("invalid_source_scope", "本人同步只支持喜欢、收藏和指定收藏夹。")
        if kind == "collection":
            if not isinstance(collection_id, str) or not re.fullmatch(r"[0-9]{1,32}", collection_id):
                raise ContextError("invalid_source_scope", "指定收藏夹需要平台稳定数字身份。")
        elif collection_id is not None:
            raise ContextError("invalid_source_scope", "喜欢与全收藏不能携带收藏夹范围。")
        if type(timeout) not in {int, float} or not 1 <= timeout <= 120:
            raise ContextError("invalid_source_timeout", "列表等待时间无效。")
        batch = CreatorBatch(limit)
        account = self.account()
        if account.public()["account_ref"] != expected_account_ref:
            raise ContextError("source_account_changed", "登录账号与已确认同步范围不同，未读取该账号列表。")
        endpoint, cursor_field = {
            "liked": ("/aweme/v1/web/aweme/favorite/", "max_cursor"),
            "saved": ("/aweme/v1/web/aweme/listcollection/", "cursor"),
            "collection": ("/aweme/v1/web/collects/video/list/", "cursor"),
        }[kind]
        url = "https://www.douyin.com/user/self?showTab=" + (
            "like" if kind == "liked" else "favorite_collection"
        )
        if collection_id:
            url += "&collects_id=" + collection_id
        assert self.browser.context is not None
        page = self.browser.context.new_page()
        failures: list[ContextError] = []
        seen: set[str] = set()

        def observed(response):
            parts = urlsplit(response.url)
            if parts.hostname != "www.douyin.com" or parts.path != endpoint or batch.done or failures:
                return
            params = parse_qs(parts.query, keep_blank_values=True)
            if (
                kind == "liked"
                and params.get("sec_user_id") != [account.sec_uid]
                or "sec_user_id" in params
                and params["sec_user_id"] != [account.sec_uid]
                or kind == "collection"
                and params.get("collects_id") != [collection_id]
                or kind == "saved"
                and "collects_id" in params
            ):
                failures.append(
                    ContextError("source_scope_mismatch", "列表请求不是已确认账号或收藏夹，未合并其他范围。")
                )
                return
            cursors = params.get(cursor_field, [])
            if len(cursors) != 1 or not re.fullmatch(r"[0-9]{1,32}", cursors[0]):
                failures.append(ContextError("source_cursor_invalid", "列表请求没有明确分页起点。"))
                return
            if cursors[0] in seen:
                return
            try:
                batch.accept(work_page(self._json(response), cursor_field=cursor_field), cursors[0])
                seen.add(cursors[0])
            except ContextError as error:
                failures.append(error)
            except Exception:
                failures.append(ContextError("source_shape_changed", "本人列表响应未能解析，未当作空列表。"))

        page.on("response", observed)
        page.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type == "media" else route.fallback(),
        )
        deadline = time.monotonic() + timeout
        last_pages, scrolls = 0, 0
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=min(30_000, timeout * 1000))
            resolved = urlsplit(page.url)
            if (
                resolved.scheme != "https"
                or resolved.hostname != "www.douyin.com"
                or resolved.path != "/user/self"
            ):
                raise ContextError("source_scope_mismatch", "本人页面跳转到了其他范围，未保存列表。")
            while time.monotonic() < deadline:
                if failures:
                    raise failures[0]
                if batch.done:
                    break
                if batch.pages > last_pages and scrolls < 5:
                    last_pages = batch.pages
                    page.mouse.move(800, 700)
                    page.mouse.wheel(0, 1800)
                    scrolls += 1
                page.wait_for_timeout(250)
            if not batch.pages:
                raise ContextError(
                    "source_access_required", "未观察到所选列表，不能判为0条；请检查独立登录或页面兼容性。"
                )
        except ContextError:
            raise
        except Exception:
            raise ContextError(
                "source_site_unavailable", "本人页面加载失败；未绕过验证。", retryable=True
            ) from None
        finally:
            page.close()
        # Account switching during observation cannot mix one user's list into another user's scope.
        if self.account().public()["account_ref"] != expected_account_ref:
            raise ContextError("source_account_changed", "观察期间登录账号已改变，未提交列表。")
        return batch
