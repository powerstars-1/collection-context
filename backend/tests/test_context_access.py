from __future__ import annotations

import pytest

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.interfaces.access import ACCESS_FILE, AccessRegistry
from collection_context.interfaces.security import AccessPolicy
from collection_context.interfaces.server import main as server_main
from collection_context.library.store import LibraryStore


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.initialize(tmp_path / "workspace")
    yield value
    value.close()


def test_registry_only_persists_hash_and_creation_does_not_change_library(store):
    registry = AccessRegistry(store)
    before = store.snapshot()
    result = registry.create("只读 AI")
    saved = store.files.read(ACCESS_FILE)
    assert result["token"].encode() not in saved
    assert result["permissions"] == ["collections:read"]
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    assert policy.authenticate("Bearer " + result["token"]).principal == result["principal"]
    assert store.snapshot() == before


def test_revoke_refresh_immediately_invalidates_existing_session(store):
    registry = AccessRegistry(store)
    result = registry.create("页面", ui=True)
    policy = AccessPolicy("http://127.0.0.1:8787", registry.credentials())
    credential = policy.authenticate("Bearer " + result["token"])
    key, _ = policy.create_session(credential)
    registry.revoke(result["principal"])
    registry.refresh(policy)
    with pytest.raises(ContextError):
        policy.session(key)
    with pytest.raises(ContextError):
        policy.authenticate("Bearer " + result["token"])


def test_duplicate_names_and_corrupt_registry_do_not_grant_access(store):
    registry = AccessRegistry(store)
    registry.create("已建立")
    with pytest.raises(ContextError) as caught:
        registry.create("已建立")
    assert caught.value.code == "credential_conflict"
    store.files.write(ACCESS_FILE, b"{}", replace=True)
    with pytest.raises(ContextError) as caught:
        registry.credentials()
    assert caught.value.code == "invalid_access_config"


def test_server_refuses_public_plaintext_bind_and_empty_setup(store, capsys):
    args = ["--workspace", str(store.files.root)]
    assert server_main([*args, "--bind", "0.0.0.0"]) == 1
    assert "permission_denied" in capsys.readouterr().err
    assert server_main([*args, "--remote"]) == 1
    assert "https_required" in capsys.readouterr().err
    assert server_main(args) == 1
    assert "access_setup_required" in capsys.readouterr().err


def test_version_bound_material_list_does_not_need_index_or_models(store):
    for number in range(3):
        store.upsert(
            {"native_id": str(number + 1), "title": "原创列表样例"}, kind="liked", scope_id="s_likes"
        )
    service = ContextService(store)
    first = service.list_items(limit=1)
    second = service.list_items(offset=first["next_offset"], version=first["version"], limit=1)
    assert first["items"][0]["material_ref"] != second["items"][0]["material_ref"]
    assert first["total_items"] == 3
    assert service.overview()["audio_missing"] == 3
    with pytest.raises(ContextError) as changed_filter:
        service.list_items(offset=1, version=first["version"], filters={"source_kinds": ["liked"]})
    assert changed_filter.value.code == "version_changed"
    store.exclude(first["items"][0]["material_ref"])
    with pytest.raises(ContextError) as caught:
        service.list_items(offset=1, version=first["version"])
    assert caught.value.code == "version_changed"


def test_ui_assets_are_offline_and_never_render_source_as_html():
    from importlib.resources import files

    root = files("collection_context.interfaces").joinpath("assets")
    source = root.joinpath("app.js").read_text()
    assert "innerHTML" not in source and "localStorage" not in source and "eval(" not in source
    assert ".textContent" in source
    html = root.joinpath("index.html").read_text()
    assert 'src="https://' not in html and 'href="https://' not in html
