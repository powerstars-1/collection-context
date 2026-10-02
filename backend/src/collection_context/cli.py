"""Original backend entry point; credentials and legacy services are not loaded."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from collection_context.application.agent_addition import AgentAdditionGateway
from collection_context.application.contracts import ARTIFACT_KINDS, ContextError, envelope
from collection_context.application.service import ContextService
from collection_context.infrastructure.browser import BrowserSession
from collection_context.infrastructure.files import SafeFiles
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.infrastructure.system_secrets import SystemSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog
from collection_context.sources.browser_source import DouyinBrowserSource
from collection_context.sources.session import source_session
from collection_context.workflows.connection import ConnectionCatalog, ConnectionWorkflow
from collection_context.workflows.extraction import ExtractionWorkflow
from collection_context.workflows.ingestion import IngestionWorkflow
from collection_context.workflows.jobs import JobManager
from collection_context.workflows.scheduling import ProcessingSchedule
from collection_context.workflows.source_schedule import SourceSchedule
from collection_context.workflows.synchronization import SynchronizationWorkflow
from collection_context.workflows.worker import BackgroundWorker


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="收藏上下文：独立文件库与受控读取（开发版）")
    result.add_argument("--workspace", type=Path, required=True, help="新产品工作目录；不是旧 Obsidian 库")
    result.add_argument("--runtime-dir", type=Path, help="显式库外运行依赖目录；无效时不回退系统工具")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("runtime-options", help="只看固定软件安装清单、大小和许可，不下载、不登录")
    installation = commands.add_parser(
        "install-runtime", help="明确确认后安装固定运行组件；不授予同步或计费权限"
    )
    installation.add_argument(
        "--artifact", required=True, help="runtime-options 返回的固定安装项 id，不是 URL"
    )
    installation.add_argument("--confirm-install", action="store_true", help="确认下载及安装这一个软件组件")
    runtime_probe = commands.add_parser(
        "probe-runtime", help="明确启动新空白浏览器验证渲染，不访问抖音或模型"
    )
    runtime_probe.add_argument(
        "--browser-dir", type=Path, required=True, help="不存在的新探测目录；拒绝借用已有登录态"
    )
    runtime_probe.add_argument(
        "--headed", action="store_true", help="明确测试可见浏览器；仍只渲染本机空白样例"
    )
    media_probe = commands.add_parser(
        "probe-media-runtime", help="用原创本机样例验证已安装的媒体组件，不调用模型或来源"
    )
    media_probe.add_argument("--probe-dir", type=Path, required=True, help="不存在的新探测目录，须在库外")
    ocr_probe = commands.add_parser("probe-ocr-runtime", help="对明确提供的六张原创测试页做本机OCR，不联网")
    ocr_probe.add_argument("--probe-dir", type=Path, required=True)
    ocr_probe.add_argument("--image-dir", type=Path, required=True, help="库外的固定原创测试页目录")
    commands.add_parser("init", help="只初始化新空目录，不覆盖已有文件")
    commands.add_parser("upgrade-writer", help="显式升级早期开发库写锁；保留资料，不自动清除旧占用")
    commands.add_parser("rebuild-index", help="显式重建派生索引，不调用模型")
    legacy = commands.add_parser("bind-legacy-ref", help="库主人预览并关联旧m1引用，不读取或迁移旧库")
    legacy.add_argument("--legacy-ref", required=True)
    legacy.add_argument("--ref", required=True, help="已入库的稳定资料引用；需人工核对同一作品")
    legacy.add_argument("--preview-token", help="先预览；再次携带返回的标识并确认")
    legacy.add_argument("--confirm-binding", action="store_true", help="明确确认只创建这一个引用映射")
    edits = commands.add_parser("accept-edit", help="库主人核对正文或素材入口卡的外部编辑，不调用模型")
    edits.add_argument("--ref", required=True)
    edits.add_argument("--artifact", required=True, choices=(*ARTIFACT_KINDS, "entry"))
    edits.add_argument("--preview-token", help="先预览；确认时携带预览标识")
    edits.add_argument(
        "--confirm-edit", action="store_true", help="接纳预览修改；入口卡保存为备注，正文依赖按预览过期"
    )
    summary = commands.add_parser("refresh-summary", help="预览并确认仅从已保存正文更新总结；不重跑音视频")
    summary.add_argument("--ref", required=True)
    summary.add_argument("--preview-token")
    summary.add_argument("--idempotency-key")
    summary.add_argument("--confirm-fee", action="store_true", help="明确授权最多一次总结模型请求；只排队")
    access = commands.add_parser("access-key", help="本地建立产品访问口令；仅在本终端显示一次，不是模型 Key")
    access.add_argument("--name", required=True)
    access.add_argument("--ui", action="store_true", help="同时允许登录页面；不授予处理或计费权限")
    access.add_argument(
        "--add", action="store_true", help="单独允许AI登记单链接/查看自己的任务；不下载、不计费"
    )
    access.add_argument(
        "--manage", action="store_true", help="与--ui同时使用；授予库主人管理与确认处理权限，勿发给AI"
    )
    revoke = commands.add_parser("revoke-access", help="撤销产品访问口令，活动 HTTP 服务即时拒绝其会话")
    revoke.add_argument("--principal", required=True)
    search = commands.add_parser("search", help="关键词检索已有资料，不同步、不提取")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=3)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--version", help="继续搜索时携带上一页version")
    search.add_argument("--source-kind", action="append", dest="source_kinds")
    search.add_argument("--scope-id")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--time-basis")
    read = commands.add_parser("read", help="按受控引用读取；继续读取携带版本")
    read.add_argument("--ref", required=True)
    read.add_argument("--artifact", default="original")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--max-chars", type=int, default=4000)
    read.add_argument("--version")
    status = commands.add_parser("status", help="查看证据缺口，不重试任务")
    status.add_argument("--ref", required=True)
    commands.add_parser("model-settings", help="只显示非密钥的三角色配置，不测试、不发模型请求")
    model = commands.add_parser("configure-model", help="本地配置模型角色；不发请求、不开自动处理")
    model.add_argument("--role", choices=("audio", "vision", "summary"), required=True)
    model.add_argument("--base-url", required=True)
    model.add_argument("--model", required=True)
    model.add_argument("--protocol", choices=("chat", "chat_audio", "transcription"), default="chat")
    model.add_argument("--parameters", default="{}", help="受控模型参数JSON；不要包含密钥")
    credential = model.add_mutually_exclusive_group(required=True)
    credential.add_argument("--credential-ref", help="已存独立凭据的引用，不是API Key")
    credential.add_argument("--prompt-key", action="store_true", help="仅从交互终端无回显输入模型Key")
    model.add_argument("--credential-dir", type=Path, help="与库分开的固定凭据目录；秘密不导出")
    model.add_argument("--credential-backend", choices=("private-file", "system"), default="private-file")
    prepare = commands.add_parser("prepare-video", help="本地视频准备、登记输入；不请求模型")
    prepare.add_argument("--ref", required=True)
    prepare.add_argument("--input", type=Path, required=True)
    images = commands.add_parser("prepare-images", help="完整登记按顺序的原图；不请求模型")
    images.add_argument("--ref", required=True)
    images.add_argument("--image", type=Path, action="append", required=True)
    submit = commands.add_parser("submit-extraction", help="固定输入/模型/调用上限并排队；不发请求")
    submit.add_argument("--input-id", required=True)
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--max-calls", type=int, required=True)
    run = commands.add_parser("run-job", help="本地显式执行一个提取任务；会上传登记媒体并可能计费")
    run.add_argument("--job-id", required=True)
    run.add_argument("--credential-dir", type=Path, required=True)
    run.add_argument("--credential-backend", choices=("private-file", "system"), default="private-file")
    job = commands.add_parser("job-status", help="只查看本地拥有者任务，不派发、不重试")
    job.add_argument("--job-id", required=True)
    cancel = commands.add_parser("cancel-job", help="取消指定任务；已发出的请求仍可能产生费用")
    cancel.add_argument("--job-id", required=True)
    worker = commands.add_parser("worker", help="串行执行已授权任务；来源与模型权限分开，不重试阻塞任务")
    worker.add_argument("--credential-dir", type=Path, help="仅允许模型请求时必需；来源同步不读取模型秘密")
    worker.add_argument("--credential-backend", choices=("private-file", "system"), default="private-file")
    worker.add_argument(
        "--allow-source-sync", action="store_true", help="明确允许执行已排队固定来源同步，不授权模型费用"
    )
    worker.add_argument("--browser-dir", type=Path, help="仅来源同步使用的库外独立浏览器目录")
    worker.add_argument("--headed", action="store_true", help="有桌面时显示同步浏览器；不自动弹登录授权")
    worker.add_argument(
        "--allow-model-calls", action="store_true", help="确认上传已登记媒体并按任务上限调用模型"
    )
    worker.add_argument("--once", action="store_true", help="只检查一轮后退出")
    worker.add_argument("--poll-seconds", type=float, default=5)
    worker.add_argument("--max-jobs", type=int, default=1, help="每轮1至20条；始终串行，不是全库上限")
    commands.add_parser("processing-settings", help="只看自动边界、上限、暂停数量；不请求模型")
    auto = commands.add_parser("configure-auto", help="启用后新资料按固定策略提取；旧积压不会自动恢复")
    auto.add_argument("--enabled", choices=("yes", "no"), required=True)
    auto.add_argument("--allow-model-calls", action="store_true")
    auto.add_argument("--max-calls", type=int, default=0)
    auto.add_argument("--max-new-tasks", type=int, default=5, help="本次授权总新任务上限，不是每轮无限刷新")
    resume = commands.add_parser("resume-auto", help="先预览选择的暂停任务，再携带预览标识确认恢复")
    resume.add_argument("--job-id", action="append", required=True)
    resume.add_argument("--preview-token")
    resume.add_argument("--allow-model-calls", action="store_true")
    history = commands.add_parser("submit-history", help="明确选择1至20份已登记输入，原子排队历史批次")
    history.add_argument("--input-id", action="append", required=True)
    history.add_argument("--idempotency-key", required=True)
    history.add_argument("--max-calls", type=int, required=True, help="每条任务调用上限；总上限为条数乘此值")
    history.add_argument("--allow-model-calls", action="store_true")
    history_cancel = commands.add_parser("cancel-history", help="取消一个历史批次，保留已发出请求/用量")
    history_cancel.add_argument("--batch-id", required=True)
    queued_link = commands.add_parser("submit-link", help="登记一条抖音作品，等待来源后台；不访问平台或模型")
    queued_link.add_argument("--url", required=True)
    queued_link.add_argument("--download", action="store_true")
    queued_link.add_argument("--idempotency-key", required=True)
    queued_link.add_argument("--allow-source-sync", action="store_true")
    agent_add = commands.add_parser(
        "agent-add", help="使用环境中的独立添加口令登记单链接；不执行平台/模型请求"
    )
    agent_add.add_argument("--url", required=True)
    agent_add.add_argument("--idempotency-key", required=True)
    agent_job = commands.add_parser("agent-job", help="使用独立添加口令查看本身份的单链接任务")
    agent_job.add_argument("--job-id", required=True)
    add = commands.add_parser("add-link", help="用独立浏览器同步一条抖音作品；不调用模型")
    add.add_argument("--url", required=True, help="抖音作品完整链接或分享文字")
    add.add_argument(
        "--browser-dir", type=Path, required=True, help="库外专用浏览器目录；不能借用其他项目账户"
    )
    add.add_argument("--headed", action="store_true", help="有桌面环境时显示独立浏览器")
    add.add_argument("--download", action="store_true", help="显式下载原媒体并做本地准备；不发模型请求")
    add.add_argument("--idempotency-key", help="可选固定提交标识；带原标识恢复时先查看任务状态")
    login = commands.add_parser(
        "connect-douyin", help="显示本产品独立浏览器登录；只验证本人账号，不同步或计费"
    )
    login.add_argument("--browser-dir", type=Path, required=True)
    login.add_argument("--wait-seconds", type=int, default=180, help="等待本人登录，1至300秒")
    connection = commands.add_parser("connection-status", help="以正常本人资料响应确认登录，不读取喜欢或收藏")
    connection.add_argument("--browser-dir", type=Path, required=True)
    commands.add_parser(
        "connection-snapshot", help="只读本地已观察账号/收藏夹证明，不访问平台；过期不冒充在线"
    )
    folders = commands.add_parser(
        "discover-collections", help="观察本人收藏夹名称/身份/分页，不同步作品或调用模型"
    )
    folders.add_argument("--browser-dir", type=Path, required=True)
    folders.add_argument("--limit", type=int, default=100)
    folders.add_argument("--headed", action="store_true")
    creator = commands.add_parser(
        "sync-creator", help="本产品浏览器同步指定博主作品，默认最多5条、无模型请求"
    )
    creator.add_argument("--url", required=True)
    creator.add_argument("--browser-dir", type=Path, required=True)
    creator.add_argument("--limit", type=int, default=5)
    creator.add_argument("--download", action="store_true", help="下载本轮作品并准备原媒体，不发模型请求")
    creator.add_argument("--headed", action="store_true")
    scope = commands.add_parser("configure-sync", help="注册固定本人列表或博主范围；不访问平台或模型")
    scope.add_argument("--kind", choices=("liked", "saved", "collection", "creator"), required=True)
    scope.add_argument("--account-ref", help="connection-status返回的本人账号引用")
    scope.add_argument("--collection-id", help="指定收藏夹的稳定数字身份，不能用名称代替")
    scope.add_argument("--creator-url")
    scope.add_argument("--limit", type=int, default=5)
    scope.add_argument("--download", action="store_true")
    sync = commands.add_parser("submit-sync", help="显式登记手动同步批次，稳定幂等键防止重复任务")
    sync.add_argument("--config-id", required=True)
    sync.add_argument("--idempotency-key", required=True)
    commands.add_parser("sync-settings", help="只看定时范围、下次检查与受阻状态；不访问平台或模型")
    timer = commands.add_parser("configure-auto-sync", help="独立开关固定来源定时同步；不启用模型处理")
    timer.add_argument("--config-id", required=True)
    timer.add_argument("--enabled", choices=("yes", "no"), required=True)
    timer.add_argument("--allow-source-sync", action="store_true")
    timer.add_argument("--interval-minutes", type=int, default=60)
    timer.add_argument(
        "--reset-blocked", action="store_true", help="明确确认重新检查受阻来源；不改写旧失败记录"
    )
    return result


def local_media(path: Path) -> tuple[bytes, str]:
    mime = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower())
    if mime is None:
        raise ContextError("unsupported_media", "请选择受支持的视频或静态原图。")
    with SafeFiles(path.absolute().parent) as files:
        return files.read(path.name, max_bytes=128_000_000), mime


def no_model_authority(_: str) -> str:
    raise ContextError("processing_authorization_required", "来源同步未授权模型请求或秘密读取。")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    data: Any
    store = None
    secrets: CredentialBackend | None = None
    try:
        if args.command == "runtime-options":
            from collection_context.application.runtime_setup import runtime_options

            data = runtime_options()
        elif args.command == "install-runtime":
            from collection_context.application.runtime_setup import install_runtime

            if args.runtime_dir is None:
                raise ContextError("runtime_install_directory_required", "请明确指定库外的运行依赖目录。")
            data = install_runtime(
                args.runtime_dir,
                library_dir=args.workspace,
                artifact_id=args.artifact,
                installation_confirmed=args.confirm_install,
            )
        elif args.command == "probe-runtime":
            from collection_context.application.runtime_setup import probe_runtime

            if args.runtime_dir is None:
                raise ContextError("runtime_install_directory_required", "请明确指定库外的运行依赖目录。")
            data = probe_runtime(
                args.runtime_dir,
                library_dir=args.workspace,
                browser_dir=args.browser_dir,
                headless=not args.headed,
            )
        elif args.command == "probe-media-runtime":
            from collection_context.application.runtime_media_probe import probe_media_runtime

            if args.runtime_dir is None:
                raise ContextError("runtime_install_directory_required", "请明确指定库外的运行依赖目录。")
            data = probe_media_runtime(args.runtime_dir, library_dir=args.workspace, probe_dir=args.probe_dir)
        elif args.command == "probe-ocr-runtime":
            from collection_context.application.runtime_ocr_probe import probe_ocr_runtime

            if args.runtime_dir is None:
                raise ContextError("runtime_install_directory_required", "请明确指定库外的运行依赖目录。")
            data = probe_ocr_runtime(
                args.runtime_dir,
                library_dir=args.workspace,
                probe_dir=args.probe_dir,
                image_dir=args.image_dir,
            )
        elif args.command == "init":
            store = LibraryStore.initialize(args.workspace)
            data = {"initialized": True, "auto_sync": False, "auto_process": False}
        else:
            store = LibraryStore(args.workspace)
            service = ContextService(store)
            if args.command == "upgrade-writer":
                data = store.upgrade_writer()
            elif args.command == "rebuild-index":
                data = FileIndex(store).rebuild()
            elif args.command == "bind-legacy-ref":
                from collection_context.library.legacy_references import LegacyReferences

                aliases = LegacyReferences(store)
                if args.confirm_binding:
                    data = aliases.bind(args.legacy_ref, args.ref, preview_token=args.preview_token)
                elif args.preview_token is not None:
                    raise ContextError("confirmation_required", "携带预览标识仍须明确确认映射。")
                else:
                    data = aliases.preview(args.legacy_ref, args.ref)
            elif args.command == "accept-edit":
                from collection_context.application.library_management import LibraryManagement

                edits = LibraryManagement(store, authorize=lambda: None)
                if args.confirm_edit:
                    data = edits.accept_edit(
                        args.ref, artifact=args.artifact, preview_token=args.preview_token, confirmed=True
                    )
                elif args.preview_token is not None:
                    raise ContextError("confirmation_required", "携带预览标识仍须明确确认接纳修改。")
                else:
                    data = edits.preview_edit(args.ref, artifact=args.artifact)
            elif args.command == "refresh-summary":
                from collection_context.application.library_management import LibraryManagement

                library = LibraryManagement(store, authorize=lambda: None)
                if args.confirm_fee:
                    data = library.submit_summary(
                        args.ref,
                        preview_token=args.preview_token,
                        idempotency_key=args.idempotency_key,
                        fee_confirmed=True,
                    )
                elif args.preview_token is not None or args.idempotency_key is not None:
                    raise ContextError(
                        "processing_authorization_required", "携带预览或任务标识仍需明确费用授权。"
                    )
                else:
                    data = library.preview_summary(args.ref)
            elif args.command == "submit-link":
                from collection_context.workflows.addition import AdditionWorkflow

                data = AdditionWorkflow(store).submit(
                    url=args.url,
                    download=args.download,
                    idempotency_key=args.idempotency_key,
                    source_confirmed=args.allow_source_sync,
                )
            elif args.command == "access-key":
                data = AccessRegistry(store).create(args.name, ui=args.ui, manage=args.manage, add=args.add)
            elif args.command in {"agent-add", "agent-job"}:
                from collection_context.workflows.addition import AdditionWorkflow

                registry = AccessRegistry(store)
                credential = registry.authenticate(os.environ.get("COLLECTION_CONTEXT_ACCESS_TOKEN"))
                gateway = AgentAdditionGateway(
                    AdditionWorkflow(store, agent_authority=registry.authorize_add), credential.principal
                )
                action, arguments = (
                    ("add_collection", {"url": args.url, "idempotency_key": args.idempotency_key})
                    if args.command == "agent-add"
                    else ("get_job", {"job_id": args.job_id})
                )
                result = gateway.dispatch(action, arguments)
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["ok"] else 1
            elif args.command == "revoke-access":
                data = AccessRegistry(store).revoke(args.principal)
            elif args.command == "search":
                filters = {
                    key: getattr(args, key)
                    for key in ("source_kinds", "scope_id", "since", "until", "time_basis")
                    if getattr(args, key) is not None
                }
                data = service.search(
                    args.query, limit=args.limit, filters=filters, offset=args.offset, version=args.version
                )
            elif args.command == "read":
                data = service.read(
                    args.ref,
                    artifact=args.artifact,
                    offset=args.offset,
                    max_chars=args.max_chars,
                    version=args.version,
                )
            elif args.command == "model-settings":
                data = ModelCatalog(store).public_settings()
            elif args.command == "configure-model":
                try:
                    parameters = json.loads(args.parameters)
                except (ValueError, TypeError):
                    raise ContextError("invalid_model_config", "模型参数不是有效JSON；未保存。") from None
                credential_ref = args.credential_ref
                if args.prompt_key:
                    if args.credential_dir is None or not sys.stdin.isatty():
                        raise ContextError(
                            "interactive_credential_required",
                            "请在交互终端指定独立凭据目录，无回显输入Key；不要传命令行Key。",
                        )
                    backend = SystemSecrets if args.credential_backend == "system" else FileSecrets
                    secrets = (
                        backend(args.credential_dir)
                        if args.credential_dir.exists()
                        else backend.initialize(args.credential_dir)
                    )
                    ExtractionWorkflow(
                        store, secrets.get
                    )  # Reject overlap before reading/storing any secret.
                    credential_ref = secrets.put(getpass.getpass("模型API Key（不回显）："))
                identity = ModelCatalog(store).configure(
                    role=args.role,
                    base_url=args.base_url,
                    model=args.model,
                    protocol=args.protocol,
                    parameters=parameters,
                    credential_ref=credential_ref,
                )
                data = {
                    "profile_id": identity,
                    "settings": ModelCatalog(store).public_settings(),
                    "model_requests": 0,
                }
            elif args.command == "prepare-video":
                media, mime = local_media(args.input)
                if not mime.startswith("video/"):
                    raise ContextError("unsupported_media", "视频准备仅接受受支持的视频文件。")
                identity = PreparedInputs(store, runtime_dir=args.runtime_dir).prepare_video(
                    args.ref, media, mime_type=mime
                )
                data = {"input_id": identity, "model_requests": 0}
            elif args.command == "prepare-images":
                identity = PreparedInputs(store).prepare_images(
                    args.ref, [local_media(path) for path in args.image]
                )
                data = {"input_id": identity, "model_requests": 0}
            elif args.command == "submit-extraction":
                data = ExtractionWorkflow(store, lambda _: "unused-at-submission").submit(
                    args.input_id, idempotency_key=args.idempotency_key, max_calls=args.max_calls
                )
            elif args.command == "run-job":
                backend = SystemSecrets if args.credential_backend == "system" else FileSecrets
                secrets = backend(args.credential_dir)
                data = ExtractionWorkflow(store, secrets.get).run(args.job_id)
            elif args.command == "job-status":
                data = JobManager(store).get(args.job_id)
            elif args.command == "cancel-job":
                data = JobManager(store).cancel(args.job_id)
            elif args.command in {
                "processing-settings",
                "configure-auto",
                "resume-auto",
                "submit-history",
                "cancel-history",
            }:
                schedule = ProcessingSchedule(ExtractionWorkflow(store, lambda _: "not-used-at-submission"))
                if args.command == "processing-settings":
                    data = schedule.status()
                elif args.command == "configure-auto":
                    data = schedule.configure(
                        args.enabled == "yes",
                        allow_model_calls=args.allow_model_calls,
                        max_calls=args.max_calls,
                        max_new_tasks=args.max_new_tasks,
                    )
                elif args.command == "resume-auto":
                    data = (
                        schedule.resume(
                            args.job_id,
                            preview_token=args.preview_token,
                            allow_model_calls=args.allow_model_calls,
                        )
                        if args.preview_token
                        else schedule.resume_preview(args.job_id)
                    )
                elif args.command == "submit-history":
                    data = schedule.history(
                        args.input_id,
                        idempotency_key=args.idempotency_key,
                        max_calls=args.max_calls,
                        allow_model_calls=args.allow_model_calls,
                    )
                else:
                    data = schedule.cancel_history(args.batch_id)
            elif args.command in {"configure-sync", "submit-sync"}:
                workflow_sync = SynchronizationWorkflow(store)
                if args.command == "configure-sync":
                    data = workflow_sync.configure(
                        args.kind,
                        account_ref=args.account_ref,
                        collection_id=args.collection_id,
                        creator_url=args.creator_url,
                        limit=args.limit,
                        download=args.download,
                    )
                else:
                    data = workflow_sync.submit(args.config_id, idempotency_key=args.idempotency_key)
            elif args.command in {"sync-settings", "configure-auto-sync"}:
                schedule_sync = SourceSchedule(SynchronizationWorkflow(store))
                data = (
                    schedule_sync.status()
                    if args.command == "sync-settings"
                    else schedule_sync.configure(
                        args.config_id,
                        enabled=args.enabled == "yes",
                        allow_source_sync=args.allow_source_sync,
                        interval_minutes=args.interval_minutes,
                        reset_blocked=args.reset_blocked,
                    )
                )
            elif args.command == "worker":
                if not args.allow_model_calls and not args.allow_source_sync:
                    raise ContextError(
                        "processing_authorization_required", "请明确确认后台执行已排队任务的模型费用。"
                    )
                if args.allow_model_calls and args.credential_dir is None:
                    raise ContextError("credential_setup_required", "允许模型请求时须提供独立凭据目录。")
                if args.allow_source_sync and args.browser_dir is None:
                    raise ContextError("source_setup_required", "允许来源同步时须提供本产品独立浏览器目录。")
                if args.allow_model_calls:
                    backend = SystemSecrets if args.credential_backend == "system" else FileSecrets
                    secrets = backend(args.credential_dir)
                workflow = ExtractionWorkflow(store, secrets.get if secrets else no_model_authority)
                sync_workflow = (
                    SynchronizationWorkflow(
                        store,
                        lambda: source_session(
                            store, args.browser_dir, headless=not args.headed, runtime_dir=args.runtime_dir
                        ),
                        runtime_dir=args.runtime_dir,
                    )
                    if args.allow_source_sync
                    else None
                )
                stop = threading.Event()
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
                try:
                    for sig in previous:
                        signal.signal(sig, lambda *_: stop.set())
                    data = BackgroundWorker(
                        workflow, sync_workflow, addition_authority=AccessRegistry(store).authorize_add
                    ).serve(
                        allow_model_calls=args.allow_model_calls,
                        allow_source_sync=args.allow_source_sync,
                        once=args.once,
                        poll_seconds=args.poll_seconds,
                        max_jobs=args.max_jobs,
                        stop=stop,
                        emit=lambda event: print(json.dumps(envelope(event), ensure_ascii=False), flush=True),
                    )
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
            elif args.command == "connection-snapshot":
                data = ConnectionCatalog(store).status()
            elif args.command == "add-link":
                import uuid

                from collection_context.workflows.addition import AdditionWorkflow

                addition = AdditionWorkflow(
                    store,
                    lambda: source_session(
                        store, args.browser_dir, headless=not args.headed, runtime_dir=args.runtime_dir
                    ),
                    runtime_dir=args.runtime_dir,
                )
                job = addition.submit(
                    url=args.url,
                    download=args.download,
                    idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                    source_confirmed=True,
                )
                if job["state"] == "queued":
                    job = addition.run(job["id"])
                if job["state"] not in {"succeeded", "partial"}:
                    raise ContextError(
                        (job.get("error") or {}).get("code", "link_job_pending"),
                        "单条任务尚未完成，请查看原任务状态；未自动重试受阻任务。",
                    )
                data = {
                    **job["stages"]["link_import"]["result"]["result"],
                    "job_id": job["id"],
                    "state": job["state"],
                }
            elif args.command in {
                "connect-douyin",
                "connection-status",
                "discover-collections",
                "sync-creator",
            }:
                profile = args.browser_dir.absolute()
                root = store.files.root
                if profile.is_relative_to(root) or root.is_relative_to(profile):
                    raise ContextError("unsafe_login_profile", "登录目录必须与可导出资料库分开。")
                with BrowserSession(
                    profile,
                    headless=args.command != "connect-douyin" and not getattr(args, "headed", False),
                    **(
                        {"runtime_dir": args.runtime_dir, "library_dir": store.files.root}
                        if args.runtime_dir is not None
                        else {}
                    ),
                ) as browser:
                    source = DouyinBrowserSource(browser)
                    if args.command == "connect-douyin":
                        observed = ConnectionWorkflow(store, source).observe(
                            interactive=True, timeout=args.wait_seconds
                        )
                        data = {
                            **observed,
                            "account_ref": ConnectionCatalog.authorize(
                                store.snapshot(), observed["version"], "liked", None
                            ),
                            "private_sources_verified": False,
                            "model_requests": 0,
                        }
                    elif args.command == "connection-status":
                        observed = ConnectionWorkflow(store, source).observe()
                        data = {
                            **observed,
                            "account_ref": ConnectionCatalog.authorize(
                                store.snapshot(), observed["version"], "liked", None
                            ),
                            "private_sources_verified": False,
                            "model_requests": 0,
                        }
                    elif args.command == "discover-collections":
                        data = ConnectionWorkflow(store, source).observe(discover=True, limit=args.limit)
                    elif args.command == "sync-creator":
                        data = IngestionWorkflow(store, source, runtime_dir=args.runtime_dir).sync_creator(
                            args.url, limit=args.limit, download=args.download
                        )
            else:
                data = service.status(args.ref)
        output, status = envelope(data), 0
    except ContextError as error:
        output, status = envelope(error=error), 1
    finally:
        if secrets is not None:
            secrets.close()
        if store is not None:
            store.close()
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
