"""Original legacy-shaped fixtures: read evidence without migration or paid work."""

from __future__ import annotations

import hashlib
import json
import os
import socket

import pytest

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.files import SafeFiles
from collection_context.library import legacy_layout
from collection_context.library.legacy_layout import LegacyLayoutReader, reference
from collection_context.library.store import LibraryStore

INBOX = "00_素材收件箱/抖音"
ATTACHMENT_NAMES = {
    "audio": "音频转写.md",
    "screen": "画面文字.md",
    "summary": "内容总结.md",
    "readable": "可读内容.md",
    "image": "图片提取.md",
}


def write_card(vault, *, native_id="1201", name="原创教程.md", attachment=None, body=None):
    path = vault / INBOX / name
    path.parent.mkdir(parents=True, exist_ok=True)
    attachment = attachment if attachment is not None else f"80_附件/抖音/{native_id}"
    if body is None:
        body = (
            f"# 原创教程 {native_id}\n\n## 基本信息\n"
            f"- 平台：抖音\n- 作者：原创样例作者\n- 作品ID：{native_id}\n"
            f"- 链接：https://www.douyin.com/video/{native_id}?example=discard#fragment\n"
            f"- 来源：抖音收藏\n- 入库时间：2026-10-01T00:00:00Z\n"
            f"- 附件目录：{attachment}\n\n## 素材完整性\n- 内容完整：true\n"
            "\n## 原始材料\n原创文字：UI tutorial 390×844，蓝色按钮。\n"
            "\n## 用户备注\n用户自己的偏好备注，不是作者原文。\n"
        )
    path.write_text(body, encoding="utf-8")
    return path, reference(path.relative_to(vault).as_posix())


def inventory(root):
    return {
        p.relative_to(root).as_posix(): (
            "directory" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        )
        for p in root.rglob("*")
    }


def failure(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


@pytest.fixture
def legacy_vault(tmp_path):
    root = tmp_path / "原项目目录"
    vault = root / "content-vault"
    card, ref = write_card(vault)
    attachment = vault / "80_附件/抖音/1201"
    attachment.mkdir(parents=True)
    bodies = {
        "original": "原创文字：UI tutorial 390×844，蓝色按钮。",
        "user_note": "用户自己的偏好备注，不是作者原文。",
        "audio": "音频证据：先生成线框，再调颜色。",
        "screen": "画面证据：rounded-xl，gap-4，390×844。",
        "summary": "总结证据：先线框后配色，旧总结未核验。",
        "readable": "可读正文：完整说明，不等于视频百分百覆盖。",
        "image": "图片证据：一张蓝色按钮示意图。",
    }
    for kind, filename in ATTACHMENT_NAMES.items():
        (attachment / filename).write_text(bodies[kind], encoding="utf-8")
    (root / ".env.local").write_text("SYNTHETIC_SECRET=do-not-read", encoding="utf-8")
    private = vault / "90_Agent协作"
    private.mkdir()
    (private / "private.md").write_text("private sentinel", encoding="utf-8")
    (attachment / "video.mp4").write_bytes(b"synthetic media never read")
    return root, vault, card, ref, attachment, bodies


@pytest.mark.parametrize("direct_vault", [False, True])
def test_read_all_evidence_and_search_without_writes_or_network(legacy_vault, monkeypatch, direct_vault):
    root, vault, card, ref, attachment, bodies = legacy_vault
    before = inventory(root)
    opened = []
    real_read = SafeFiles.read

    def trace_read(self, path, **kwargs):
        opened.append(path)
        return real_read(self, path, **kwargs)

    monkeypatch.setattr(SafeFiles, "read", trace_read)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network is forbidden"))
    reader = LegacyLayoutReader(vault if direct_vault else root)
    try:
        for kind, body in bodies.items():
            if kind == 'readable':
                failure('invalid_artifact', lambda: reader.read(ref, artifact=kind))
                continue
            result = reader.read(ref, artifact=kind)
            assert result["text"] == body
            assert result["content_untrusted"] and result["model_requests"] == 0
            assert result["coverage"]["complete"] is None
            assert result["coverage"]["accuracy"] == "not_verified"
        result = reader.search("rounded-xl")
        assert result["items"][0]["material_ref"] == ref
        assert "screen" in result["items"][0]["matched_artifacts"]
        assert reader.status(ref)["artifacts"].keys() == bodies.keys() - {'readable'}
        assert reader.overview()["model_requests"] == 0
    finally:
        reader.close()
    assert inventory(root) == before
    allowed = {card.relative_to(vault).as_posix()} | {
        (attachment / name).relative_to(vault).as_posix() for name in ATTACHMENT_NAMES.values()
    }
    assert set(opened) <= allowed


def test_unknown_action_dates_not_inferred_from_ingestion_mtime_or_claimed_completeness(legacy_vault):
    root, _, card, ref, _, _ = legacy_vault
    os.utime(card, (1_790_000_000, 1_790_000_000))
    reader = LegacyLayoutReader(root)
    try:
        row = reader.list_items()["items"][0]
        assert row["published_at"] is row["first_observed_at"] is row["last_observed_at"] is None
        assert row["relations"] == [{"kind": "saved", "scope_id": "s_legacy_saved", "action_at": None}]
        assert not row["action_time_known"]
        assert reader.status(ref)["scope_coverage"]["state"] == "unknown"
        filtered = reader.search("原创", filters={"since": "2026-10-01T00:00:00Z", "time_basis": "action_at"})
        assert filtered["items"] == []
        assert reader.search("原创", filters={"source_kinds": ["saved"]})["items"]
        assert reader.search("原创", filters={"source_kinds": ["liked"]})["items"] == []
        assert reader.read(ref)["source_url"] == "https://www.douyin.com/video/1201"
    finally:
        reader.close()


def test_search_matches_across_evidence_and_continuations_bind_query_and_file_version(legacy_vault):
    root, vault, _, ref, attachment, _ = legacy_vault
    _, second = write_card(vault, native_id="1202", name="第二份教程.md")
    reader = LegacyLayoutReader(root)
    try:
        assert reader.search("蓝色 rounded-xl")["items"][0]["material_ref"] == ref
        first = reader.search("原创", limit=1)
        assert first["next_offset"] == 1 and first["total_matches"] == 2
        failure("version_required", lambda: reader.search("原创", offset=1))
        second_page = reader.search("原创", limit=1, offset=1, version=first["version"])
        assert {first["items"][0]["material_ref"], second_page["items"][0]["material_ref"]} == {ref, second}
        failure("version_changed", lambda: reader.search("教程", offset=1, version=first["version"]))
        (attachment / "画面文字.md").write_text("changed evidence", encoding="utf-8")
        failure("version_changed", lambda: reader.search("原创", offset=1, version=first["version"]))
    finally:
        reader.close()


def test_read_continuation_cannot_mix_old_card_or_artifact(legacy_vault):
    root, _, card, ref, attachment, bodies = legacy_vault
    reader = LegacyLayoutReader(root)
    try:
        first = reader.read(ref, artifact="screen", max_chars=8)
        rest = reader.read(ref, artifact="screen", offset=8, version=first["version"])
        assert first["text"] + rest["text"] == bodies["screen"]
        failure("version_required", lambda: reader.read(ref, artifact="screen", offset=8))
        card.write_text(card.read_text().replace("蓝色按钮", "红色按钮"), encoding="utf-8")
        failure(
            "version_changed", lambda: reader.read(ref, artifact="screen", offset=8, version=first["version"])
        )
        first = reader.read(ref, artifact="screen", max_chars=8)
        (attachment / "画面文字.md").write_text("new changed screen evidence", encoding="utf-8")
        failure(
            "version_changed", lambda: reader.read(ref, artifact="screen", offset=8, version=first["version"])
        )
    finally:
        reader.close()


@pytest.mark.parametrize(
    "form", ["../../80_附件/抖音/1201", "[素材](../../80_附件/抖音/1201)", "[[80_附件/抖音/1201|素材]]"]
)
def test_explicit_supported_markdown_links_resolve_inside_same_vault(legacy_vault, form):
    root, vault, _, _, _, bodies = legacy_vault
    _, ref = write_card(vault, attachment=form)
    reader = LegacyLayoutReader(root)
    try:
        assert reader.read(ref, artifact="screen")["text"] == bodies["screen"]
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("attachment", "code"),
    [
        ("80_附件/抖音/91201", "attachment_identity_mismatch"),
        ("../../../../outside", "forbidden_path"),
        ("80_附件/小红书/1201", "forbidden_path"),
        ("file:///tmp/1201", "forbidden_path"),
        ("80_附件/抖音/%31%32%30%31", "forbidden_path"),
    ],
)
def test_invalid_association_does_not_retrieve_other_item_or_hide_original(legacy_vault, attachment, code):
    root, vault, _, _, _, _ = legacy_vault
    _, ref = write_card(vault, attachment=attachment)
    reader = LegacyLayoutReader(root)
    try:
        assert "蓝色按钮" in reader.read(ref)["text"]
        failure(code, lambda: reader.read(ref, artifact="screen"))
        assert reader.status(ref)["artifacts"]["screen"]["state"] == "unavailable"
        found = reader.search("原创")
        assert found["items"] and any(g["code"] == code for g in found["integrity_gaps"])
    finally:
        reader.close()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_unsafe_card_and_artifact_never_expose_external_body(legacy_vault, tmp_path, unsafe_kind):
    root, vault, _, ref, attachment, _ = legacy_vault
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET_UNRELATED_BODY", encoding="utf-8")
    unsafe_card = vault / INBOX / "危险卡.md"
    unsafe_artifact = attachment / "画面文字.md"
    unsafe_artifact.unlink()
    if unsafe_kind == "symlink":
        unsafe_card.symlink_to(outside)
        unsafe_artifact.symlink_to(outside)
    else:
        os.link(outside, unsafe_card)
        os.link(outside, unsafe_artifact)
    reader = LegacyLayoutReader(root)
    try:
        failure("forbidden_path", lambda: reader.read(reference(unsafe_card.relative_to(vault).as_posix())))
        failure("forbidden_path", lambda: reader.read(ref, artifact="screen"))
        result = reader.search("SECRET_UNRELATED_BODY")
        assert not result["items"] and result["coverage"]["partial"]
        assert "SECRET_UNRELATED_BODY" not in json.dumps(reader.list_items())
    finally:
        reader.close()


@pytest.mark.parametrize("body, code", [(b" \n\n", "artifact_missing"), (b"\xff\xfe", "invalid_artifact")])
def test_empty_and_invalid_utf8_artifacts_are_not_replaced_with_summary(legacy_vault, body, code):
    root, _, _, ref, attachment, _ = legacy_vault
    (attachment / "音频转写.md").write_bytes(body)
    reader = LegacyLayoutReader(root)
    try:
        failure(code, lambda: reader.read(ref, artifact="audio"))
        state = reader.status(ref)["artifacts"]["audio"]
        assert state["state"] != "ready" and state["issue"] == code
        assert reader.read(ref, artifact="summary")["text"].startswith("总结证据")
    finally:
        reader.close()


@pytest.mark.parametrize(
    "bad_field",
    [
        "标题: [one, two]",
        "标题: {nested: value}",
        "标题: !!str execute",
        "标题: &anchor value",
        "标题: |\n  multiline",
    ],
)
def test_complex_header_is_rejected_as_unsupported_not_executed_or_stringified(legacy_vault, bad_field):
    root, _, card, ref, _, _ = legacy_vault
    card.write_text(f"---\n{bad_field}\n---\n" + card.read_text(), encoding="utf-8")
    reader = LegacyLayoutReader(root)
    try:
        failure("invalid_legacy_card", lambda: reader.read(ref))
    finally:
        reader.close()


@pytest.mark.parametrize("suffix", ["\n## 原文文案\nAnother original\n", "\n## 基本信息\n- 作者：另一人\n"])
def test_duplicate_meaningful_sections_fail_instead_of_selecting_arbitrarily(legacy_vault, suffix):
    root, _, card, ref, _, _ = legacy_vault
    card.write_text(card.read_text() + suffix, encoding="utf-8")
    reader = LegacyLayoutReader(root)
    try:
        failure("legacy_section_ambiguous", lambda: reader.read(ref))
    finally:
        reader.close()


def test_conflicting_frontmatter_and_basic_identity_are_rejected(legacy_vault):
    root, _, card, ref, _, _ = legacy_vault
    card.write_text("---\n作者: 别的作者\n---\n" + card.read_text(), encoding="utf-8")
    reader = LegacyLayoutReader(root)
    try:
        failure("legacy_section_ambiguous", lambda: reader.read(ref))
    finally:
        reader.close()


@pytest.mark.parametrize("direct_vault", [False, True])
def test_new_library_cannot_bypass_exclusion_via_legacy_mode(tmp_path, direct_vault):
    workspace = tmp_path / "new"
    store = LibraryStore.initialize(workspace)
    try:
        item = store.upsert(
            {"native_id": "1201", "body": "Excluded original"}, kind="saved", scope_id="s_saved"
        )["item"]
        store.exclude(item["id"])
    finally:
        store.close()
    write_card(workspace / "content-vault")
    before = inventory(workspace)
    failure(
        "legacy_mode_not_allowed",
        lambda: LegacyLayoutReader(workspace / "content-vault" if direct_vault else workspace),
    )
    assert inventory(workspace) == before


@pytest.mark.parametrize("query", [None, True, [], {}, "", "   "])
def test_search_invalid_query_does_not_turn_into_unfiltered_listing(legacy_vault, query):
    root, *_ = legacy_vault
    reader = LegacyLayoutReader(root)
    try:
        failure("invalid_argument", lambda: reader.search(query))
    finally:
        reader.close()


def test_invalid_url_port_is_a_domain_error_and_scan_can_report_gap(legacy_vault):
    root, _, card, ref, _, _ = legacy_vault
    card.write_text(card.read_text().replace("www.douyin.com/", "www.douyin.com:invalid/"), encoding="utf-8")
    reader = LegacyLayoutReader(root)
    try:
        failure("invalid_legacy_card", lambda: reader.read(ref))
        result = reader.list_items()
        assert not result["items"] and result["coverage"]["partial"]
    finally:
        reader.close()


def test_image_card_reports_audio_not_applicable_even_if_stray_audio_exists(legacy_vault):
    root, _, card, ref, _, _ = legacy_vault
    card.write_text(card.read_text().replace("/video/1201", "/note/1201"), encoding="utf-8")
    reader = LegacyLayoutReader(root)
    try:
        failure("artifact_not_applicable", lambda: reader.read(ref, artifact="audio"))
        assert reader.status(ref)["artifacts"]["audio"]["state"] == "not_applicable"
    finally:
        reader.close()


def test_scan_budget_reports_partial_instead_of_silent_complete(legacy_vault, monkeypatch):
    root, _, _, _, _, _ = legacy_vault
    reader = LegacyLayoutReader(root)
    monkeypatch.setattr(legacy_layout, "MAX_SCAN_BYTES", 20)
    try:
        result = reader.list_items()
        assert result["coverage"]["partial"]
        assert any(g["code"] == "scan_limit" for g in result["integrity_gaps"])
    finally:
        reader.close()


@pytest.mark.parametrize("operation", ["read", "status", "search"])
def test_card_edit_between_association_and_evidence_read_never_returns_mixed_result(
    legacy_vault, monkeypatch, operation
):
    root, _, card, ref, _, _ = legacy_vault
    reader = LegacyLayoutReader(root)
    original_read = reader.safe.read
    changed = False

    def edit_during_read(path, **kwargs):
        nonlocal changed
        if path.endswith("画面文字.md") and not changed:
            changed = True
            card.write_text(card.read_text().replace("蓝色按钮", "另一个版本"), encoding="utf-8")
        return original_read(path, **kwargs)

    monkeypatch.setattr(reader.safe, "read", edit_during_read)
    try:
        if operation == "read":
            failure("version_changed", lambda: reader.read(ref, artifact="screen"))
        elif operation == "status":
            failure("version_changed", lambda: reader.status(ref))
        else:
            result = reader.search("原创")
            assert not result["items"] and result["coverage"]["partial"]
            assert any(g["code"] == "version_changed" for g in result["integrity_gaps"])
        assert changed
    finally:
        reader.close()


def test_blank_card_is_a_gap_while_explicit_card_with_empty_original_is_missing(legacy_vault):
    root, vault, card, ref, _, _ = legacy_vault
    (vault / INBOX / "空白.md").write_text(" \n", encoding="utf-8")
    card.write_text(
        card.read_text().replace("原创文字：UI tutorial 390×844，蓝色按钮。", ""), encoding="utf-8"
    )
    reader = LegacyLayoutReader(root)
    try:
        result = reader.list_items()
        assert result["total_items"] == 1 and result["coverage"]["partial"]
        assert any(g["code"] == "invalid_legacy_card" for g in result["integrity_gaps"])
        assert reader.status(ref)["artifacts"]["original"]["state"] == "missing"
        failure("artifact_missing", lambda: reader.read(ref))
    finally:
        reader.close()


@pytest.mark.parametrize("marker_place", ["root", "vault"])
def test_live_reader_rejects_new_managed_layout_markers(legacy_vault, marker_place):
    root, vault, _, ref, _, _ = legacy_vault
    reader = LegacyLayoutReader(root)
    try:
        ((root if marker_place == "root" else vault) / ".context").mkdir()
        for action in (lambda: reader.read(ref), lambda: reader.status(ref), reader.list_items):
            failure("legacy_mode_not_allowed", action)
    finally:
        reader.close()


def test_unsafe_non_markdown_directory_is_a_reported_gap(legacy_vault, tmp_path):
    root, vault, _, _, _, _ = legacy_vault
    external = tmp_path / "外部目录"
    external.mkdir()
    (external / "越界正文.md").write_text("不可读的哨兵", encoding="utf-8")
    (vault / INBOX / "往期收藏").symlink_to(external, target_is_directory=True)
    reader = LegacyLayoutReader(root)
    try:
        result = reader.list_items()
        assert result["coverage"]["partial"]
        assert result["total_items"] == 1
        assert {"code": "forbidden_path"} in result["integrity_gaps"]
        assert not reader.search("不可读的哨兵")["items"]
    finally:
        reader.close()
