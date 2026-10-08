"""Bounded Pi SDK adapter, explicit credentials via stdin, no second service."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import sys
from pathlib import Path
from typing import Any

from collection_context.application.contracts import ContextError
from collection_context.processing.models import CloudModelClient, ModelResult, _usage


def node_runtime() -> str | None:
    explicit = os.environ.get("COLLECTION_CONTEXT_NODE")
    if explicit:
        return explicit if Path(explicit).is_file() and os.access(explicit, os.X_OK) else None
    found = shutil.which("node")
    if found:
        return found
    if sys.platform == "darwin":
        # launchd does not inherit an interactive shell's Homebrew/user PATH.
        for path in (Path.home()/".local/bin/node", Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node")):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


def invoke(value: dict[str, Any], timeout: float = 15) -> Any:
    worker = Path(__file__).with_name("pi_worker.mjs")
    node = node_runtime()
    if node is None or not worker.is_file():
        raise ContextError("model_runtime_missing", "模型组件未就绪，请安装完整运行环境。")
    try:
        result = subprocess.run(
            [node, str(worker)], input=json.dumps(value).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout,
            env={"PATH": os.environ.get("PATH", ""), "PI_TELEMETRY_DISABLED": "1"},
        )
        if result.returncode or len(result.stdout) > 4_000_000:
            raise ValueError
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ContextError("upstream_outcome_unknown", "模型响应超时，请核对用量后重试。", possibly_charged=True) from None
    except (OSError, ValueError):
        raise ContextError("pi_request_failed", "模型请求未完成，请检查服务、模型和网络。", possibly_charged=True) from None


class PiModelClient(CloudModelClient):
    def _chat(self, content: list[dict[str, Any]]) -> ModelResult:
        started = time.monotonic()
        result = invoke({"action": "complete", "profile": {
            "base_url": self.profile.base_url, "model": self.profile.model,
            "api_key": self.profile.api_key, "timeout": self.profile.timeout,
            "parameters": self.profile.parameters,
            "provider": self.profile.provider,
            "api": self.profile.api,
        }, "content": content}, self.profile.timeout + 5)
        return ModelResult(result["text"], result.get("actual_model"), self.profile.model,
            _usage(result.get("usage")), result.get("upstream_request_id"),
            time.monotonic() - started, result.get("finish_reason"), result["status"])
