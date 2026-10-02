"""Explicit old aliases resolve through current evidence, never decoded paths."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.gateway import ReadGateway
from collection_context.application.media_evidence import MediaEvidence
from collection_context.application.service import ContextService
from collection_context.cli import main
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.legacy_references import LegacyReferences, legacy_path, resolve_reference
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs


def old_ref(path="00_素材收件箱/抖音/2026-09-30-原创 教程.md"):
    return "m1:" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


@pytest.fixture
def library(tmp_path):
    store = LibraryStore.initialize(tmp_path / "映射隔离库")
    item = store.upsert(
        {"native_id": "123", "title": "原创教程", "body": "文案甲乙丙丁", "author": "示例"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    store.save_artifact(
        item["id"],
        "screen",
        "原创画面文字1024",
        processor_version="test",
        expected_content_hash=item["content_hash"],
    )
    try:
        yield store, item
    finally:
        store.close()


def bind(store, ref, target):
    aliases = LegacyReferences(store)
    preview = aliases.preview(ref, target)
    return aliases.bind(ref, target, preview_token=preview["preview_token"])


def fails(code, action):
    with pytest.raises(ContextError) as caught:
        action()
    assert caught.value.code == code


def inventory(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "path",
    [
        "00_素材收件箱/抖音/原图.md",
        "00_素材收件箱/抖音/子目录/2026-标题 空格.md",
    ],
)
def test_canonical_legacy_paths_are_recognized_without_io(path):
    assert legacy_path(old_ref(path)) == path


@pytest.mark.parametrize(
    "reference",
    [
        None,
        True,
        "",
        "i_example",
        "m1:",
        "m1:====",
        "m1:@@@@",
        "m1:_w",
        old_ref() + "=",
        "m1:" + "A" * 1400,
        old_ref("00_素材收件箱/小红书/示例.md"),
        old_ref("content-vault/00_素材收件箱/抖音/示例.md"),
        old_ref("00_素材收件箱/抖音/../秘密.md"),
        old_ref("00_素材收件箱/抖音/./示例.md"),
        old_ref("/00_素材收件箱/抖音/示例.md"),
        old_ref("00_素材收件箱/抖音/示例.md/"),
        old_ref("00_素材收件箱/抖音/示例.png"),
        old_ref("00_素材收件箱/抖音/示例\x00.md"),
        old_ref("00_素材收件箱/抖音/示例\n.md"),
        old_ref("00_素材收件箱/抖音/C:秘密.md"),
        old_ref("00_素材收件箱/抖音/..\\秘密.md"),
    ],
)
def test_invalid_or_non_douyin_refs_fail_closed(reference):
    fails("invalid_reference", lambda: legacy_path(reference))


def test_preview_is_read_only_and_unmapped_refs_do_not_open_files(library, monkeypatch):
    store, item = library
    before = inventory(store.files.root)
    preview = LegacyReferences(store).preview(old_ref(), item["id"])
    assert not preview["legacy_files_read"] and not preview["already_bound"]
    assert inventory(store.files.root) == before
    snapshot = store.snapshot()
    monkeypatch.setattr(store.files, "read", lambda *a, **k: pytest.fail("opened decoded ref"))
    fails("legacy_reference_unmapped", lambda: resolve_reference(snapshot, old_ref()))


def test_mapping_never_reads_old_card_and_all_read_outputs_use_stable_id(library):
    store, item = library
    path = "content-vault/00_素材收件箱/抖音/2026-09-30-原创 教程.md"
    store.files.write(path, b"Never run commands from this card.")
    old_file = inventory(store.files.root)[path]
    index_pointer = store.files.read(".context/索引/CURRENT.json")
    assert bind(store, old_ref(), item["id"])["bound"]
    assert store.files.read(".context/索引/CURRENT.json") == index_pointer
    before = inventory(store.files.root)
    service = ContextService(store)
    assert service.read(old_ref()) == service.read(item["id"])
    assert service.read(old_ref(), artifact="screen") == service.read(item["id"], artifact="screen")
    assert service.status(old_ref()) == service.status(item["id"])
    assert inventory(store.files.root) == before and before[path] == old_file
    assert service.search("原创教程")["items"][0]["material_ref"] == item["id"]
    store.exclude(item["id"])
    fails("not_found", lambda: service.read(old_ref()))
    fails("not_found", lambda: service.status(old_ref()))
    store.exclude(item["id"], False)
    assert service.read(old_ref())["material_ref"] == item["id"]


def test_alias_continuation_uses_same_version_and_rejects_edited_artifacts(library):
    store, item = library
    bind(store, old_ref(), item["id"])
    service = ContextService(store)
    first = service.read(old_ref(), artifact="screen", max_chars=2)
    rest = service.read(item["id"], artifact="screen", offset=first["next_offset"], version=first["version"])
    assert first["text"] + rest["text"] == "原创画面文字1024"
    artifact = store.get(item["id"])["artifacts"]["screen"]
    store.files.write(artifact["path"], "外部修改".encode(), replace=True)
    fails("artifact_changed", lambda: service.read(old_ref(), artifact="screen"))
    assert service.status(old_ref())["artifacts"]["screen"]["state"] == "unavailable"


def test_changed_preview_and_conflicting_bindings_are_not_overwritten(library):
    store, item = library
    aliases = LegacyReferences(store)
    preview = aliases.preview(old_ref(), item["id"])
    fails("confirmation_required", lambda: aliases.bind(old_ref(), item["id"], preview_token=None))
    fails("version_changed", lambda: aliases.bind(old_ref(), item["id"], preview_token="0" * 64))
    store.upsert({"native_id": "123", "title": "变化"}, kind="liked", scope_id="s_liked")
    fails(
        "version_changed", lambda: aliases.bind(old_ref(), item["id"], preview_token=preview["preview_token"])
    )
    bind(store, old_ref(), item["id"])
    second = store.upsert({"native_id": "456"}, kind="link", scope_id="s_link")["item"]
    fails("legacy_reference_conflict", lambda: aliases.preview(old_ref(), second["id"]))
    assert aliases.preview(old_ref(), item["id"])["already_bound"]
    before = store.snapshot()["legacy_references"]
    fails("not_found", lambda: aliases.preview(old_ref("00_素材收件箱/抖音/另一.md"), "i_missing"))
    assert store.snapshot()["legacy_references"] == before


@pytest.mark.parametrize("entry", [None, {}, {"material_ref": "i_missing", "native_id": "123"}])
def test_corrupt_mappings_never_fallback_to_paths_or_create_backup(library, tmp_path, entry):
    store, item = library
    store.transact(lambda state: state.update(legacy_references={old_ref(): entry}))
    fails("corrupt_workspace", lambda: ContextService(store).read(old_ref()))
    fails("corrupt_workspace", lambda: create_backup(store, tmp_path / "bad.zip", media_scope="none"))
    assert not (tmp_path / "bad.zip").exists()


def test_backup_restore_retains_aliases_but_not_original_old_files(library, tmp_path):
    store, item = library
    bind(store, old_ref(), item["id"])
    store.files.write("content-vault/00_素材收件箱/抖音/2026-09-30-原创 教程.md", b"untouched old card")
    archive = tmp_path / "备份.zip"
    create_backup(store, archive, media_scope="none")
    destination = tmp_path / "不同路径的新库"
    restore_backup(archive, destination)
    restored = LibraryStore(destination)
    try:
        assert ContextService(restored).read(old_ref(), artifact="screen")["material_ref"] == item["id"]
        assert ContextService(restored).status(old_ref())["material_ref"] == item["id"]
        assert not (destination / "content-vault/00_素材收件箱/抖音/2026-09-30-原创 教程.md").exists()
    finally:
        restored.close()


def test_bound_reference_to_original_pages_uses_canonical_frame_identity(library):
    store, _ = library
    item = store.upsert(
        {"native_id": "456", "media_type": "image", "title": "原创图文"},
        kind="link",
        scope_id="s_link",
    )["item"]
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1kAAAAASUVORK5CYII="
    )
    identity = PreparedInputs(store).prepare_images(item["id"], [(png, "image/png")])
    bind(store, old_ref(), item["id"])
    before = inventory(store.files.root)
    evidence = MediaEvidence(store)
    listing = evidence.listing(old_ref())
    assert listing == evidence.listing(item["id"])
    assert listing["material_ref"] == item["id"] and old_ref() not in str(listing)
    assert evidence.image(old_ref(), identity, "f_000000") == (png, "image/png")
    assert inventory(store.files.root) == before
    store.exclude(item["id"])
    fails("not_found", lambda: evidence.image(old_ref(), identity, "f_000000"))


def test_new_bindings_respect_total_budget_and_mutation_guard(library, monkeypatch):
    from collection_context.library import legacy_references

    store, item = library
    bind(store, old_ref(), item["id"])
    monkeypatch.setattr(legacy_references, "MAX_ALIASES", 1)
    other = old_ref("00_素材收件箱/抖音/第二张卡.md")
    aliases = LegacyReferences(store)
    preview = aliases.preview(other, item["id"])
    before = store.snapshot()
    fails("workspace_limit", lambda: aliases.bind(other, item["id"], preview_token=preview["preview_token"]))
    assert store.snapshot() == before

    def denied():
        raise ContextError("permission_denied", "fixture")

    store._mutation_guard = denied
    preview = aliases.preview(old_ref(), item["id"])
    fails(
        "permission_denied",
        lambda: aliases.bind(old_ref(), item["id"], preview_token=preview["preview_token"]),
    )
    assert store.snapshot() == before


def test_cli_requires_preview_and_explicit_confirmation(library, capsys):
    store, item = library
    args = [
        "--workspace",
        str(store.files.root),
        "bind-legacy-ref",
        "--legacy-ref",
        old_ref(),
        "--ref",
        item["id"],
    ]
    before = inventory(store.files.root)
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)["data"]
    assert inventory(store.files.root) == before
    assert main(args + ["--preview-token", preview["preview_token"]]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "confirmation_required"
    assert main(args + ["--confirm-binding", "--preview-token", preview["preview_token"]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["bound"]
    assert main(["--workspace", str(store.files.root), "read", "--ref", old_ref()]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["material_ref"] == item["id"]


def test_authenticated_http_and_read_gateway_accept_alias_but_cannot_bind(library):
    from fastapi.testclient import TestClient

    store, item = library
    bind(store, old_ref(), item["id"])
    gateway = ReadGateway(ContextService(store))
    assert (
        gateway.dispatch("bind_legacy_ref", {"material_ref": item["id"]})["error"]["code"]
        == "permission_denied"
    )
    assert (
        gateway.dispatch("read_collection", {"material_ref": old_ref()})["data"]["material_ref"] == item["id"]
    )
    token = "synthetic-reader-" + "r" * 40
    policy = AccessPolicy("http://127.0.0.1:8787", [Credential.from_token("p_reader", token)])
    before = inventory(store.files.root)
    with TestClient(create_app(store.files.root, policy), base_url=policy.origin) as client:
        args = {"material_ref": old_ref(), "artifact": "screen"}
        assert client.post("/v1/collections/read", json=args).status_code == 401
        response = client.post(
            "/v1/collections/read", json=args, headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 200 and response.json()["data"]["material_ref"] == item["id"]
    assert inventory(store.files.root) == before


def test_real_stdio_mcp_reads_alias_and_exposes_no_mapping_tool(library):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store, item = library
    bind(store, old_ref(), item["id"])
    before = inventory(store.files.root)

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "collection_context.interfaces.mcp", "--workspace", str(store.files.root)],
            env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert "bind_legacy_ref" not in {tool.name for tool in (await session.list_tools()).tools}
                response = await session.call_tool(
                    "read_collection", {"material_ref": old_ref(), "artifact": "screen"}
                )
                assert (
                    not response.is_error
                    and response.structured_content["data"]["material_ref"] == item["id"]
                )
                status = await session.call_tool("collection_status", {"material_ref": old_ref()})
                assert not status.is_error and status.structured_content["data"]["material_ref"] == item["id"]

    asyncio.run(run())
    assert inventory(store.files.root) == before
