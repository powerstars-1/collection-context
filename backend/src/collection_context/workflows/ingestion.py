"""Own single-link ingestion, separate from paid extraction and existing private vaults."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from collection_context.application.contracts import ContextError, digest, utc_now, validate_source
from collection_context.infrastructure.public_http import PublicDownloadError
from collection_context.infrastructure.download_limits import download_max_bytes
from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.douyin import VERSION, ObservedItem, source_asset_identity
from collection_context.sources.downloads import DouyinDownloads
from collection_context.sources.links import parse_link

if TYPE_CHECKING:
    from collection_context.sources.browser_source import DouyinBrowserSource


class IngestionWorkflow:
    def __init__(
        self,
        store: LibraryStore,
        source: DouyinBrowserSource,
        downloads: DouyinDownloads | None = None,
        *,
        runtime_dir: Path | None = None,
    ):
        self.store, self.source = store, source
        self.downloads = downloads or DouyinDownloads(max_bytes=download_max_bytes(store.snapshot()["settings"]))
        self.runtime_dir = runtime_dir

    def add_link(self, url: str, *, download: bool = False) -> dict[str, Any]:
        observed = self.source.fetch_item(url)
        return self.import_item(observed, kind="link", scope_id="s_link", download=download)

    def sync_creator(self, url: str, *, limit: int = 5, download: bool = False) -> dict[str, Any]:
        if type(download) is not bool or type(limit) is not int or not 1 <= limit <= 20:
            raise ContextError("invalid_argument", "媒体下载开关应为布尔值，数量应为1至20条。")
        link = parse_link(url)
        if link.kind != "creator":
            raise ContextError("source_scope_mismatch", "请指定博主完整主页，不从单作品自动扩大范围。")
        scope_id = "s_" + digest(["douyin_creator", link.identity])[:32]
        now = utc_now()

        def pending(state):
            scope = state["scopes"].setdefault(scope_id, {"kind": "creator", "source_url": link.url})
            scope.pop("error", None)
            scope.update(status="running", complete=False, requested_limit=limit, last_attempt_at=now)

        self.store.transact(pending)
        results: list[dict[str, Any]] = []
        try:
            batch = self.source.fetch_creator(url, limit=limit)
            for item in batch.items.values():
                results.append(self.import_item(item, kind="creator", scope_id=scope_id, download=download))
            coverage = batch.coverage()
            status = (
                "ready"
                if batch.done and all(result["download"] != "blocked" for result in results)
                else "partial"
            )

            def finished(state):
                state["scopes"][scope_id].update(**coverage, status=status, last_sync_at=utc_now())

            self.store.transact(finished)
            return {
                "scope_id": scope_id,
                "status": status,
                "coverage": coverage,
                "items": results,
                "model_requests": 0,
            }
        except ContextError as error:
            failure = error.as_dict()

            def failed(state):
                state["scopes"][scope_id].update(
                    status="blocked", complete=False, error=failure, observed_count=len(results)
                )

            self.store.transact(failed)
            raise

    def import_item(
        self,
        observed: ObservedItem,
        *,
        kind: str,
        scope_id: str,
        download: bool = False,
        observer_principal: str = "local_owner",
    ) -> dict[str, Any]:
        if type(download) is not bool:
            raise ContextError("invalid_argument", "媒体下载开关应为布尔值。")
        # Scope and action time are chosen by a proven connection, never from a content payload.
        result = self.store.upsert(
            observed.source, kind=kind, scope_id=scope_id, observer_principal=observer_principal
        )
        item = result["item"]
        if item["excluded"]:
            raise ContextError("excluded_material", "资料已排除；同步不会恢复、下载或处理它。")
        # Query signatures rotate; hash only offered kind/page and URL host/path, never persist URLs.
        # A changed public resource identity invalidates old machine evidence even if its caption is unchanged.
        source_asset_hash = source_asset_identity(observed)

        def observed_media(state):
            current = state["items"][item["id"]]
            if current["content_hash"] != item["content_hash"] or current["excluded"]:
                raise ContextError("version_changed", "来源资料在入库期间变化，未绑定旧资源。")
            previous = current.get("source_asset_hash")
            changed = previous is not None and previous != source_asset_hash
            if changed:
                for artifact_kind, artifact in current["artifacts"].items():
                    if artifact_kind not in {"original", "user_note"}:
                        artifact["state"] = "stale"
                current.pop("prepared_input", None)
                current.pop("media_preparation", None)
                current.pop("source_input_binding", None)
                current.pop("downloaded_media", None)
            current["source_asset_hash"] = source_asset_hash
            return changed

        media_changed = self.store.transact(observed_media)
        body = observed.source["body"] or "（平台未提供文字说明，未推断视频正文。）"
        original = (
            f"# {observed.source['title']}\n\n{body}\n\n"
            f"来源：{observed.source['source_url']}\n\n作者：{observed.source['author']}\n\n"
            "本文件仅含平台文字说明；音频转写、画面内容与总结各自单独保存。\n"
        )
        self.store.save_artifact(
            item["id"],
            "original",
            original,
            processor_version=VERSION,
            expected_content_hash=item["content_hash"],
            coverage={"complete": True, "scope": "platform_description"},
        )
        FileIndex(self.store).rebuild()
        output: dict[str, Any] = {
            "material_ref": item["id"],
            "native_id": item["native_id"],
            "source_url": item["source_url"],
            "created": result["created"],
            "content_changed": result["content_changed"],
            "source_assets_changed": media_changed,
            "metadata": "ready",
            "download": "not_requested",
            "extraction": "not_requested",
            "model_requests": 0,
        }
        return self.prepare_download(observed, output) if download else output

    def prepare_download(self, observed: ObservedItem, output: dict[str, Any]) -> dict[str, Any]:
        """Prepare already committed metadata; durable callers checkpoint before entering here."""
        item = self.store.get(output["material_ref"])
        source_asset_hash = source_asset_identity(observed)
        if (
            item["excluded"]
            or item.get("source_asset_hash") != source_asset_hash
            or item["content_hash"] != digest(validate_source(observed.source))
        ):
            raise ContextError("version_changed", "来源媒体身份已变化或资料已排除，未继续下载。")
        output = dict(output)
        try:
            registry = PreparedInputs(self.store, runtime_dir=self.runtime_dir)
            identity = registry.reusable_source(item["id"], source_asset_hash)
            reused = identity is not None
            if identity is None:
                media = registry.source_media(item["id"], source_asset_hash)
                reused = media is not None
                if media is None:
                    try:
                        media = self.downloads.fetch(observed)
                    except PublicDownloadError as error:
                        if error.http_status not in {403, 404, 410}:
                            raise
                        # Lists can carry expired CDN URLs. Reopen this exact
                        # work once through the normal page, not a signed API.
                        fresh = self.source.fetch_item(observed.source["source_url"])
                        if fresh.source != observed.source:
                            raise ContextError("source_snapshot_changed", "作品内容在下载前变化，请重新同步。") from None
                        media = self.downloads.fetch(fresh)
                        fresh_hash = source_asset_identity(fresh)
                        if fresh_hash != source_asset_hash:
                            def refresh(state):
                                current = state["items"][item["id"]]
                                if current["excluded"] or current["content_hash"] != item["content_hash"] or current.get("source_asset_hash") != source_asset_hash:
                                    raise ContextError("version_changed", "下载期间资料已变化。")
                                for name, artifact in current["artifacts"].items():
                                    if name not in {"original", "user_note"}:
                                        artifact["state"] = "stale"
                                for key in ("prepared_input", "media_preparation", "source_input_binding", "downloaded_media"):
                                    current.pop(key, None)
                                current["source_asset_hash"] = fresh_hash
                            self.store.transact(refresh)
                            source_asset_hash = fresh_hash
                            output["source_assets_changed"] = True
                    registry.save_source_media(item["id"], media, source_asset_hash=source_asset_hash, content_hash=item["content_hash"])
                # Download success is not the same as OCR/frame readiness.
                try:
                    if item["media_type"] == "image":
                        identity = registry.prepare_images(item["id"], media, expected_content_hash=item["content_hash"])
                    else:
                        identity = registry.prepare_video(item["id"], media[0][0], mime_type=media[0][1], expected_content_hash=item["content_hash"])
                    registry.bind_source(item["id"], identity, source_asset_hash, item["content_hash"])
                except ContextError as error:
                    output.update(download="ready", download_reused=reused, preparation="blocked", preparation_error=error.as_dict())
                    FileIndex(self.store).rebuild()
                    return output
            output.update(download="ready", input_id=identity, download_reused=reused)
        except ContextError as error:
            # A failed download does not hide successfully committed metadata or trigger a model request.
            output.update(download="blocked", error=error.as_dict())
        FileIndex(self.store).rebuild()
        return output
