"""One bounded read service for CLI, MCP and HTTP; no models or tasks dispatched."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from collection_context.application.contracts import (
    ARTIFACT_KINDS,
    SOURCE_KINDS,
    ContextError,
    digest,
    valid_id,
    validate_time,
)
from collection_context.library.index import (
    FileIndex,
    artifact_bytes,
    audio_not_applicable,
    is_current,
    library_version,
    normalize,
    original_text,
)
from collection_context.library.legacy_references import resolve_reference
from collection_context.library.store import LibraryStore


def bounded_integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContextError("invalid_argument", f"{name} 须在 {minimum}～{maximum} 之间。")
    return value


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "material_ref": item["id"],
        "title": item["title"],
        "author": item["author"],
        "platform": item["platform"],
        "media_type": item["media_type"],
        "source_url": item["source_url"],
        "published_at": item["published_at"],
        "first_observed_at": item["first_observed_at"],
        "last_observed_at": item["last_observed_at"],
        "relations": list(item["relations"].values()),
    }


def scope_coverage(state: dict[str, Any]) -> dict[str, Any]:
    # Do not return internal cursors, login data or future provider configuration.
    allowed = {"kind", "status", "complete", "observed_count", "last_sync_at"}
    scopes = [
        {"scope_id": sid, **{k: v for k, v in value.items() if k in allowed}}
        for sid, value in state["scopes"].items()
    ]
    return {"state": "unknown" if not scopes else "reported", "scopes": scopes}


class ContextService:
    def __init__(self, store: LibraryStore):
        self.store = store
        self.index = FileIndex(store)

    def list_items(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        version: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bounded_integer(offset, "offset", 0, 100_000)
        bounded_integer(limit, "limit", 1, 20)
        if offset and not version:
            raise ContextError("version_required", "继续列表须携带上一页版本。")
        filters = self._filters(filters)
        state = self.store.snapshot()
        current_version = digest({"library_version": library_version(state), "filters": filters})
        if version is not None and current_version != version:
            raise ContextError("version_changed", "资料列表已变化，请重新从第一页读取。", retryable=True)
        items = [
            item
            for item in state["items"].values()
            if not item["excluded"] and self._matches_filters(item, filters)
        ]
        items.sort(key=lambda item: (item["first_observed_at"], item["id"]), reverse=True)
        if offset > len(items):
            raise ContextError("invalid_argument", "列表位置超过范围。")
        rows = []
        for item in items[offset : offset + limit]:
            rows.append(
                {
                    **public_item(item),
                    "artifact_states": {
                        kind: a["state"] if is_current(item, a) else "stale"
                        for kind, a in item["artifacts"].items()
                    },
                    "evidence_checked": False,
                    "content_untrusted": True,
                }
            )
        next_offset = offset + len(rows) if offset + len(rows) < len(items) else None
        return {
            "items": rows,
            "total_items": len(items),
            "offset": offset,
            "next_offset": next_offset,
            "version": current_version,
            "scope_coverage": scope_coverage(state),
        }

    def overview(self) -> dict[str, Any]:
        state = self.store.snapshot()
        items = [item for item in state["items"].values() if not item["excluded"]]
        job_counts: dict[str, int] = {}
        for job in state["jobs"].values():
            job_counts[job["state"]] = job_counts.get(job["state"], 0) + 1
        return {
            "total_items": len(items),
            "job_counts": job_counts,
            "audio_missing": sum(
                "audio" not in item["artifacts"] for item in items if not audio_not_applicable(item)
            ),
            "auto_sync": state["settings"].get("auto_sync", False),
            "auto_process": state["settings"].get("auto_process", False),
            "scope_coverage": scope_coverage(state),
            "development_candidate": True,
        }

    @staticmethod
    def _item(state: dict[str, Any], ref: str) -> dict[str, Any]:
        item = state["items"].get(resolve_reference(state, ref))
        if item is None or item["excluded"]:
            raise ContextError("not_found", "资料不存在或已排除。")
        return item

    def status(self, ref: str) -> dict[str, Any]:
        state = self.store.snapshot()
        item = self._item(state, ref)
        artifacts: dict[str, Any] = {}
        for kind in sorted(ARTIFACT_KINDS):
            if kind == "audio" and audio_not_applicable(item):
                artifacts[kind] = {"state": "not_applicable", "coverage": {"has_audio": False}}
                continue
            artifact = item["artifacts"].get(kind)
            if artifact is None:
                artifacts[kind] = {"state": "ready" if kind == "original" else "missing", "coverage": None}
                continue
            status = artifact["state"] if is_current(item, artifact) else "stale"
            issue = None
            try:
                artifact_bytes(self.store, item, kind)
            except ContextError as error:
                status, issue = "unavailable", error.code
            artifacts[kind] = {
                "state": status,
                "issue": issue,
                "version": artifact["version"],
                "created_at": artifact["created_at"],
                "processor_version": artifact["processor_version"],
                "coverage": artifact["coverage"],
            }
        return {
            **public_item(item),
            "artifacts": artifacts,
            "content_version": item["content_hash"],
            "library_version": library_version(state),
            "scope_coverage": scope_coverage(state),
            "accuracy": "not_verified",
            "content_untrusted": True,
        }

    def read(
        self,
        ref: str,
        *,
        artifact: str = "original",
        offset: int = 0,
        max_chars: int = 4000,
        version: str | None = None,
    ) -> dict[str, Any]:
        bounded_integer(offset, "offset", 0, 500_000)
        bounded_integer(max_chars, "max_chars", 1, 20_000)
        if not isinstance(artifact, str) or artifact not in ARTIFACT_KINDS:
            raise ContextError("invalid_artifact", "请选择原文、转写、画面、总结、可读内容、图片或备注。")
        if offset and not version:
            raise ContextError("version_required", "继续读取须带上上一页返回的版本，避免拼接不同正文。")
        state = self.store.snapshot()
        item = self._item(state, ref)
        stored = item["artifacts"].get(artifact)
        if artifact == "original" and stored is None:
            text, current_version, output_state = (
                original_text(item),
                "m_" + digest(item["content_hash"]),
                "ready",
            )
            coverage = {"source": "platform_metadata", "accuracy": "not_verified"}
        else:
            text = artifact_bytes(self.store, item, artifact).decode("utf-8")
            current_version = stored["version"]
            output_state = stored["state"] if is_current(item, stored) else "stale"
            coverage = stored["coverage"]
        if version is not None and version != current_version:
            raise ContextError("version_changed", "正文版本已变更；请从开头重新读取。", retryable=True)
        if offset > len(text):
            raise ContextError("invalid_argument", "继续读取位置超过正文长度。")
        fragment = text[offset : offset + max_chars]
        next_offset = offset + len(fragment) if offset + len(fragment) < len(text) else None
        return {
            **public_item(item),
            "artifact": artifact,
            "text": fragment,
            "version": current_version,
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": len(text),
            "state": output_state,
            "coverage": coverage,
            "content_untrusted": True,
            "warnings": (
                ["输入已变化，此产物不能当作当前完整提取。"]
                if output_state == "stale"
                else ["此产物的覆盖不完整或尚未核验，请查看覆盖缺口。"]
                if coverage.get("complete") is False
                else []
            ),
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 3,
        filters: dict[str, Any] | None = None,
        offset: int = 0,
        version: str | None = None,
    ) -> dict[str, Any]:
        bounded_integer(limit, "limit", 1, 20)
        bounded_integer(offset, "offset", 0, 100_000)
        if version is not None and (
            type(version) is not str
            or len(version) != 64
            or any(c not in "0123456789abcdef" for c in version)
        ):
            raise ContextError("invalid_argument", "搜索版本须为上一页返回的受控版本。")
        if offset and version is None:
            raise ContextError("version_required", "继续搜索须携带上一页version与next_offset。")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or "\x00" in query:
            raise ContextError("invalid_argument", "请输入不超过 500 字符的搜索词。")
        terms = list(dict.fromkeys(normalize(query).split()))
        if len(terms) > 20:
            raise ContextError("invalid_argument", "搜索词过多，请缩小问题。")
        filters = self._filters(filters)
        state = self.store.snapshot()
        index = self.index.load(state)
        matches, integrity_gaps = [], []
        for ref, fields in index["documents"].items():
            item = self._item(state, ref)
            if not self._matches_filters(item, filters):
                continue
            if not all(any(term in normalize(text) for text in fields.values()) for term in terms):
                continue
            # Revalidate only candidate evidence; an external edit cannot return cached text.
            current = {"metadata": "\n".join((item["title"], item["author"], item["body"]))}
            for kind in sorted(fields.keys() - {"metadata"}):
                try:
                    current[kind] = artifact_bytes(self.store, item, kind).decode("utf-8")
                except ContextError as error:
                    integrity_gaps.append({"material_ref": ref, "artifact": kind, "code": error.code})
            if not all(any(term in normalize(text) for text in current.values()) for term in terms):
                continue
            evidence = [
                kind for kind, text in current.items() if any(term in normalize(text) for term in terms)
            ]
            score = sum(4 if term in normalize(item["title"]) else 1 for term in terms)
            chosen = next((kind for kind in evidence if kind != "metadata"), "metadata")
            text = current[chosen]
            normalized = normalize(text)
            first = min((normalized.find(term) for term in terms if term in normalized), default=0)
            matches.append(
                (
                    score,
                    ref,
                    {
                        **public_item(item),
                        "matched_artifacts": evidence,
                        "snippet": text[max(0, first - 40) : max(0, first - 40) + 250],
                        "content_version": item["content_hash"],
                        "content_untrusted": True,
                        "action_time_known": any(
                            r["action_at"] is not None
                            for r in item["relations"].values()
                            if ("source_kinds" not in filters or r["kind"] in filters["source_kinds"])
                            and ("scope_id" not in filters or r["scope_id"] == filters["scope_id"])
                        ),
                    },
                )
            )
        matches.sort(key=lambda row: (-row[0], row[1]))
        # Bind the query/filter and revalidated candidate set, not job progress
        # or a particular page size. External artifact edits can change matches
        # without a manifest commit; their integrity gaps must invalidate pages.
        current_version = digest(
            {
                "library_version": index["library_version"],
                "query_terms": sorted(terms),
                "filters": filters,
                "matches": [(row[0], row[1]) for row in matches],
                "integrity_gaps": index["gaps"] + integrity_gaps,
            }
        )
        if version is not None and version != current_version:
            raise ContextError(
                "version_changed", "搜索结果或条件已变化，请从第一页重新搜索。", retryable=True
            )
        if offset > len(matches):
            raise ContextError("invalid_argument", "搜索位置超过结果范围。")
        rows = [row[2] for row in matches[offset : offset + limit]]
        next_offset = offset + len(rows) if offset + len(rows) < len(matches) else None
        return {
            "query": query,
            "items": rows,
            "total_matches": len(matches),
            "has_more": next_offset is not None,
            "offset": offset,
            "next_offset": next_offset,
            "version": current_version,
            "library_version": index["library_version"],
            "scope_coverage": scope_coverage(state),
            "integrity_gaps": index["gaps"] + integrity_gaps,
            "retrieval": "lexical",
            "semantic_search": False,
            "model_requests": 0,
        }

    @staticmethod
    def _filters(value: dict[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        allowed = {"source_kinds", "scope_id", "since", "until", "time_basis"}
        if not isinstance(value, dict) or value.keys() - allowed:
            raise ContextError("invalid_argument", "搜索筛选项无效。")
        result = dict(value)
        if "source_kinds" in value:
            kinds = value["source_kinds"]
            if (
                not isinstance(kinds, list)
                or not kinds
                or any(not isinstance(k, str) or k not in SOURCE_KINDS for k in kinds)
            ):
                raise ContextError("invalid_argument", "来源筛选无效。")
        if "scope_id" in value:
            valid_id(value["scope_id"])
        if "since" in value or "until" in value:
            if value.get("time_basis") not in {
                "action_at",
                "published_at",
                "first_observed_at",
                "last_observed_at",
            }:
                raise ContextError("invalid_argument", "时间筛选须明确操作、发布或发现时间。")
            for field in ("since", "until"):
                if field in value:
                    result[field] = validate_time(value[field])
            if (
                "since" in result
                and "until" in result
                and datetime.fromisoformat(result["since"]) > datetime.fromisoformat(result["until"])
            ):
                raise ContextError("invalid_argument", "开始时间不能晚于结束时间。")
        elif "time_basis" in value:
            raise ContextError("invalid_argument", "时间依据需同时指定时间范围。")
        return result

    @staticmethod
    def _matches_filters(item: dict[str, Any], filters: dict[str, Any]) -> bool:
        relations = [
            r
            for r in item["relations"].values()
            if ("source_kinds" not in filters or r["kind"] in filters["source_kinds"])
            and ("scope_id" not in filters or r["scope_id"] == filters["scope_id"])
        ]
        if not relations:
            return False
        basis = filters.get("time_basis")
        if basis is None:
            return True
        times = [r["action_at"] for r in relations] if basis == "action_at" else [item[basis]]
        return any(
            time is not None
            and (
                "since" not in filters
                or datetime.fromisoformat(time) >= datetime.fromisoformat(filters["since"])
            )
            and (
                "until" not in filters
                or datetime.fromisoformat(time) <= datetime.fromisoformat(filters["until"])
            )
            for time in times
        )
