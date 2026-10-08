"""Own read-only Douyin Markdown layout adapter; no old project imports or migration.

Only fixed inbox cards, named text sections and explicit same-platform attachment
directories participate. Configuration, private areas, media and arbitrary links
are never read. Legacy declarations are not trusted processing checkpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from collection_context.application.contracts import ARTIFACT_KINDS, ContextError, digest
from collection_context.application.service import ContextService, bounded_integer
from collection_context.infrastructure.files import SafeFiles
from collection_context.library.index import normalize
from collection_context.library.legacy_references import legacy_path

INBOX = "00_素材收件箱/抖音"
ATTACHMENTS = "80_附件/抖音"
MAX_CARDS = 10_000
MAX_SCAN_BYTES = 32_000_000
MAX_TEXT_BYTES = 2_000_000
FILENAMES = {
    "audio": "音频转写.md",
    "screen": "画面文字.md",
    "summary": "内容总结.md",
    "image": "图片提取.md",
}
SOURCE_ALIASES = {
    "抖音收藏": "saved",
    "收藏": "saved",
    "抖音喜欢": "liked",
    "喜欢": "liked",
    "抖音博主作品": "creator",
}


def reference(path: str) -> str:
    result = "m1:" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
    legacy_path(result)
    return result


def section(text: str, names: tuple[str, ...]) -> str:
    matches = list(re.finditer(r"^## ([^\r\n]+)[ \t]*$", text, re.MULTILINE))
    values = [
        text[match.end() : matches[pos + 1].start() if pos + 1 < len(matches) else len(text)].strip()
        for pos, match in enumerate(matches)
        if match.group(1).strip() in names
    ]
    if len(values) > 1:
        raise ContextError("legacy_section_ambiguous", "旧卡有重复或冲突的正文栏目，未猜测来源。")
    return values[0] if values else ""


def fields(text: str) -> dict[str, str]:
    header = ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end < 0 or end > 16_000:
            raise ContextError("invalid_legacy_card", "旧卡头信息不完整或超过上限。")
        header = text[4:end]
    result: dict[str, str] = {}
    allowed = {
        "标题",
        "平台",
        "链接",
        "原文链接",
        "作者",
        "作品ID",
        "作品 ID",
        "附件目录",
        "来源",
        "素材类型",
        "媒体类型",
    }
    lines = [(line, True) for line in header.splitlines()] + [
        (line, False) for line in section(text, ("基本信息",)).splitlines()
    ]
    for line, in_header in lines:
        match = re.fullmatch(r"(?:- )?([^:：\n]{1,40})[:：]\s*(.*)", line.strip())
        if not match or match[1].strip() not in allowed:
            continue
        key, value = match[1].strip(), match[2].strip()
        if in_header and value.startswith(("[", "{")):
            raise ContextError("invalid_legacy_card", "旧卡头信息只接受普通文本，不展开列表或对象。")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise ContextError("invalid_legacy_card", "旧卡的文本字段无效。") from None
            if not isinstance(value, str):
                raise ContextError("invalid_legacy_card", "旧卡只支持普通文本字段。")
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        if value.startswith(("!", "&", "*", "|", ">")) or any(ord(c) < 32 for c in value):
            raise ContextError("invalid_legacy_card", "旧卡复杂头信息需先人工核对，不执行或展开。")
        if key in result and result[key] != value:
            raise ContextError("legacy_section_ambiguous", "旧卡文本字段有冲突，未选择其中一个。")
        result[key] = value
    for left, right in (("作品ID", "作品 ID"), ("链接", "原文链接")):
        if left in result and right in result and result[left] != result[right]:
            raise ContextError("legacy_section_ambiguous", "旧卡同义字段有冲突，未猜测来源。")
    return result


def attachment_path(card_path: str, value: str, native_id: str | None) -> str | None:
    if not value:
        return None
    wiki = re.fullmatch(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", value)
    link = re.fullmatch(r"\[[^\]]*\]\(([^\s)]+)\)", value)
    value = wiki[1] if wiki else link[1] if link else value.strip("`")
    if value.startswith(("/", "\\")) or any(c in value for c in ("\\", ":", "\x00", "%", "?", "#")):
        raise ContextError("forbidden_path", "附件关联不在固定旧资料范围，未读取目标。")
    parts = list(PurePosixPath(card_path).parent.parts) if value.startswith(("../", "./")) else []
    for component in value.split("/"):
        if component == "..":
            if not parts:
                raise ContextError("forbidden_path", "附件关联越出旧库，未读取目标。")
            parts.pop()
        elif component == ".":
            continue
        else:
            parts.append(component)
    resolved = "/".join(parts)
    SafeFiles.parts(resolved)
    if (
        len(parts) != 3
        or parts[:2] != ["80_附件", "抖音"]
        or any(part.startswith((".", "_")) for part in parts)
    ):
        raise ContextError("forbidden_path", "只接受同平台明确的附件目录，不跟随其他链接。")
    if native_id and not re.search(r"(?<![0-9])" + re.escape(native_id) + r"(?![0-9])", parts[-1]):
        raise ContextError("attachment_identity_mismatch", "旧卡作品ID与附件目录不符，未读另一条作品。")
    return resolved


class LegacyLayoutReader:
    def __init__(self, root: Path):
        root = root.absolute()
        self.guard_roots = (root, root.parent) if root.name == "content-vault" else (root,)
        self.files: SafeFiles | None = None
        # Explicit mode only. Never use this adapter to ignore a new library's
        # manifest, exclusions, corruption or access registry.
        base = SafeFiles(root)
        try:
            if base.entry_exists("context-workspace.json") or base.entry_exists(".context"):
                raise ContextError(
                    "legacy_mode_not_allowed", "新产品库须按清单读取，不能用旧库模式绕过状态。"
                )
            vault = root / "content-vault" if base.entry_exists("content-vault") else root
            self.guard_roots = tuple(dict.fromkeys((*self.guard_roots, vault)))
            if vault == root and root.name == "content-vault":
                with SafeFiles(root.parent) as parent:
                    if parent.entry_exists("context-workspace.json") or parent.entry_exists(".context"):
                        raise ContextError(
                            "legacy_mode_not_allowed", "此目录属于新产品库，不能绕过清单读取。"
                        )
            files = SafeFiles(vault)
            try:
                if files.entry_exists("context-workspace.json") or files.entry_exists(".context"):
                    raise ContextError("legacy_mode_not_allowed", "此目录属于新产品库，不能绕过清单读取。")
                if not files.entry_exists(INBOX):
                    raise ContextError("not_found", "旧库中没有抖音素材收件箱。")
            except BaseException:
                files.close()
                raise
            self.files = files
        finally:
            base.close()

    @property
    def safe(self) -> SafeFiles:
        assert self.files is not None
        return self.files

    def close(self) -> None:
        if self.files is not None:
            self.files.close()

    def _check_layout(self) -> None:
        # A running reader cannot continue after its source becomes a managed
        # library whose manifest/exclusions must be honored by the new reader.
        self.safe.check_root()
        for root in self.guard_roots:
            with SafeFiles(root) as files:
                if files.entry_exists("context-workspace.json") or files.entry_exists(".context"):
                    raise ContextError(
                        "legacy_mode_not_allowed", "此目录现在属于新产品库，请按清单重新连接。"
                    )

    def _text(self, path: str, budget: list[int] | None = None) -> tuple[str, str]:
        body = self.safe.read(path, max_bytes=MAX_TEXT_BYTES)
        if budget is not None:
            budget[0] += len(body)
            if budget[0] > MAX_SCAN_BYTES:
                raise ContextError("scan_limit", "旧库只读扫描超过本次字节预算，覆盖不完整。")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ContextError("invalid_artifact", "旧资料不是有效UTF-8文本。") from None
        if len(text) > 500_000:
            raise ContextError("invalid_artifact", "旧资料正文超过读取上限。")
        return text, hashlib.sha256(body).hexdigest()

    def _card(self, ref: str, budget: list[int] | None = None) -> dict[str, Any]:
        self._check_layout()
        path = legacy_path(ref)
        if any(part.startswith((".", "_")) for part in path.split("/")):
            raise ContextError("forbidden_path", "隐藏目录和模板不属于可读素材。")
        text, sha = self._text(path, budget)
        if not text.strip():
            raise ContextError("invalid_legacy_card", "旧素材卡为空。")
        meta = fields(text)
        if meta.get("平台", "抖音") not in {"抖音", "douyin"}:
            raise ContextError("invalid_legacy_card", "旧卡的平台与抖音目录不一致。")
        title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
        title = meta.get("标题") or (title_match[1] if title_match else PurePosixPath(path).stem)
        source_url = meta.get("原文链接", meta.get("链接", ""))
        if source_url:
            try:
                url = urlsplit(source_url)
                valid_url = (
                    url.scheme == "https"
                    and url.hostname in {"www.douyin.com", "douyin.com", "v.douyin.com"}
                    and not url.username
                    and not url.password
                    and url.port in {None, 443}
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ContextError("invalid_legacy_card", "旧卡来源不是受支持的抖音HTTPS地址。")
            source_url = url._replace(query="", fragment="").geturl()
        native_id = meta.get("作品ID", meta.get("作品 ID"))
        url_id = re.search(r"/(?:video|note)/([0-9]{1,64})(?:/|$)", urlsplit(source_url).path)
        if native_id and (not re.fullmatch(r"[0-9]{1,64}", native_id) or url_id and native_id != url_id[1]):
            raise ContextError("invalid_legacy_card", "旧卡作品ID无效或与链接冲突。")
        native_id = native_id or (url_id[1] if url_id else None)
        association_error = None
        try:
            attachment = attachment_path(path, meta.get("附件目录", ""), native_id)
        except ContextError as error:
            attachment, association_error = None, error.code
        kind = SOURCE_ALIASES.get(meta.get("来源", ""))
        media = meta.get("媒体类型", meta.get("素材类型", ""))
        media_type = (
            "image"
            if media in {"图文", "图片", "image"} or "/note/" in source_url
            else "video"
            if media in {"视频", "video"} or "/video/" in source_url
            else "unknown"
        )
        return {
            "material_ref": ref,
            "title": title[:500],
            "author": meta.get("作者", "")[:500],
            "platform": "douyin",
            "media_type": media_type,
            "source_url": source_url,
            "published_at": None,
            "first_observed_at": None,
            "last_observed_at": None,
            "relations": (
                [{"kind": kind, "scope_id": "s_legacy_" + kind, "action_at": None}] if kind else []
            ),
            "path": path,
            "card_sha": sha,
            "attachment": attachment,
            "attachment_issue": association_error,
            "original": section(text, ("原始材料", "正文文本", "原文文案")),
            "user_note": section(text, ("用户备注",)),
        }

    @staticmethod
    def _public(card: dict[str, Any]) -> dict[str, Any]:
        return {
            key: card[key]
            for key in (
                "material_ref",
                "title",
                "author",
                "platform",
                "media_type",
                "source_url",
                "published_at",
                "first_observed_at",
                "last_observed_at",
                "relations",
            )
        }

    def _artifact(self, card: dict[str, Any], kind: str, budget: list[int] | None = None) -> tuple[str, str]:
        if kind in {"original", "user_note"}:
            text, sha = card[kind], card["card_sha"]
        else:
            if kind == "audio" and card["media_type"] == "image":
                raise ContextError("artifact_not_applicable", "此旧卡明确为图文，音频转写不适用。")
            if not card["attachment"]:
                raise ContextError(
                    card["attachment_issue"] or "attachment_association_missing",
                    "旧卡没有可核对的附件关联，未猜测文件位置。",
                )
            text, sha = self._text(card["attachment"] + "/" + FILENAMES[kind], budget)
        if not text.strip():
            raise ContextError("artifact_missing", "旧资料此类正文为空，未用其他栏目代替。")
        return text, "l_" + digest([card["card_sha"], kind, sha])

    @staticmethod
    def _coverage() -> dict[str, Any]:
        return {
            "complete": None,
            "accuracy": "not_verified",
            "layout": "legacy_douyin_readonly",
            "checkpoint_verified": False,
            "timestamp_precision": "unknown",
        }

    def _check_card(self, card: dict[str, Any], budget: list[int] | None = None) -> None:
        # A card may be edited independently while an attachment is read. Do not
        # return a text attachment paired with outdated source/identity metadata.
        self._check_layout()
        _, sha = self._text(card["path"], budget)
        if sha != card["card_sha"]:
            raise ContextError("version_changed", "读取期间旧卡发生变化，请重新读取。")

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
            raise ContextError("invalid_artifact", "旧库只读取固定文字产物，不接受文件路径。")
        if offset and not version:
            raise ContextError("version_required", "继续读取旧正文须带版本，不能拼接两版内容。")
        card = self._card(ref)
        text, current = self._artifact(card, artifact)
        if version is not None and version != current:
            raise ContextError("version_changed", "旧卡或正文已变化，请重新从开头读取。")
        if offset > len(text):
            raise ContextError("invalid_argument", "正文读取位置超出范围。")
        result = text[offset : offset + max_chars]
        self._check_card(card)
        return {
            **self._public(card),
            "artifact": artifact,
            "text": result,
            "version": current,
            "offset": offset,
            "next_offset": offset + len(result) if offset + len(result) < len(text) else None,
            "total_chars": len(text),
            "state": "ready",
            "coverage": self._coverage(),
            "content_untrusted": True,
            "model_requests": 0,
            "warnings": ["旧文件存在不代表处理完整或识别准确；没有迁移或核验旧任务状态。"],
        }

    def status(self, ref: str) -> dict[str, Any]:
        card = self._card(ref)
        artifacts = {}
        for kind in sorted(ARTIFACT_KINDS):
            try:
                _, version = self._artifact(card, kind)
                artifacts[kind] = {"state": "ready", "version": version, "coverage": self._coverage()}
            except ContextError as error:
                artifacts[kind] = {
                    "state": "not_applicable"
                    if error.code == "artifact_not_applicable"
                    else "missing"
                    if error.code in {"not_found", "artifact_missing", "attachment_association_missing"}
                    else "unavailable",
                    "issue": error.code,
                    "coverage": self._coverage(),
                }
        self._check_card(card)
        return {
            **self._public(card),
            "artifacts": artifacts,
            "accuracy": "not_verified",
            "content_untrusted": True,
            "layout": "legacy_douyin_readonly",
            "scope_coverage": {"state": "unknown", "scopes": []},
            "attachment_association": "explicit" if card["attachment"] else "unavailable",
            "model_requests": 0,
        }

    def _scan(self) -> tuple[list[tuple[dict[str, Any], dict[str, str]]], list[dict[str, str]], str, bool]:
        self._check_layout()
        records, gaps, budget, directories = [], [], [0], [(INBOX, 0)]
        examined = 0
        partial = False
        signatures = []
        while directories:
            path, depth = directories.pop(0)
            try:
                entries = self.safe.list_directory(path, max_entries=MAX_CARDS)
            except ContextError as error:
                gaps.append({"code": error.code})
                partial = True
                continue
            for entry in entries:
                name = entry["name"]
                if name.startswith((".", "_")):
                    continue
                child = path + "/" + name
                if entry["kind"] == "unsafe":
                    gaps.append({"code": "forbidden_path"})
                    partial = True
                    continue
                if entry["kind"] == "directory":
                    if depth < 3 and len(directories) < 1000:
                        directories.append((child, depth + 1))
                    else:
                        partial = True
                    continue
                if not name.endswith(".md"):
                    continue
                examined += 1
                if examined > MAX_CARDS or budget[0] > MAX_SCAN_BYTES:
                    partial = True
                    directories.clear()
                    break
                try:
                    if entry["kind"] != "file":
                        raise ContextError("forbidden_path", "链接或特殊文件不参与旧库读取。")
                    ref = reference(child)
                    card = self._card(ref, budget)
                    texts = {}
                    versions = {}
                    for kind in sorted(ARTIFACT_KINDS):
                        try:
                            text, version = self._artifact(card, kind, budget)
                            texts[kind], versions[kind] = text, version
                        except ContextError as error:
                            if error.code == "scan_limit":
                                raise
                            if error.code not in {"not_found", "artifact_missing", "artifact_not_applicable"}:
                                gaps.append({"material_ref": ref, "artifact": kind, "code": error.code})
                                partial = True
                    self._check_card(card, budget)
                    signatures.append([ref, card["card_sha"], versions])
                    records.append((card, texts))
                except ContextError as error:
                    gaps.append({"code": error.code})
                    partial = True
                    if error.code == "scan_limit":
                        directories.clear()
                        break
        self._check_layout()
        if len(gaps) > 1000:
            gaps = gaps[:1000] + [{"code": "additional_gaps_omitted"}]
        return records, gaps, digest([signatures, gaps, partial]), partial

    @staticmethod
    def _matches(card: dict[str, Any], filters: dict[str, Any]) -> bool:
        if "source_kinds" in filters and not any(
            relation["kind"] in filters["source_kinds"] for relation in card["relations"]
        ):
            return False
        if "scope_id" in filters and not any(
            relation["scope_id"] == filters["scope_id"] for relation in card["relations"]
        ):
            return False
        # No old mtime/ingest timestamp is promoted into a true action time.
        return not ({"since", "until"} & filters.keys())

    def _listing(
        self,
        *,
        query: str | None,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None,
        version: str | None,
    ) -> dict[str, Any]:
        bounded_integer(limit, "limit", 1, 20)
        bounded_integer(offset, "offset", 0, 100_000)
        if offset and not version:
            raise ContextError("version_required", "继续旧库列表须携带上一页版本。")
        validated = ContextService._filters(filters)
        terms = []
        if query is not None:
            if not isinstance(query, str) or not query.strip() or len(query) > 500 or "\x00" in query:
                raise ContextError("invalid_argument", "请输入不超过500字的搜索词。")
            terms = list(dict.fromkeys(normalize(query).split()))
            if len(terms) > 20:
                raise ContextError("invalid_argument", "搜索词过多。")
        records, gaps, stamp, partial = self._scan()
        current = digest([stamp, sorted(terms), validated])
        if version is not None and version != current:
            raise ContextError("version_changed", "旧资料、证据或查询条件已变化，请重新读取第一页。")
        matches = []
        for card, texts in records:
            if not self._matches(card, validated):
                continue
            fields = {"metadata": "\n".join((card["title"], card["author"])), **texts}
            if not all(any(term in normalize(text) for text in fields.values()) for term in terms):
                continue
            evidence = [
                kind for kind, text in fields.items() if any(term in normalize(text) for term in terms)
            ]
            chosen = next((kind for kind in evidence if kind != "metadata"), "metadata")
            text = fields[chosen]
            first = min((normalize(text).find(term) for term in terms if term in normalize(text)), default=0)
            matches.append(
                {
                    **self._public(card),
                    "matched_artifacts": evidence,
                    "snippet": text[max(0, first - 40) : max(0, first - 40) + 250],
                    "artifact_states": {kind: "ready" for kind in texts},
                    "content_untrusted": True,
                    "action_time_known": False,
                    "evidence_checked": True,
                }
            )
        matches.sort(key=lambda card: card["material_ref"])
        if offset > len(matches):
            raise ContextError("invalid_argument", "旧库列表位置超出范围。")
        rows = matches[offset : offset + limit]
        return {
            "items": rows,
            "total_matches" if query is not None else "total_items": len(matches),
            "query": query,
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < len(matches) else None,
            "version": current,
            "library_version": stamp,
            "scope_coverage": {"state": "unknown", "scopes": []},
            "coverage": {"partial": partial, "scanned_items": len(records), "accuracy": "not_verified"},
            "integrity_gaps": gaps,
            "retrieval": "lexical",
            "semantic_search": False,
            "layout": "legacy_douyin_readonly",
            "model_requests": 0,
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
        if not isinstance(query, str):
            raise ContextError("invalid_argument", "搜索词必须是文本。")
        return self._listing(query=query, limit=limit, filters=filters, offset=offset, version=version)

    def list_items(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        version: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._listing(query=None, limit=limit, filters=filters, offset=offset, version=version)

    def overview(self) -> dict[str, Any]:
        records, gaps, _, partial = self._scan()
        return {
            "total_items": len(records),
            "job_counts": {},
            "auto_sync": False,
            "auto_process": False,
            "scope_coverage": {"state": "unknown", "scopes": []},
            "layout": "legacy_douyin_readonly",
            "coverage": {"partial": partial, "accuracy": "not_verified"},
            "integrity_gaps": gaps,
            "model_requests": 0,
            "development_candidate": True,
        }
