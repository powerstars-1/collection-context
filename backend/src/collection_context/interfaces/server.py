"""Explicit opt-in server; remote binding requires TLS, never proxy-header trust."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.secrets import CredentialBackend, FileSecrets
from collection_context.infrastructure.system_secrets import SystemSecrets
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.library.store import LibraryStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收藏上下文：认证读接口与管理页（开发版）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, help="固定的库外运行依赖目录；仅由启动者配置")
    parser.add_argument("--origin", default="http://127.0.0.1:8787")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--allow-model-config", action="store_true")
    parser.add_argument("--credential-dir", type=Path)
    parser.add_argument("--credential-backend", choices=("private-file", "system"), default="private-file")
    parser.add_argument("--allow-source-connect", action="store_true")
    parser.add_argument("--browser-dir", type=Path)
    parser.add_argument("--source-connect-headless", action="store_true")
    args = parser.parse_args(argv)
    store = None
    model_secrets: CredentialBackend | None = None
    connection_runner = None
    try:
        if args.bind not in {"127.0.0.1", "::1", "localhost"} and not args.remote:
            raise ContextError("permission_denied", "默认只绑定回环地址。远程模式须显式启用并配置 TLS。")
        if args.remote and (not args.tls_cert or not args.tls_key):
            raise ContextError("https_required", "远程模式必须同时提供 TLS 证书和私钥。")
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ContextError("https_required", "证书与私钥必须同时配置。")
        if args.allow_model_config != (args.credential_dir is not None):
            raise ContextError("model_setup_disabled", "启用模型配置需同时明确授权及指定库外私有凭据目录。")
        if (
            args.allow_source_connect != (args.browser_dir is not None)
            or args.source_connect_headless
            and not args.allow_source_connect
        ):
            raise ContextError("connection_disabled", "页面连接需同时授权及固定库外独立浏览器目录。")
        if args.remote and args.allow_source_connect and not args.source_connect_headless:
            raise ContextError(
                "desktop_login_required", "远程模式不向服务器桌面弹登录窗口；只能显式启用无桌面验证。"
            )
        store = LibraryStore(args.workspace)
        registry = AccessRegistry(store)
        credentials = registry.credentials()
        if not credentials:
            raise ContextError("access_setup_required", "请先使用 access-key 命令创建产品访问口令。")
        policy = AccessPolicy(args.origin, credentials, remote=args.remote)
        parsed = urlsplit(args.origin)
        if (parsed.scheme == "https") != bool(args.tls_cert):
            raise ContextError("https_required", "外部地址协议与服务器 TLS 配置不一致。")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            import uvicorn
        except ImportError:
            raise ContextError("dependency_required", "请安装此产品的 web 可选依赖。") from None
        if args.allow_model_config:
            from collection_context.application.model_setup import separate_credentials

            separate_credentials(args.workspace, args.credential_dir)
            backend = SystemSecrets if args.credential_backend == "system" else FileSecrets
            model_secrets = (
                backend(args.credential_dir)
                if args.credential_dir.exists()
                else backend.initialize(args.credential_dir)
            )
        if args.allow_source_connect:
            from collection_context.application.connection_runner import ConnectionRunner

            connection_runner = ConnectionRunner(
                args.workspace,
                args.browser_dir,
                headless=args.source_connect_headless,
                runtime_dir=args.runtime_dir,
            )
        app = create_app(
            args.workspace,
            policy,
            refresh=lambda: registry.refresh(policy),
            model_secrets=model_secrets,
            connection_runner=connection_runner,
        )
        uvicorn.run(
            app,
            host=args.bind,
            port=port,
            proxy_headers=False,
            access_log=False,
            log_level="warning",
            limit_concurrency=20,
            timeout_keep_alive=5,
            ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
            ssl_keyfile=str(args.tls_key) if args.tls_key else None,
        )
        return 0
    except ContextError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    finally:
        if connection_runner is not None:
            connection_runner.close()
        if store is not None:
            store.close()
        if model_secrets is not None:
            model_secrets.close()


if __name__ == "__main__":
    raise SystemExit(main())
