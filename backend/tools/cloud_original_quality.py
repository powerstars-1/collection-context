"""Remaining four explicitly authorized calls, using original pages and no private vault.

Development-only acceptance driver. Transport wrapping counts real dispatch and supplies
a verified CA bundle; the actual catalog, executor, extraction and owner workflows run.
No key is stored, no platform account is opened, and failure never triggers a retry.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import ssl
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPSHandler, build_opener

from cloud_model_acceptance import CountedTransport, save_json

from collection_context.application.contracts import ContextError
from collection_context.application.library_management import LibraryManagement
from collection_context.application.service import ContextService
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.models import SameOriginRedirect
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow

TOTAL_LIMIT = 8
PREVIOUS_COUNT = 4
CREDENTIAL_REF = "k_" + "0" * 32
PAGES = (
    (
        "原创教程：先分析，再落地（第1页）",
        "01 品牌定位：轻快、可信，不使用夸张营销语。",
        "02 用户分析：初学者；手机优先；阅读成本低。",
        "03 功能层级：导航 > 搜索 > 素材卡 > 详情。",
        "04 视觉规范：主色 #2563EB，辅色 #F59E0B。",
        "画布 390 x 844；卡片宽 342px；边距 24px。",
        "卡片圆角 16px；按钮高度 44px；字号 14px。",
        "网格 gap: 12px；容器 padding: 24px。",
        "先交付品牌/用户/功能/视觉四维分析，不先画界面。",
        "提示词模板：目标 + 用户 + 信息结构 + 视觉约束。",
        "明确限制：保留搜索栏，不生成无关统计图。",
    ),
    (
        "原创教程：组件状态与测试（第2页）",
        "05 实现技术：React + Tailwind CSS + Lucide。",
        "默认态：背景 #FFFFFF，文字 #18181B。",
        "选中态：边框 2px，颜色 #2563EB。",
        "禁用态：opacity: 0.45；不响应重复提交。",
        "响应式：390px 单列；768px 双列；1280px 三列。",
        "视觉核对顺序：间距 -> 层级 -> 配色 -> 状态。",
        '代码：className="grid gap-3 md:grid-cols-2"',
        "测试：0 条素材显示空态；1 条显示详情；21 条分页。",
        "禁止猜测：本页没有音频，也没有生产 API Key。",
        "提示词补充：返回组件树、交互状态和验收步骤。",
    ),
)
CORRECTION = "\n\n人工补充校正：示例验收代号为蓝莓网格390；此信息来自人工补充，不来自原画面。\n"


def baseline(root: Path) -> tuple[dict, str]:
    body = (root / "requests.json").read_bytes()
    value = json.loads(body)
    records = value.get("requests")
    if (
        value.get("count") != PREVIOUS_COUNT
        or not isinstance(records, list)
        or len(records) != PREVIOUS_COUNT
        or [record.get("number") for record in records] != [1, 2, 3, 4]
        or [record.get("role") for record in records] != ["audio", "vision", "summary", "summary"]
        or any(
            record.get("state") != "response_opened" or record.get("http_status") != 200 for record in records
        )
    ):
        raise ContextError("acceptance_baseline_invalid", "此前请求账本不符，未新增请求。")
    return value, hashlib.sha256(body).hexdigest()


def make_pages(font: Path) -> list[bytes]:
    from PIL import Image, ImageDraw, ImageFont

    face = ImageFont.truetype(str(font), 28)
    pages = []
    for lines in PAGES:
        image = Image.new("RGB", (1350, 740), "#FAFAFA")
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(lines):
            draw.text((32, 25 + row * 62), line, fill="#18181B", font=face)
        output = io.BytesIO()
        image.save(output, format="PNG")
        pages.append(output.getvalue())
    return pages


def text_check(text: str, expected: tuple[str, ...]) -> dict:
    # Literal ground truth coverage, NOT semantic accuracy or real-video recall.
    normalized = "".join(text.split()).casefold()
    found = [token for token in expected if "".join(token.split()).casefold() in normalized]
    return {
        "expected": list(expected),
        "found": found,
        "missing": [token for token in expected if token not in found],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-remaining-four", action="store_true")
    parser.add_argument("--previous-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--font", required=True, type=Path)
    args = parser.parse_args()
    if not args.allow_remaining_four:
        parser.error("Explicit remaining-four authorization required")
    old, baseline_hash = baseline(args.previous_root)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if (
        set(config) != {"enabled", "base_url", "default_model", "keychain_service"}
        or config["enabled"] is not True
        or config["base_url"] != "https://api.getwhite.cloud/v1"
        or config["default_model"] != "mimo-v2.6-flash"
        or config["keychain_service"] != "xingyue-new-api-media-remote"
    ):
        raise ContextError("acceptance_config_changed", "已授权模型路由改变；未猜测其他模型或凭据。")
    pages = make_pages(args.font)  # Validate local inputs before touching any real credential.
    root = args.output.absolute()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)  # Existing run never re-dispatches.
    # One reservation per previous authorization, including different output paths.
    # Partial/failed claims remain for explicit review; never remove and silently retry.
    with (args.previous_root / "remaining-four-claim.json").open("x", encoding="utf-8") as claim:
        claim.write(
            json.dumps({"output": str(root), "previous_sha256": baseline_hash, "total_limit": TOTAL_LIMIT})
        )
        claim.flush()
        os.fsync(claim.fileno())
    save_json(root / "ground-truth.json", {"pages": PAGES, "correction": CORRECTION, "original": True})
    save_json(root / "previous-ledger.json", {"sha256": baseline_hash, "ledger": old})
    for index, data in enumerate(pages):
        (root / f"original-page-{index + 1}.png").write_bytes(data)
    key_result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", config["keychain_service"], "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = key_result.stdout.strip() if key_result.returncode == 0 else ""
    if not key:
        raise ContextError("acceptance_credential_unavailable", "已授权钥匙串凭据不可用；未回退旧配置。")
    import certifi

    opener = build_opener(
        SameOriginRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
    )
    transport = CountedTransport(
        root, opener, api_key=key, limit=TOTAL_LIMIT, existing_records=old["requests"]
    )
    original_client = ModelCatalog.client

    def counted(catalog, identity, resolve):
        client = original_client(catalog, identity, resolve)
        client.transport = transport
        return client

    def resolve(ref):
        if ref != CREDENTIAL_REF:
            raise ContextError("acceptance_credential_scope", "测试仅解析自身固定凭据引用。")
        return key

    report = {
        "model": config["default_model"],
        "previous_requests": PREVIOUS_COUNT,
        "total_limit": TOTAL_LIMIT,
        "platform_requests": 0,
        "amount": "unknown",
        "real_speech_tested": False,
        "real_video_recall_tested": False,
    }
    store = LibraryStore.initialize(root / "原创多页质量库")
    try:
        item = store.upsert(
            {
                "native_id": "9000000000000000051",
                "title": "原创两页教程质量验证",
                "body": "原创制作的图文页；只验收图片提取和总结，不含讲话。",
                "media_type": "image",
            },
            kind="link",
            scope_id="s_original_quality",
        )["item"]
        report["material_ref"] = item["id"]
        inputs = PreparedInputs(store).prepare_images(item["id"], [(data, "image/png") for data in pages])
        for role in ("vision", "summary"):
            ModelCatalog(store).configure(
                role=role,
                base_url=config["base_url"],
                model=config["default_model"],
                credential_ref=CREDENTIAL_REF,
                timeout=180,
                parameters={"thinking": {"type": "disabled"}, "max_completion_tokens": 2400},
            )
        with patch.object(ModelCatalog, "client", counted):
            workflow = ExtractionWorkflow(store, resolve)
            job = workflow.submit(inputs, idempotency_key="original-two-pages-once", max_calls=3)
            completed = workflow.run(job["id"])
            report["extraction_job"] = completed
            save_json(root / "report.json", report)
            if completed["state"] not in {"succeeded", "partial"} or len(transport.records) != 7:
                return 1
            service = ContextService(store)
            screen = service.read(item["id"], artifact="screen", max_chars=20_000)["text"]
            summary = service.read(item["id"], artifact="summary", max_chars=20_000)["text"]
            (root / "画面提取.md").write_text(screen, encoding="utf-8")
            (root / "首次总结.md").write_text(summary, encoding="utf-8")
            report["screen_checks"] = text_check(
                screen,
                (
                    "React",
                    "Tailwind",
                    "Lucide",
                    "#2563EB",
                    "#F59E0B",
                    "342px",
                    "16px",
                    "44px",
                    "0.45",
                    "390px",
                    "768px",
                    "1280px",
                    "21",
                    "grid-cols-2",
                ),
            )
            report["summary_checks"] = text_check(
                summary, ("品牌", "用户", "功能", "视觉", "React", "Tailwind", "390", "16", "0.45")
            )
            old_screen = store.get(item["id"])["artifacts"]["screen"]
            edited = store.files.root / old_screen["path"]
            edited.write_text(screen + CORRECTION, encoding="utf-8")
            owner = LibraryManagement(store, authorize=lambda: None)
            preview = owner.preview_edit(item["id"], artifact="screen")
            owner.accept_edit(
                item["id"], artifact="screen", preview_token=preview["preview_token"], confirmed=True
            )
            source_before_refresh = store.get(item["id"])["artifacts"]["screen"]
            preview = owner.preview_summary(item["id"])
            refresh = owner.submit_summary(
                item["id"],
                preview_token=preview["preview_token"],
                idempotency_key="original-corrected-summary-once",
                fee_confirmed=True,
            )
            refreshed = workflow.run(refresh["job_id"])
            report["refresh_job"] = refreshed
            report["refresh_only_one_call"] = len(refreshed["calls"]) == 1 and all(
                call["stage"] == "summary" for call in refreshed["calls"]
            )
            report["source_unchanged_during_refresh"] = (
                store.get(item["id"])["artifacts"]["screen"] == source_before_refresh
            )
            if refreshed["state"] in {"succeeded", "partial"}:
                fresh = service.read(item["id"], artifact="summary", max_chars=20_000)
                report["refresh_coverage"] = fresh["coverage"]
                report["refresh_checks"] = text_check(fresh["text"], ("蓝莓网格390", "人工"))
                (root / "修正后总结.md").write_text(fresh["text"], encoding="utf-8")
        report["total_requests"] = len(transport.records)
        report["new_requests"] = len(transport.records) - PREVIOUS_COUNT
        report["usage"] = [call.get("usage") for job in (completed, refreshed) for call in job["calls"]]
        report["secret_matches"] = sum(
            key.encode() in file.read_bytes() for file in root.rglob("*") if file.is_file()
        )
        report["previous_ledger_unchanged"] = baseline(args.previous_root)[1] == baseline_hash
        report["quality_gate_passed"] = (
            completed["state"] == refreshed["state"] == "succeeded"
            and not any(
                report[name]["missing"] for name in ("screen_checks", "summary_checks", "refresh_checks")
            )
            and report["refresh_only_one_call"]
            and report["source_unchanged_during_refresh"]
        )
        save_json(root / "report.json", report)
        print(
            json.dumps(
                {
                    "new_requests": report["new_requests"],
                    "total_requests": report["total_requests"],
                    "screen_missing": report["screen_checks"]["missing"],
                    "summary_missing": report["summary_checks"]["missing"],
                    "refresh_missing": report.get("refresh_checks", {}).get("missing"),
                    "amount": "unknown",
                    "secret_matches": report["secret_matches"],
                    "quality_gate_passed": report["quality_gate_passed"],
                },
                ensure_ascii=False,
            )
        )
        if report["total_requests"] != TOTAL_LIMIT or report["secret_matches"] != 0:
            return 1
        return 0 if report["quality_gate_passed"] else 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
