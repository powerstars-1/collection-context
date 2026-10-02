"""P02: original deterministic 1000-record/30-task lexical retrieval benchmark.

This creates only a NEW benchmark directory, never opens a supplied user vault,
and never imports a model/source client. Answers are frozen before measurement.
Five paraphrase challenges remain scored even if the keyword system misses them.
Fresh-process timings are not physical disk-cold timings: OS caches are not purged.

Run with the existing environment and this worktree's src on PYTHONPATH:
  python tools/retrieval_acceptance.py --output-dir /absolute/new-benchmark-dir
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from collection_context.application import gateway, service
from collection_context.application.contracts import (
    ContextError,
    canonical_bytes,
    digest,
    item_id,
    relation_id,
    validate_source,
)
from collection_context.application.gateway import ReadGateway
from collection_context.application.service import ContextService
from collection_context.infrastructure import files as storage_files
from collection_context.infrastructure.files import SafeFiles
from collection_context.library import index, store
from collection_context.library.store import LibraryStore

BENCHMARK_VERSION = "p02-original-v1"
CORPUS_SIZE = 1000
TASK_COUNT = 30
OBSERVED = "2026-03-05T10:00:00+00:00"
MANIFEST = "benchmark-manifest.json"
BASE_NATIVE = 9990000000000000000
BACKGROUND = (
    "这段原创合成资料用于固定检索基准，记录演示准备、素材整理、操作顺序、参数检查和结果保存。"
    "它不是实际教程、真实收藏、模型识别结果或用户观点；每个资料身份均独立保存。"
) * 12


@dataclass(frozen=True)
class Relation:
    kind: str
    scope: str
    action_at: str | None = None


@dataclass(frozen=True)
class Artifact:
    kind: str
    text: str


@dataclass(frozen=True)
class Record:
    key: str
    title: str
    body: str
    relations: tuple[Relation, ...] = (Relation("saved", "s_benchmark_saved"),)
    artifacts: tuple[Artifact, ...] = ()
    media_type: str = "video"
    published_at: str = "2026-03-01T10:00:00+00:00"


@dataclass(frozen=True)
class Evidence:
    kind: str
    contains: str | None = None
    error: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    query: str
    expected_keys: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    source_kinds: tuple[str, ...] = ()
    scope_id: str | None = None
    since_action: str | None = None

    def arguments(self) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if self.source_kinds:
            filters["source_kinds"] = list(self.source_kinds)
        if self.scope_id:
            filters["scope_id"] = self.scope_id
        if self.since_action:
            filters.update(since=self.since_action, time_basis="action_at")
        return {"query": self.query, "limit": 5, **({"filters": filters} if filters else {})}


def corpus() -> tuple[Record, ...]:
    """No randomness, harvested content, platform credentials or expected-answer tuning."""
    anchors = (
        Record("figma", "Figma 组件变体练习", "设计稿导出 HTML，保留组件变体的布局约束。"),
        Record("comfy", "ComfyUI 基础工作流", "基础工作流由输入节点、采样节点和保存节点串联。"),
        Record("lora", "LoRA 人物一致性训练", "使用一致的样本标注，记录人物一致性与训练参数。"),
        Record(
            "whisper",
            "Whisper 转写演示",
            "音频转写的结果需要保留时间点。",
            artifacts=(Artifact("audio", "原创转写结果：这是一段普通人声的合成资料。"),),
        ),
        Record(
            "ffmpeg",
            "FFmpeg 硬字幕参数",
            "硬字幕滤镜属于本条演示。",
            artifacts=(Artifact("screen", "画面参数：subtitles=example.ass，编码器 libx264。"),),
        ),
        Record("n8n", "n8n 自动化节点", "自动化节点连接消息、筛选条件和输出动作。"),
        Record(
            "camera",
            "合成镜头提示页",
            "提示词只保留在画面产物。",
            artifacts=(Artifact("screen", "原创提示词：电影感，自然暖光，24mm 镜头，柔和对比。"),),
        ),
        Record(
            "negative",
            "合成人物提示页",
            "负面词只保留在画面产物。",
            artifacts=(
                Artifact("screen", "负面提示词：extra-fingers, distorted-hands；此行是原创测试文字。"),
            ),
        ),
        Record(
            "seed",
            "合成参数卡",
            "种子和引导尺度在画面上。",
            artifacts=(Artifact("screen", "参数 seed 314159，CFG 7.5，尺寸 1024。"),),
        ),
        Record(
            "character",
            "合成角色参考页",
            "表情词在画面提示页。",
            artifacts=(Artifact("screen", "角色定妆 三视图，neutral expression，头部端正。"),),
        ),
        Record(
            "timeline",
            "时间线剪辑演示",
            "术语分散保留。",
            artifacts=(Artifact("screen", "镜头色温设为 4200K，此页不含标题关键词。"),),
        ),
        Record(
            "button",
            "按钮设计合成片段",
            "说明分散在两个证据类型。",
            artifacts=(
                Artifact("screen", "布局采用十二列网格。"),
                Artifact("audio", "间距按八像素的整数倍递增。"),
            ),
        ),
        Record(
            "texture",
            "木纹材质练习",
            "这是没有音轨的原创图文样例。",
            artifacts=(Artifact("image", "电商景深：前景浅虚化，背景无品牌。"),),
            media_type="image",
        ),
        Record(
            "web",
            "网页动画合成片段",
            "本条包含主人自己记录的补充词。",
            artifacts=(Artifact("user_note", "视差滚动需要保持层级速度不同。"),),
        ),
        Record(
            "liked",
            "参考卡 收藏关系 甲",
            "原始条目只对应甲的来源关系。",
            relations=(Relation("liked", "s_benchmark_liked", "2026-02-10T09:00:00+00:00"),),
        ),
        Record("saved", "参考卡 收藏关系 乙", "原始条目只对应乙的来源关系。"),
        Record(
            "collection",
            "参考卡 收藏关系 丙",
            "丙属于用户指定收藏夹。",
            relations=(Relation("collection", "s_benchmark_collection"),),
        ),
        Record(
            "creator",
            "编排片段 多来源",
            "同一条作品应只返回一次，但保留两种来源。",
            relations=(Relation("creator", "s_benchmark_creator"), Relation("liked", "s_benchmark_liked")),
        ),
        Record(
            "unknown_like",
            "参考卡 收藏关系 丁",
            "丁的点赞时间未知，不可以拿发布时间代替。",
            relations=(Relation("liked", "s_benchmark_liked"),),
        ),
        Record(
            "unknown_time",
            "未提供时间 合成记录",
            "同步观察并不代表刚刚点赞。",
            relations=(Relation("liked", "s_benchmark_liked"),),
        ),
        Record("missing_audio", "片尾音乐 拍号 待处理", "只有描述元数据，音频尚未提取。"),
        Record("missing_screen", "等待提取 排版 待处理", "只有描述元数据，画面尚未提取。"),
        Record("tts", "TTS 音色合成演示", "根据输入文本选择音色并生成语音。"),
        # Deliberately separated terms must not be joined across different records.
        Record("partial_keyword", "色温快门 单独合成资料", "这一条没有另一个分散词。"),
    )
    topics = ("界面", "节点", "字幕", "纹理", "模板", "轨道", "镜头", "图标", "编排", "剪辑")
    noise = tuple(
        Record(
            f"background_{n:04d}",
            f"原创合成练习 {n:04d} {topics[n % len(topics)]}",
            f"基准资料编号 {n:04d}，题材标签 {topics[n % len(topics)]}。星轨只是另一个独立词。",
            relations=(
                Relation(("liked", "saved", "collection", "creator", "link")[n % 5], f"s_background_{n % 5}"),
            ),
        )
        for n in range(CORPUS_SIZE - len(anchors))
    )
    return tuple(
        Record(
            r.key, r.title, r.body + "\n" + BACKGROUND, r.relations, r.artifacts, r.media_type, r.published_at
        )
        for r in anchors + noise
    )


def tasks() -> tuple[Task, ...]:
    """Fixed answer tasks, including five intentionally hard paraphrase challenges."""
    return (
        Task("q01", "tool", "Figma 组件变体", ("figma",), (Evidence("original", "组件变体"),)),
        Task("q02", "tool", "ComfyUI 基础工作流", ("comfy",), (Evidence("original", "基础工作流"),)),
        Task("q03", "tool", "LoRA 人物一致性", ("lora",), (Evidence("original", "人物一致性"),)),
        Task("q04", "tool", "Whisper 转写", ("whisper",), (Evidence("audio", "转写结果", state="ready"),)),
        Task("q05", "tool", "FFmpeg 硬字幕", ("ffmpeg",), (Evidence("screen", "subtitles="),)),
        Task("q06", "tool", "n8n 自动化节点", ("n8n",), (Evidence("original", "自动化节点"),)),
        Task("q07", "normalized_tool", "ＦＩＧＭＡ 组件变体", ("figma",), (Evidence("original", "Figma"),)),
        Task(
            "q08",
            "lexical_reordering",
            "工作流 ComfyUI 基础",
            ("comfy",),
            (Evidence("original", "基础工作流"),),
        ),
        Task("q09", "screen_prompt", "电影感 24mm 暖光", ("camera",), (Evidence("screen", "电影感"),)),
        Task(
            "q10",
            "screen_prompt",
            "负面提示词 extra-fingers",
            ("negative",),
            (Evidence("screen", "extra-fingers"),),
        ),
        Task("q11", "screen_prompt", "seed 314159 CFG", ("seed",), (Evidence("screen", "314159"),)),
        Task(
            "q12",
            "screen_prompt",
            "角色定妆 三视图 neutral",
            ("character",),
            (Evidence("screen", "neutral expression"),),
        ),
        Task(
            "q13",
            "scattered_terms",
            "时间线 镜头色温",
            ("timeline",),
            (Evidence("original", "时间线"), Evidence("screen", "镜头色温")),
        ),
        Task(
            "q14",
            "scattered_terms",
            "按钮 布局 八像素",
            ("button",),
            (Evidence("original", "按钮"), Evidence("screen", "布局"), Evidence("audio", "八像素")),
        ),
        Task(
            "q15",
            "scattered_terms",
            "木纹 电商景深",
            ("texture",),
            (
                Evidence("original", "木纹"),
                Evidence("image", "电商景深"),
                Evidence("audio", error="artifact_not_applicable", state="not_applicable"),
            ),
        ),
        Task(
            "q16",
            "scattered_terms",
            "网页 视差",
            ("web",),
            (Evidence("original", "网页"), Evidence("user_note", "视差")),
        ),
        Task(
            "q17",
            "source",
            "参考卡 收藏关系",
            ("liked", "unknown_like"),
            (Evidence("original", "收藏关系"),),
            source_kinds=("liked",),
        ),
        Task(
            "q18",
            "source",
            "参考卡 收藏关系",
            ("saved",),
            (Evidence("original", "乙"),),
            source_kinds=("saved",),
        ),
        Task(
            "q19",
            "source",
            "参考卡 收藏关系",
            ("collection",),
            (Evidence("original", "丙"),),
            scope_id="s_benchmark_collection",
        ),
        Task(
            "q20",
            "source_dedup",
            "编排片段",
            ("creator",),
            (Evidence("original", "多来源"),),
            source_kinds=("creator",),
        ),
        Task(
            "q21",
            "recent_relationship",
            "参考卡 收藏关系",
            ("liked",),
            (Evidence("original", "甲"),),
            source_kinds=("liked",),
            since_action="2026-02-01T00:00:00Z",
        ),
        Task(
            "q22",
            "unknown_action_time",
            "未提供时间 同步观察",
            (),
            source_kinds=("liked",),
            since_action="2026-02-01T00:00:00Z",
        ),
        Task(
            "q23",
            "missing",
            "片尾音乐 拍号",
            ("missing_audio",),
            (Evidence("original", "片尾音乐"), Evidence("audio", error="artifact_missing", state="missing")),
        ),
        Task(
            "q24",
            "missing",
            "等待提取 排版",
            ("missing_screen",),
            (Evidence("original", "等待提取"), Evidence("screen", error="artifact_missing", state="missing")),
        ),
        Task("q25", "no_result_cross_record", "色温快门 星轨", ()),
        Task("q26", "paraphrase", "把界面草图变成网页", ("figma",), (Evidence("original", "Figma"),)),
        Task("q27", "paraphrase", "让视频开口讲话", ("tts",), (Evidence("original", "音色"),)),
        Task("q28", "paraphrase", "把人声变成文字", ("whisper",), (Evidence("audio", "转写结果"),)),
        Task("q29", "paraphrase", "不用敲代码把应用连起来", ("n8n",), (Evidence("original", "自动化节点"),)),
        Task(
            "q30",
            "paraphrase",
            "让照片中的脸一直保持同一个人",
            ("lora",),
            (Evidence("original", "人物一致性"),),
        ),
    )


def identities() -> dict[str, str]:
    return {record.key: item_id("douyin", str(BASE_NATIVE + n)) for n, record in enumerate(corpus())}


def fixture_identity() -> dict[str, Any]:
    return {
        "version": BENCHMARK_VERSION,
        "corpus_size": CORPUS_SIZE,
        "task_count": TASK_COUNT,
        "corpus_sha256": digest([asdict(record) for record in corpus()]),
        "tasks_sha256": digest([asdict(task) for task in tasks()]),
    }


def build_library(root: Path) -> dict[str, Any]:
    """One real Store transaction seeds fixed fixtures; derived index uses its normal path.

    Construction is not an ingest/sync performance measurement. No mock index,
    hand-written CURRENT pointer, changed permissions, or fake gateway is used.
    """
    library = LibraryStore.initialize(root)
    records = corpus()
    try:

        def seed(state):
            for n, record in enumerate(records):
                source = validate_source(
                    {
                        "native_id": str(BASE_NATIVE + n),
                        "title": record.title,
                        "body": record.body,
                        "author": "原创检索基准作者",
                        "media_type": record.media_type,
                        "published_at": record.published_at,
                    }
                )
                relations = {}
                for relation in record.relations:
                    rid = relation_id(source["id"], relation.kind, relation.scope)
                    relations[rid] = {
                        "id": rid,
                        "kind": relation.kind,
                        "scope_id": relation.scope,
                        "first_observed_at": OBSERVED,
                        "last_observed_at": OBSERVED,
                        "action_at": relation.action_at,
                        "action_basis": "原创合成平台字段" if relation.action_at else None,
                    }
                state["items"][source["id"]] = {
                    **source,
                    "content_hash": digest(source),
                    "first_observed_at": OBSERVED,
                    "last_observed_at": OBSERVED,
                    "first_observed_generation": 1,
                    "first_observed_principal": "benchmark_owner",
                    "relations": relations,
                    "artifacts": {},
                    "excluded": False,
                }
            return len(state["items"])

        count = library.transact(seed)
        for record in records:
            if record.artifacts:
                item = library.get(identities()[record.key])
                library.save_bundle(
                    item["id"],
                    {
                        artifact.kind: {
                            "text": artifact.text,
                            "processor_version": "original-benchmark-v1",
                            "coverage": {"complete": False, "accuracy": "synthetic_not_verified"},
                        }
                        for artifact in record.artifacts
                    },
                    expected_content_hash=item["content_hash"],
                )
        snapshot = library.snapshot()
        return {
            "workspace_id": library.workspace_id,
            "record_count": count,
            "artifact_count": sum(len(item["artifacts"]) for item in snapshot["items"].values()),
        }
    finally:
        library.close()


def p95(samples: list[float]) -> float | None:
    """Nearest-rank; no interpolation or removal of slow/error samples."""
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1] if samples else None


def evaluate(task: Task, response: dict, access: ReadGateway) -> dict[str, Any]:
    expected = [identities()[key] for key in task.expected_keys]
    data = response.get("data") if response.get("ok") else None
    actual = [item["material_ref"] for item in data["items"]] if data else []
    unexpected = [ref for ref in actual if ref not in expected]
    missing = [ref for ref in expected if ref not in actual]
    duplicates = sorted({ref for ref in actual if actual.count(ref) > 1})
    records = {ref: record for ref, record in zip(identities().values(), corpus(), strict=True)}
    metadata_errors = []
    for item in data["items"] if data else []:
        ref = item["material_ref"]
        if ref not in expected:
            continue
        record = records[ref]
        native = str(BASE_NATIVE + next(n for n, r in enumerate(corpus()) if r.key == record.key))
        source_url = f"https://www.douyin.com/{'video' if record.media_type == 'video' else 'note'}/{native}"
        actual_relations = {
            (r.get("kind"), r.get("scope_id"), r.get("action_at")) for r in item.get("relations", [])
        }
        expected_relations = {(r.kind, r.scope, r.action_at) for r in record.relations}
        if (
            item.get("source_url") != source_url
            or item.get("title") != record.title
            or item.get("author") != "原创检索基准作者"
            or item.get("media_type") != record.media_type
            or item.get("platform") != "douyin"
            or actual_relations != expected_relations
        ):
            metadata_errors.append(ref)
    checks = []
    for ref in actual:
        if ref not in expected:
            continue
        for evidence in task.evidence:
            result = access.dispatch(
                "read_collection", {"material_ref": ref, "artifact": evidence.kind, "max_chars": 4000}
            )
            observed_error = result["error"]["code"] if not result["ok"] else None
            check = observed_error == evidence.error
            if evidence.error is None:
                check &= bool(result["ok"] and result["data"]["material_ref"] == ref)
                check &= evidence.contains is None or bool(
                    result["ok"] and evidence.contains in result["data"]["text"]
                )
            if evidence.state is not None:
                status = access.dispatch("collection_status", {"material_ref": ref})
                check &= bool(
                    status["ok"] and status["data"]["artifacts"][evidence.kind]["state"] == evidence.state
                )
            checks.append(
                {
                    "material_ref": ref,
                    "artifact": evidence.kind,
                    "passed": bool(check),
                    "error": observed_error,
                }
            )
    successful_search = bool(response.get("ok"))
    evidence_ok = all(check["passed"] for check in checks)
    zero_models = bool(
        data is not None and data.get("model_requests") == 0 and data.get("semantic_search") is False
    )
    return {
        "id": task.id,
        "category": task.category,
        "query": task.query,
        "filters": task.arguments().get("filters", {}),
        "expected_refs": expected,
        "actual_top5": actual,
        "missing_refs": missing,
        "unexpected_refs": unexpected,
        "duplicate_refs": duplicates,
        "metadata_association_errors": metadata_errors,
        "top5_hit": successful_search and not missing if expected else None,
        "no_result_correct": successful_search and not actual if not expected else None,
        "evidence_checks": checks,
        "evidence_passed": evidence_ok,
        "passed": successful_search
        and not missing
        and not unexpected
        and not duplicates
        and not metadata_errors
        and evidence_ok
        and zero_models,
        "search_error": response["error"]["code"] if not successful_search else None,
        "total_matches": data["total_matches"] if data else None,
        "zero_model_calls": zero_models,
    }


def _code_fingerprint() -> dict[str, str]:
    result = {"benchmark_tool": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    for module in (gateway, service, index, store, storage_files):
        source_file = module.__file__
        if source_file is None:
            raise ContextError("benchmark_source_unavailable", "不能核对基准源码版本。")
        result[module.__name__] = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
    return result


def _validate_fixture(output: Path) -> Path:
    with SafeFiles(output) as files:
        metadata = json.loads(files.read(MANIFEST, max_bytes=65_536))
    if metadata.get("fixture") != fixture_identity() or not isinstance(metadata.get("workspace_id"), str):
        raise ContextError("benchmark_fixture_invalid", "不是此固定基准的自有目录。")
    library = output / "library"
    handle = LibraryStore(library)
    try:
        if handle.workspace_id != metadata["workspace_id"] or set(handle.snapshot()["items"]) != set(
            identities().values()
        ):
            raise ContextError("benchmark_fixture_invalid", "基准库身份或资料集合不匹配。")
    finally:
        handle.close()
    return library


def _cold_child(output: Path, task_id: str) -> dict[str, Any]:
    task = next((task for task in tasks() if task.id == task_id), None)
    if task is None:
        raise ContextError("benchmark_task_invalid", "未知固定基准任务。")
    # Validation is reported separately from open/query; this first validation
    # may warm OS pages, so neither number is described as disk-cold.
    started = time.perf_counter()
    library_root = _validate_fixture(output)
    validated = time.perf_counter()
    handle = LibraryStore(library_root)
    try:
        access = ReadGateway(ContextService(handle))
        opened = time.perf_counter()
        response = access.dispatch("search_collections", task.arguments())
        finished = time.perf_counter()
        return {
            "id": task.id,
            "validation_ms": (validated - started) * 1000,
            "open_and_first_query_ms": (finished - validated) * 1000,
            "first_query_ms": (finished - opened) * 1000,
            "ok": response["ok"],
            "actual_top5": [item["material_ref"] for item in response["data"]["items"]]
            if response["ok"]
            else [],
            "error": response["error"]["code"] if not response["ok"] else None,
            "zero_model_calls": bool(response["ok"] and response["data"]["model_requests"] == 0),
        }
    finally:
        handle.close()


def measure_cold(output: Path) -> dict[str, Any]:
    rows = []
    script = Path(__file__).resolve()
    env = {
        "PYTHONPATH": str(script.parent.parent / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.defpath,
    }
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    for task in tasks():
        started = time.perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--cold-child", task.id, "--output-dir", str(output)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
            elapsed = (time.perf_counter() - started) * 1000
            child = (
                json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 65_536 else None
            )
            if not isinstance(child, dict) or child.get("id") != task.id:
                raise ValueError
            rows.append({**child, "process_end_to_end_ms": elapsed})
        except (subprocess.TimeoutExpired, OSError, ValueError):
            rows.append(
                {
                    "id": task.id,
                    "ok": False,
                    "error": "cold_process_unavailable",
                    "process_end_to_end_ms": (time.perf_counter() - started) * 1000,
                }
            )
    return {
        "state": "measured",
        "samples": rows,
        "process_count": len(rows),
        "process_end_to_end_p95_ms": p95([row["process_end_to_end_ms"] for row in rows]),
        "first_query_p95_ms": p95([row["first_query_ms"] for row in rows if "first_query_ms" in row]),
        "errors": sum(not row["ok"] for row in rows),
        "method": "one fresh Python process per task; end-to-end includes imports and fixture identity validation; OS/page cache not purged; first query follows fixture metadata validation",
    }


def run_acceptance(output: Path, *, warm_rounds: int = 3, cold_processes: bool = True) -> dict[str, Any]:
    if (
        not output.is_absolute()
        or output == Path(output.anchor)
        or ".." in output.parts
        or output.exists()
        or output.is_symlink()
    ):
        raise ContextError("benchmark_output_not_new", "只允许新的绝对基准目录；不读取或覆盖已有库。")
    if type(warm_rounds) is not int or not 1 <= warm_rounds <= 10 or type(cold_processes) is not bool:
        raise ContextError("benchmark_argument", "基准轮数或冷启动开关无效。")
    before = _code_fingerprint()
    output.mkdir(mode=0o700, parents=True)
    started = time.perf_counter()
    fixture = build_library(output / "library")
    built_ms = (time.perf_counter() - started) * 1000
    with SafeFiles(output) as files:
        files.write(
            MANIFEST,
            canonical_bytes({"fixture": fixture_identity(), "workspace_id": fixture["workspace_id"]}),
        )
    handle = LibraryStore(output / "library")
    try:
        access = ReadGateway(ContextService(handle))
        baseline = handle.snapshot()
        rows = []
        for task in tasks():
            # Correctness/reading pass also explicitly warms every fixed query.
            rows.append(evaluate(task, access.dispatch("search_collections", task.arguments()), access))
        timings = []
        for round_index in range(warm_rounds):
            # Rotate, rather than always measure the same query first.
            ordered = tasks()[round_index:] + tasks()[:round_index]
            for task in ordered:
                began = time.perf_counter()
                response = access.dispatch("search_collections", task.arguments())
                elapsed = (time.perf_counter() - began) * 1000
                actual = (
                    [item["material_ref"] for item in response["data"]["items"]] if response["ok"] else []
                )
                prior = next(row for row in rows if row["id"] == task.id)
                timings.append(
                    {
                        "id": task.id,
                        "round": round_index,
                        "ms": elapsed,
                        "ok": response["ok"],
                        "same_top5": actual == prior["actual_top5"],
                    }
                )
        unchanged = handle.snapshot() == baseline
    finally:
        handle.close()
    cold: dict[str, Any] = (
        measure_cold(output)
        if cold_processes
        else {"state": "not_run", "reason": "explicit offline test/diagnostic option"}
    )
    stable = _code_fingerprint() == before
    warm_p95 = p95([entry["ms"] for entry in timings])
    correct = sum(row["passed"] for row in rows)
    wrong = sum(
        len(row["unexpected_refs"]) + len(row["metadata_association_errors"]) + len(row["duplicate_refs"])
        for row in rows
    )
    if cold["state"] == "measured":
        expected_top5 = {row["id"]: row["actual_top5"] for row in rows}
        for sample in cold["samples"]:
            sample["same_top5_as_warm"] = sample.get("actual_top5") == expected_top5[sample["id"]]
    acceptance_complete = bool(
        cold["state"] == "measured"
        and cold["errors"] == 0
        and stable
        and len(cold["samples"]) == TASK_COUNT
        and all(sample["ok"] and sample["same_top5_as_warm"] for sample in cold["samples"])
    )
    report = {
        "benchmark": fixture_identity(),
        "fixture_only": True,
        "real_user_relevance_validated": False,
        "hardware": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "source_fingerprints": before,
        "source_stable_during_run": stable,
        "build": {**fixture, "elapsed_ms": built_ms, "included_in_query_timings": False},
        "accuracy": {
            "tasks": len(rows),
            "tasks_correct": correct,
            "positive_tasks": sum(bool(row["expected_refs"]) for row in rows),
            "positive_top5_hits": sum(row["top5_hit"] is True for row in rows),
            "no_result_tasks": sum(not row["expected_refs"] for row in rows),
            "no_result_correct": sum(row["no_result_correct"] is True for row in rows),
            "wrong_associations": wrong,
            "failed_task_ids": [row["id"] for row in rows if not row["passed"]],
            "rows": rows,
        },
        "warm": {
            "rounds": warm_rounds,
            "samples": timings,
            "sample_count": len(timings),
            "p95_ms": warm_p95,
            "max_ms": max(entry["ms"] for entry in timings),
            "method": "one fixed correctness/reading warm-up pass then unchanged real Gateway search; nearest-rank P95; no error/slow samples dropped",
        },
        "cold": cold,
        "acceptance_complete": acceptance_complete,
        "read_only_measurement": unchanged,
        "retrieval": "lexical",
        "semantic_search": False,
        "model_requests": 0,
        "platform_requests": 0,
        "targets": {
            "tasks_correct_minimum": 27,
            "wrong_associations_maximum": 0,
            "warm_p95_maximum_ms": 2000,
        },
        "targets_met": bool(
            acceptance_complete
            and correct >= 27
            and wrong == 0
            and warm_p95 is not None
            and warm_p95 <= 2000
            and unchanged
            and stable
            and all(entry["ok"] and entry["same_top5"] for entry in timings)
        ),
        "limitations": [
            "Synthetic fixed corpus, not a user relevance study or all-vault scalability proof.",
            "Five expected-answer paraphrase tasks stay scored; literal retrieval is not semantic recall.",
            "Fresh Python processes do not evict OS/filesystem caches.",
            "Corpus construction uses a real Store bulk transaction; not an ingestion/synchronization throughput claim.",
            "Missing extraction is tested as missing/not_applicable, never complete.",
        ],
    }
    with SafeFiles(output) as files:
        files.write("retrieval-report.json", canonical_bytes(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--warm-rounds", type=int, default=3)
    parser.add_argument(
        "--skip-cold", action="store_true", help="Diagnostic only; output explicitly marks cold not measured"
    )
    parser.add_argument("--cold-child", choices=[task.id for task in tasks()], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = (
            _cold_child(args.output_dir, args.cold_child)
            if args.cold_child
            else run_acceptance(
                args.output_dir, warm_rounds=args.warm_rounds, cold_processes=not args.skip_cold
            )
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if args.cold_child or report["targets_met"] else 2
    except (ContextError, OSError, ValueError):
        print(json.dumps({"state": "failed", "error": "benchmark_failed", "details_redacted": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
