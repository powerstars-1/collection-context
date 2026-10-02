"""Rebuild summaries from registered text snapshots, not from old model stage caches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from collection_context.application.contracts import ContextError, digest, valid_id
from collection_context.library.index import artifact_bytes, audio_not_applicable, is_current
from collection_context.library.store import LibraryStore
from collection_context.processing.profiles import ModelCatalog
from collection_context.processing.stages import summary_stage
from collection_context.workflows.executor import Stage, StageOutcome, plan

VERSION = "summary_refresh_v1"
SOURCE_KINDS = ("original", "audio", "screen", "image")
OUTPUT_KINDS = ("summary", "readable")


def capture(store: LibraryStore, ref: str) -> dict[str, Any]:
    item = store.get(valid_id(ref))
    for kind in OUTPUT_KINDS:
        previous = item["artifacts"].get(kind)
        if previous is not None:
            if previous.get("owner_edit"):
                raise ContextError("owner_edit_conflict", "总结或可读内容已有人工作修改，未自动替换。")
            artifact_bytes(store, item, kind)  # Unaccepted edits must not be overwritten either.
    original = {"title": item["title"], "body": item["body"]}
    sources, gaps = [], []
    for kind in SOURCE_KINDS:
        if kind == "audio" and audio_not_applicable(item):
            continue
        artifact = item["artifacts"].get(kind)
        if artifact is None:
            continue
        if not is_current(item, artifact):
            gaps.append({"artifact": kind, "state": "stale"})
            continue
        text = artifact_bytes(store, item, kind).decode("utf-8")
        if kind == "original":
            original["body"] = text
        else:
            sources.append(
                {
                    "kind": kind,
                    "text": text,
                    "version": artifact["version"],
                    "sha256": artifact["sha256"],
                    "evidence_id": "t_" + digest([ref, kind, artifact["version"], artifact["sha256"]]),
                    "coverage": artifact["coverage"],
                }
            )
    kinds = {value["kind"] for value in sources}
    if not kinds & {"screen", "image"}:
        gaps.append({"artifact": "visual", "state": "missing"})
    if not audio_not_applicable(item) and "audio" not in kinds:
        gaps.append({"artifact": "audio", "state": "missing"})
    if not sources and not original["body"].strip():
        raise ContextError("summary_evidence_missing", "尚无当前可读证据，不能只按标题生成总结。")
    relevant = {kind: item["artifacts"].get(kind) for kind in SOURCE_KINDS}
    outputs = {kind: item["artifacts"].get(kind) for kind in OUTPUT_KINDS}
    return {
        "source_version": digest([item["content_hash"], item.get("prepared_input"), relevant]),
        "output_version": digest(outputs),
        "outputs": outputs,
        "source_artifacts": [
            {"kind": kind, "version": value["version"], "sha256": value["sha256"]}
            for kind, value in relevant.items()
            if value is not None and is_current(item, value)
        ],
        "material_ref": ref,
        "content_hash": item["content_hash"],
        "prepared_input": item.get("prepared_input"),
        "original": original,
        "sources": sources,
        "coverage": {
            "complete": not gaps
            and bool(sources)
            and all(source["coverage"].get("complete") is True for source in sources),
            "accuracy": "not_verified",
            "text_snapshot_refs": [source["evidence_id"] for source in sources],
            "text_snapshot_sources": [
                {key: source[key] for key in ("kind", "version", "sha256", "evidence_id")}
                for source in sources
            ],
            "missing_or_stale": gaps,
            "source_artifact_coverage": {source["kind"]: source["coverage"] for source in sources},
            "raw_media_reprocessed": False,
            "original_basis": "registered_artifact"
            if relevant["original"] is not None and is_current(item, relevant["original"])
            else "platform_metadata",
        },
    }


def verify(store: LibraryStore, context: dict[str, Any]) -> dict[str, Any]:
    snapshot = capture(store, context["material_ref"])
    if snapshot["source_version"] != context["source_version"]:
        raise ContextError("version_changed", "已确认的正文或输出版本已变化，未覆盖旧输入的总结。")
    if snapshot["output_version"] != context["output_version"]:
        if not all(
            value is not None and value["coverage"].get("refresh_context_id") == digest(context)
            for value in snapshot["outputs"].values()
        ):
            raise ContextError("version_changed", "总结输出已变化，未覆盖其他任务的结果。")
        snapshot["already_published"] = True
    return snapshot


def build(
    store: LibraryStore,
    resolve_secret: Callable[[str], str],
    context: dict[str, Any],
    *,
    planning: bool = False,
) -> list[Stage]:
    if (
        not isinstance(context, dict)
        or set(context)
        != {
            "schema_version",
            "workflow",
            "processor_version",
            "material_ref",
            "source_version",
            "output_version",
            "model_profiles",
            "summary_input_chars",
        }
        or type(context["schema_version"]) is not int
        or context["schema_version"] != 1
        or context["workflow"] != "summary_refresh"
        or context["processor_version"] != VERSION
        or any(
            not isinstance(context[key], str) or len(context[key]) != 64
            for key in ("source_version", "output_version")
        )
        or not isinstance(context["model_profiles"], dict)
        or set(context["model_profiles"]) != {"summary"}
    ):
        raise ContextError("invalid_extraction_plan", "正文总结任务结构或版本不兼容。")
    snapshot = verify(store, context)
    models = ModelCatalog(store)
    profile_id = context["model_profiles"]["summary"]
    if models.get(profile_id)["role"] != "summary":
        raise ContextError("model_config_changed", "总结任务只能使用固定的总结模型。")
    # Validate prompt limits BEFORE resolving a real credential.
    placeholder = models.client(profile_id, lambda _: "planning-only-placeholder")
    dependencies = tuple("text_" + source["kind"] for source in snapshot["sources"])
    source_outputs = {
        "text_" + source["kind"]: {
            "status": "ready",
            "output": {
                "kind": source["kind"],
                "text": source["text"],
                "evidence_id": source["evidence_id"],
                "artifact_version": source["version"],
            },
        }
        for source in snapshot["sources"]
    }
    combined = summary_stage(
        snapshot["material_ref"],
        snapshot["original"],
        dependencies,
        placeholder,
        source_coverage=snapshot["coverage"],
        max_input_chars=context["summary_input_chars"],
        registered_text_refs=True,
    )
    assert combined.validate is not None
    combined.validate(source_outputs)
    if not planning:
        combined = summary_stage(
            snapshot["material_ref"],
            snapshot["original"],
            dependencies,
            models.client(profile_id, resolve_secret),
            source_coverage=snapshot["coverage"],
            max_input_chars=context["summary_input_chars"],
            registered_text_refs=True,
        )
    stages = []
    for source in snapshot["sources"]:
        output = source_outputs["text_" + source["kind"]]["output"]

        def read(_values, output=output):
            verify(store, context)
            return StageOutcome(output)

        stages.append(Stage("text_" + source["kind"], digest(source), VERSION, read))
    base_invoke, base_validate = combined.invoke, combined.validate

    def validate(values):
        verify(store, context)
        assert base_validate is not None
        base_validate(values)

    def invoke(values):
        verify(store, context)
        return base_invoke(values)

    stages.append(replace(combined, invoke=invoke, validate=validate))

    def publish(values):
        result = values["summary"]
        if result["status"] not in {"ready", "partial"}:
            raise ContextError("no_extraction_output", "总结没有有效结果，旧文件保留。")
        output = result["output"]
        coverage = {
            **snapshot["coverage"],
            "summary_warnings": output.get("warnings", []),
            "complete": snapshot["coverage"]["complete"] and result["status"] == "ready",
            "refresh_context_id": digest(context),
        }
        readable = "# 原文\n\n" + snapshot["original"]["title"] + "\n\n" + snapshot["original"]["body"]
        for source in snapshot["sources"]:
            readable += f"\n\n# {source['kind']} [{source['evidence_id']}]\n\n{source['text']}"
        readable += "\n\n# 内容总结\n\n" + output["text"]
        if not output["text"].strip() or len(output["text"]) > 500_000 or len(readable) > 500_000:
            raise ContextError("summary_output_limit", "总结或可读正文超过资料上限，未截断后声称完整。")

        def save(state):
            current = verify(store, context)
            item = state["items"][context["material_ref"]]
            if current.get("already_published"):
                return {
                    "artifact_versions": {
                        kind: value["version"] for kind, value in current["outputs"].items()
                    },
                    "source_evidence_version": context["source_version"],
                    "raw_media_reprocessed": False,
                }
            published = store._write_artifacts(
                item,
                {
                    "summary": {"text": output["text"], "processor_version": VERSION, "coverage": coverage},
                    "readable": {"text": readable, "processor_version": VERSION, "coverage": coverage},
                },
                expected_content_hash=snapshot["content_hash"],
            )
            for value in published.values():
                value["source_artifacts"] = snapshot["source_artifacts"]
            return {
                "artifact_versions": {kind: value["version"] for kind, value in published.items()},
                "source_evidence_version": context["source_version"],
                "raw_media_reprocessed": False,
            }

        # Preserve the paid ledger on stale inputs; publication can fail without a new model retry.
        def check_commit():
            verify(store, context)

        published = store.transact(save, before_commit=check_commit)
        return StageOutcome(published, status=result["status"])

    stages.append(
        Stage(
            "publish",
            digest(context),
            VERSION,
            publish,
            dependencies=(*dependencies, "summary"),
            allow_partial_dependencies=True,
            cacheable=False,
        )
    )
    return stages


def prepare(store: LibraryStore, ref: str, *, max_input_chars: int = 100_000) -> dict[str, Any]:
    snapshot = capture(store, ref)
    context = {
        "schema_version": 1,
        "workflow": "summary_refresh",
        "processor_version": VERSION,
        "material_ref": ref,
        "source_version": snapshot["source_version"],
        "output_version": snapshot["output_version"],
        "model_profiles": ModelCatalog(store).pin(("summary",)),
        "summary_input_chars": max_input_chars,
    }
    stages = build(store, lambda _: "planning-only-placeholder", context, planning=True)
    return {"plan": plan(stages), "extraction": context}
