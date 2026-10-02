"""Strict transport-neutral dispatch, restricted to the public read use cases."""

from __future__ import annotations

from typing import Any

from collection_context.application.contracts import ContextError, canonical_bytes, envelope
from collection_context.application.service import ContextService

READ_TOOLS = frozenset({"search_collections", "read_collection", "collection_status"})


class ReadGateway:
    def __init__(self, service: ContextService):
        self.service = service

    def dispatch(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            fields = {
                "search_collections": ({"query"}, {"query", "limit", "filters", "offset", "version"}),
                "read_collection": (
                    {"material_ref"},
                    {"material_ref", "artifact", "offset", "max_chars", "version"},
                ),
                "collection_status": ({"material_ref"}, {"material_ref"}),
                "list_collections": (set(), {"offset", "limit", "version", "filters"}),
                "library_overview": (set(), set()),
            }
            if action not in fields:
                raise ContextError("permission_denied", "此入口只提供搜索、读取和资料状态。")
            required, allowed = fields[action]
            if not isinstance(arguments, dict) or required - arguments.keys() or arguments.keys() - allowed:
                raise ContextError("invalid_argument", "缺少必填字段或包含不支持的参数。")
            if len(canonical_bytes(arguments)) > 65_536:
                raise ContextError("invalid_argument", "请求参数超过大小限制。")
            if action == "search_collections":
                result = self.service.search(**arguments)
            elif action == "read_collection":
                result = self.service.read(
                    arguments["material_ref"], **{k: v for k, v in arguments.items() if k != "material_ref"}
                )
            elif action == "collection_status":
                result = self.service.status(arguments["material_ref"])
            elif action == "list_collections":
                result = self.service.list_items(**arguments)
            else:
                result = self.service.overview()
            return envelope(result)
        except ContextError as error:
            return envelope(error=error)
        except (ValueError, TypeError, OverflowError, RecursionError):
            # Never echo request input or an exception that could contain credentials.
            return envelope(error=ContextError("invalid_argument", "请求格式或字段类型无效。"))


def http_status(result: dict[str, Any]) -> int:
    if result["ok"]:
        return 200
    code = result["error"]["code"]
    if code == "permission_denied":
        return 403
    if code in {"not_found", "artifact_missing"}:
        return 404
    if code in {"index_outdated", "index_unavailable", "version_changed", "artifact_changed", "writer_busy"}:
        return 409
    if code in {"storage_unavailable", "unsupported_platform", "corrupt_workspace"}:
        return 503
    return 400
