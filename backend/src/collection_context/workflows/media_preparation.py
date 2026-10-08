"""Owner-selected local preparation from already saved media, never a model call."""

from collection_context.application.contracts import ContextError, digest
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs, preparation_error
from collection_context.infrastructure.media import PROCESSOR_VERSION
from collection_context.processing.ocr_selection import OCR_SELECTION_VERSION
from collection_context.workflows.jobs import JobManager


class MediaPreparation:
    def __init__(self, store: LibraryStore, runtime_dir=None):
        self.store, self.runtime_dir = store, runtime_dir
        self.jobs = JobManager(store)

    def submit(self, *, material_ref, mode, idempotency_key):
        if mode not in {"audio", "full"}:
            raise ContextError("invalid_argument", "请选择音频或完整媒体准备。")
        item = self.store.get(material_ref)
        if item["excluded"] or not item.get("downloaded_media"):
            raise ContextError("media_not_prepared", "请先保存这一条的原媒体。")
        payload = {"material_ref": material_ref, "mode": mode, "content_hash": item["content_hash"],
                   "source_asset_hash": item.get("source_asset_hash"), "previous_input": item.get("prepared_input")}

        def admit(state, submitted):
            for job in state["jobs"].values():
                if job["id"] == submitted["id"] or job["state"] not in {"queued", "running"}:
                    continue
                preparation = job["payload"].get("media_preparation", {})
                extraction = job["payload"].get("extraction", {})
                extracted = state.get("prepared_inputs", {}).get(extraction.get("input_id"), {})
                if preparation.get("material_ref") == material_ref or extracted.get("material_ref") == material_ref:
                    raise ContextError("processing_already_queued", "这条资料已有运行任务，请等它完成后再准备媒体。")

        return self.store.transact(self.jobs._submission("add", {"media_preparation": payload},
            idempotency_key=idempotency_key, max_calls=0, admission=admit))

    def run(self, job_id):
        job = self.jobs.get(job_id)
        if job["principal"] != "local_owner" or job["kind"] != "add" or job["budget"]["max_calls"] != 0:
            raise ContextError("invalid_argument", "媒体准备任务不匹配。")
        if job["state"] != "queued":
            return job
        plan = job["payload"]["media_preparation"]
        with self.store.storage.executor(self.store.files.root, expected_identity=self.store.files.identity):
            self.jobs.start(job_id)
            def check_running():
                current = self.jobs.get(job_id)
                if current["state"] != "running" or current.get("cancel_requested"):
                    raise ContextError("job_cancelled", "任务已停止，未更新媒体输入。")
            guarded = LibraryStore(self.store.files.root, mutation_guard=check_running)
            def progress(stage, state, value):
                check_running()
                error = ContextError(value["error_code"], ("音频" if stage == "prepare_audio" else "画面") + "准备未完成，已成功的输入保留。") if value.get("error_code") else None
                self.jobs.set_stage(job_id, stage, state_name=state,
                    input_hash=digest([plan, stage]), result={"progress": value}, error=error)
            try:
                progress("prepare_audio", "running", {"phase": "check"})
                item = self.store.get(plan["material_ref"])
                if item["excluded"] or item["content_hash"] != plan["content_hash"] or item.get("source_asset_hash") != plan["source_asset_hash"] or item.get("prepared_input") != plan["previous_input"]:
                    raise ContextError("version_changed", "资料输入已变化，请重新选择准备。")
                registry = PreparedInputs(guarded, runtime_dir=self.runtime_dir)
                source = registry.source_media(item["id"], plan["source_asset_hash"])
                if not source:
                    raise ContextError("media_not_prepared", "保存的原媒体不可用，请先保存这一条。")
                previous = registry.load(plan["previous_input"]) if plan["previous_input"] else None
                same_source = previous is not None and previous["content_hash"] == item["content_hash"] and previous["originals"] == item["downloaded_media"]["originals"]
                reusable_audio = same_source and not preparation_error(previous["coverage"], "audio")
                reusable_vision = same_source and bool(previous["frames"]) and preparation_error(previous["coverage"], "vision") in {None, "vision_deferred"}
                if not reusable_vision:
                    # A previous audio-only scope may have hidden the frame manifest.
                    # Recover verified inputs from this content's recent local history.
                    snapshot = self.store.snapshot()
                    candidates = []
                    for prior in sorted(snapshot["jobs"].values(), key=lambda value: value["created_at"], reverse=True):
                        ref = prior["payload"].get("extraction", {}).get("input_id") or (prior.get("stages", {}).get("link_import", {}).get("result") or {}).get("result", {}).get("input_id")
                        if ref and snapshot.get("prepared_inputs", {}).get(ref, {}).get("material_ref") == item["id"] and ref not in candidates:
                            candidates.append(ref)
                        if len(candidates) >= 20: break
                    for ref in candidates:
                        candidate = registry.load(ref)
                        if candidate["content_hash"] == item["content_hash"] and candidate["originals"] == item["downloaded_media"]["originals"] and candidate["frames"] and not preparation_error(candidate["coverage"], "vision") and (candidate["kind"] == "image" or candidate["processor_version"] == PROCESSOR_VERSION and candidate["coverage"].get("ocr_selection_version") == OCR_SELECTION_VERSION):
                            previous = {**candidate, "audio": previous["audio"] if reusable_audio else candidate["audio"]}
                            reusable_vision = True
                            reusable_audio = reusable_audio or bool(candidate["audio"]) and not preparation_error(candidate["coverage"], "audio")
                            break
                if previous and previous["kind"] == "video":
                    reusable_vision = reusable_vision and previous["processor_version"] == PROCESSOR_VERSION and (previous["coverage"].get("ocr_selection_version") in {None, OCR_SELECTION_VERSION})
                if item["media_type"] == "image":
                    if plan["mode"] == "audio":
                        raise ContextError("audio_unavailable", "图文没有音轨，请使用画面提取。")
                    progress("prepare_audio", "not_applicable", {"phase": "no_audio"})
                    progress("prepare_vision", "running", {"phase": "images"})
                    identity = plan["previous_input"] if reusable_vision else registry.prepare_images(item["id"], source, expected_content_hash=item["content_hash"])
                    progress("prepare_vision", "ready", {"count": len(source), "reused": bool(reusable_vision)})
                elif reusable_audio and reusable_vision:
                    audio = registry.audio(previous)
                    frames = registry.frames(previous)
                    progress("prepare_audio", "ready", {"count": len(audio), "reused": True})
                    progress("prepare_vision", "ready", {"count": len(frames), "reused": True})
                    coverage = dict(previous["coverage"])
                    if plan["mode"] == "audio":
                        coverage["audio_only"] = True
                        coverage["preparation_errors"] = {**coverage.get("preparation_errors", {}), "vision": {"code": "vision_deferred"}}
                    elif coverage.get("audio_only"):
                        coverage.pop("audio_only", None)
                        errors = dict(coverage.get("preparation_errors", {})); errors.pop("vision", None)
                        if errors: coverage["preparation_errors"] = errors
                        else: coverage.pop("preparation_errors", None)
                    identity = registry.save(item["id"], content_hash=item["content_hash"], originals=source,
                        audio=audio, frames=frames, coverage=coverage, processor_version=previous["processor_version"],
                        strategy_hash=previous["strategy_hash"], kind="video")
                else:
                    identity = registry.prepare_video(item["id"], source[0][0], mime_type=source[0][1],
                        expected_content_hash=item["content_hash"], audio_only=plan["mode"] == "audio",
                        reuse_audio=registry.audio(previous) if reusable_audio else None,
                        reuse_frames=registry.frames(previous) if reusable_vision else None, progress=progress)
                    if plan["mode"] == "audio" and not reusable_vision:
                        progress("prepare_vision", "not_applicable", {"phase": "not_selected"})
                registry.bind_source(item["id"], identity, plan["source_asset_hash"], item["content_hash"])
                prepared = registry.manifest(identity)
                partial = any(code and code != "vision_deferred" for code in (preparation_error(prepared["coverage"], role) for role in ("audio", "vision")))
                self.jobs.set_stage(job_id, "link_import", state_name="ready", input_hash=digest(plan),
                    result={"result": {"material_ref": item["id"], "metadata": "ready", "download": "ready", "input_id": identity}})
                return self.jobs.finish(job_id, "partial" if partial else "succeeded")
            except ContextError as error:
                if self.jobs.get(job_id)["state"] != "running":
                    return self.jobs.get(job_id)
                for name, stage in self.jobs.get(job_id)["stages"].items():
                    if name.startswith("prepare_") and stage["state"] == "running":
                        self.jobs.set_stage(job_id, name, state_name="failed", input_hash=stage["input_hash"], error=error)
                self.jobs.set_stage(job_id, "prepare_media", state_name="failed", input_hash=digest(plan), error=error)
                return self.jobs.finish(job_id, "failed", error=error)
            finally:
                guarded.close()
