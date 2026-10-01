"""Authenticated bounded HTTP read API, over the same original service as CLI/MCP."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
from pathlib import Path
from typing import Any

from collection_context.application.agent_addition import AgentAdditionGateway
from collection_context.application.connection_runner import ConnectionRunner
from collection_context.application.contracts import ContextError, canonical_bytes, envelope
from collection_context.application.gateway import ReadGateway, http_status
from collection_context.application.management import ManagementService
from collection_context.application.service import ContextService
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.security import AccessPolicy
from collection_context.library.store import LibraryStore
from collection_context.workflows.addition import AdditionWorkflow

COOKIE_NAME = "context_session"
MAX_BODY = 65_536
MAX_RESPONSE = 256_000
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def decode_json(body: bytes) -> dict[str, Any]:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ContextError(
            "invalid_argument", "请输入有效的 JSON 对象，不允许重复字段或非有限数字。"
        ) from None


class HttpBoundary:
    def __init__(self, app, policy: AccessPolicy, refresh: Callable[[], None] | None = None):
        self.app, self.policy, self.refresh = app, policy, refresh
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {}
        for key, value in scope["headers"]:
            name = key.decode("latin-1").lower()
            if name in headers and name in {
                "host",
                "origin",
                "authorization",
                "content-length",
                "cookie",
                "x-csrf-token",
            }:
                await self.deny(send, "invalid_argument", "安全相关请求头重复。", 400)
                return
            headers[name] = value.decode("latin-1")
        if headers.get("host", "").lower() != self.policy.authority.lower():
            await self.deny(send, "forbidden_host", "此地址未被服务配置允许。", 403)
            return
        if self.policy.remote and scope.get("scheme") != "https":
            await self.deny(send, "https_required", "远程访问只允许 HTTPS。", 403)
            return
        if "origin" in headers and headers["origin"] != self.policy.origin:
            await self.deny(send, "forbidden_origin", "拒绝跨站访问。", 403)
            return
        if headers.get("sec-fetch-site") == "cross-site":
            await self.deny(send, "forbidden_origin", "拒绝跨站访问。", 403)
            return
        path, method = scope["path"], scope["method"]
        if self.refresh is not None:
            try:
                self.refresh()
            except ContextError:
                await self.deny(
                    send, "access_config_unavailable", "访问规则暂时不可读取，停止授权请求。", 503
                )
                return
        state = scope.setdefault("state", {})
        ip = (scope.get("client") or ("unknown", 0))[0]
        if not self.policy.rate_allowed("ip:" + ip, limit=240):
            await self.deny(send, "rate_limited", "请求过于频繁，请稍后再试。", 429)
            return
        if (
            path == "/v1/session"
            and method == "POST"
            and not self.policy.rate_allowed("login:" + ip, limit=8)
        ):
            await self.deny(send, "rate_limited", "登录尝试过于频繁，请稍后再试。", 429)
            return
        protected = path.startswith("/v1/") and not (path == "/v1/session" and method == "POST")
        if protected:
            try:
                session = None
                if "authorization" in headers:
                    credential = self.policy.authenticate(headers["authorization"])
                else:
                    cookies = SimpleCookie()
                    cookie_header = headers.get("cookie", "")
                    if (
                        sum(part.split("=", 1)[0].strip() == COOKIE_NAME for part in cookie_header.split(";"))
                        > 1
                    ):
                        raise ContextError("authentication_required", "页面会话字段重复。")
                    try:
                        cookies.load(cookie_header)
                    except CookieError:
                        raise ContextError("authentication_required", "页面会话格式无效。") from None
                    cookie = cookies.get(COOKIE_NAME)
                    credential, session = self.policy.session(cookie.value if cookie else None)
                    if method not in {"GET", "HEAD"} and not hmac.compare_digest(
                        headers.get("x-csrf-token", "").encode(), session.csrf.encode()
                    ):
                        raise ContextError("csrf_required", "页面请求缺少有效的会话校验。")
                if path.startswith("/v1/collections") and "collections:read" not in credential.permissions:
                    raise ContextError("permission_denied", "此凭据没有读库权限。")
                if (
                    path == "/v1/collections" and method == "POST" or path.startswith("/v1/jobs")
                ) and "collections:add" not in credential.permissions:
                    raise ContextError("permission_denied", "此入口需要主人单独授予添加权限；默认 AI 只读。")
                if path.startswith("/v1/management") and (
                    "ui:manage" not in credential.permissions or session is None
                ):
                    raise ContextError(
                        "permission_denied", "管理操作需要库主人管理口令的页面会话；AI读口令不具备此权限。"
                    )
                if not self.policy.rate_allowed("principal:" + credential.principal):
                    raise ContextError("rate_limited", "此凭据请求过于频繁。")
                state.update(credential=credential, session=session)
            except ContextError as error:
                code = (
                    429
                    if error.code == "rate_limited"
                    else 403
                    if error.code in {"permission_denied", "csrf_required"}
                    else 401
                )
                await self.deny(send, error.code, error.message, code)
                return
        if path.startswith("/v1/") and method == "POST":
            if headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                await self.deny(send, "invalid_argument", "此接口需要 application/json。", 415)
                return
        try:
            declared = int(headers.get("content-length", "0"))
            if not 0 <= declared <= MAX_BODY:
                raise ValueError
        except ValueError:
            await self.deny(send, "request_too_large", "请求体超过大小限制或长度无效。", 413)
            return
        # Enforce actual bytes too; a false/absent Content-Length cannot bypass the cap.
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY:
                        await self.deny(send, "request_too_large", "请求体超过大小限制。", 413)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self.deny(send, "request_timeout", "读取请求超时。", 408)
            return
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def secured_send(message):
            if message["type"] == "http.response.start":
                present = {key.lower() for key, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (key.encode(), value.encode())
                    for key, value in SECURITY_HEADERS.items()
                    if key.encode() not in present
                ]
            await send(message)

        if protected and self.active >= 2:
            await self.deny(send, "concurrency_limited", "正在处理其他请求，请稍后重试。", 429)
            return
        self.active += int(protected)
        try:
            await self.app(scope, buffered_receive, secured_send)
        finally:
            self.active -= int(protected)

    @staticmethod
    async def deny(send, code: str, message: str, status: int):
        body = canonical_bytes(envelope(error=ContextError(code, message)))
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]
        headers += [(key.encode(), value.encode()) for key, value in SECURITY_HEADERS.items()]
        if status == 429:
            headers.append((b"retry-after", b"60"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def create_app(
    workspace: Path,
    policy: AccessPolicy,
    *,
    refresh: Callable[[], None] | None = None,
    model_secrets: FileSecrets | None = None,
    connection_runner: ConnectionRunner | None = None,
):
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, Response
        from starlette.concurrency import run_in_threadpool
        from starlette.exceptions import HTTPException
    except ImportError:
        raise ContextError("dependency_required", "请安装此产品的 web 可选依赖。") from None
    store = LibraryStore(workspace)
    gateway = ReadGateway(ContextService(store))
    addition = AdditionWorkflow(store, agent_authority=AccessRegistry(store).authorize_add)
    try:
        management = ManagementService(
            store, model_secrets=model_secrets, connection_runner=connection_runner
        )
    except BaseException:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            if connection_runner is not None:
                await run_in_threadpool(connection_runner.close)
            store.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(HttpBoundary, policy=policy, refresh=refresh)

    @app.get("/")
    @app.get("/connect")
    @app.get("/activity")
    @app.get("/settings")
    @app.get("/access")
    async def shell():
        return Response(
            files("collection_context.interfaces").joinpath("assets/index.html").read_bytes(),
            media_type="text/html",
        )

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return Response(
            files("collection_context.interfaces").joinpath("assets", name).read_bytes(),
            media_type="text/javascript" if name == "app.js" else "text/css",
        )

    def result_response(result, status: int | None = None):
        if len(canonical_bytes(result)) > MAX_RESPONSE:
            return JSONResponse(
                envelope(error=ContextError("response_too_large", "结果超过大小上限，请缩小读取范围。")),
                status_code=413,
            )
        return JSONResponse(result, status_code=status if status is not None else http_status(result))

    @app.exception_handler(ContextError)
    async def business_error(_, error: ContextError):
        result = envelope(error=error)
        return result_response(
            result, 401 if error.code == "authentication_required" else http_status(result)
        )

    @app.exception_handler(HTTPException)
    async def not_found(_, error):
        return result_response(
            envelope(error=ContextError("not_found", "此接口不存在或方法不支持。")), error.status_code
        )

    @app.get("/health")
    async def health():
        return result_response(envelope({"service": "collection-context", "development_candidate": True}))

    # Bind the locally imported type for FastAPI's annotation resolution without making
    # optional web dependencies mandatory for the standard-library core.
    async def body(request):
        if request.query_params:
            raise ContextError("invalid_argument", "此接口不接收查询字符串参数。")
        return decode_json(await request.body())

    async def search(request):
        result = await run_in_threadpool(gateway.dispatch, "search_collections", await body(request))
        return result_response(result)

    search.__annotations__["request"] = Request
    app.add_api_route("/v1/collections/search", search, methods=["POST"])

    async def add_collection(request):
        agent = AgentAdditionGateway(addition, request.state.credential.principal)
        result = await run_in_threadpool(agent.dispatch, "add_collection", await body(request))
        return result_response(result, 202 if result["ok"] and result["data"]["state"] == "queued" else None)

    add_collection.__annotations__["request"] = Request
    app.add_api_route("/v1/collections", add_collection, methods=["POST"])

    async def get_job(job_id: str, request):
        if request.query_params:
            raise ContextError("invalid_argument", "任务状态不接收查询参数。")
        agent = AgentAdditionGateway(addition, request.state.credential.principal)
        return result_response(await run_in_threadpool(agent.dispatch, "get_job", {"job_id": job_id}))

    get_job.__annotations__["request"] = Request
    app.add_api_route("/v1/jobs/{job_id}", get_job, methods=["GET"])

    async def read(request):
        result = await run_in_threadpool(gateway.dispatch, "read_collection", await body(request))
        return result_response(result)

    read.__annotations__["request"] = Request
    app.add_api_route("/v1/collections/read", read, methods=["POST"])

    async def list_items(request):
        return result_response(
            await run_in_threadpool(gateway.dispatch, "list_collections", await body(request))
        )

    list_items.__annotations__["request"] = Request
    app.add_api_route("/v1/collections/list", list_items, methods=["POST"])

    async def overview(request):
        if request.query_params:
            raise ContextError("invalid_argument", "此接口不接收查询参数。")
        return result_response(await run_in_threadpool(gateway.dispatch, "library_overview", {}))

    overview.__annotations__["request"] = Request
    app.add_api_route("/v1/collections/overview", overview, methods=["GET"])

    async def status(material_ref: str, request):
        if request.query_params:
            raise ContextError("invalid_argument", "此接口不接收查询字符串参数。")
        return result_response(
            await run_in_threadpool(gateway.dispatch, "collection_status", {"material_ref": material_ref})
        )

    status.__annotations__["request"] = Request
    app.add_api_route("/v1/collections/{material_ref}/status", status, methods=["GET"])

    async def manage(action: str, request):
        return result_response(await run_in_threadpool(management.dispatch, action, await body(request)))

    manage.__annotations__["request"] = Request
    app.add_api_route("/v1/management/{action}", manage, methods=["POST"])

    async def login(request):
        value = await body(request)
        if set(value) != {"token"} or not isinstance(value["token"], str):
            raise ContextError("invalid_argument", "页面登录只接收产品访问令牌。")
        credential = policy.authenticate("Bearer " + value["token"])
        key, session = policy.create_session(credential)
        response = result_response(envelope(policy.public_session(credential, session)))
        response.set_cookie(
            COOKIE_NAME,
            key,
            max_age=policy.session_seconds,
            httponly=True,
            secure=policy.remote or policy.origin.startswith("https:"),
            samesite="strict",
            path="/",
        )
        return response

    login.__annotations__["request"] = Request
    app.add_api_route("/v1/session", login, methods=["POST"])

    async def session_info(request):
        if request.query_params:
            raise ContextError("invalid_argument", "会话接口不接收查询参数。")
        return result_response(
            envelope(policy.public_session(request.state.credential, request.state.session))
        )

    session_info.__annotations__["request"] = Request
    app.add_api_route("/v1/session", session_info, methods=["GET"])

    async def logout(request):
        await body(request)
        token = request.cookies.get(COOKIE_NAME)
        if token:
            policy.sessions.pop(token, None)
        response = result_response(envelope({"logged_out": True}))
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=policy.remote, samesite="strict")
        return response

    logout.__annotations__["request"] = Request
    app.add_api_route("/v1/session/logout", logout, methods=["POST"])
    return app
