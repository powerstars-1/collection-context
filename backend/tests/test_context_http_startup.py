"""A fresh read HTTP service never eagerly loads the optional execution graph."""

from __future__ import annotations

import subprocess
import sys

import pytest
from test_context_mcp_startup import GUARD, child_env

from collection_context.library.index import FileIndex
from collection_context.library.store import LibraryStore


@pytest.mark.parametrize("module", ["http", "server"])
def test_http_entry_import_does_not_load_source_or_media(tmp_path, module):
    result = subprocess.run(
        [sys.executable, "-B", "-c", GUARD + f"\nimport collection_context.interfaces.{module}\n"],
        env=child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_fresh_http_reads_and_denied_optional_calls_never_load_execution(tmp_path):
    pytest.importorskip("fastapi")
    store = LibraryStore.initialize(tmp_path / "original-library")
    item = store.upsert(
        {"native_id": "918", "title": "原创 HTTP 启动", "body": "只读接口无额外权限"},
        kind="saved",
        scope_id="s_saved",
    )["item"]
    FileIndex(store).rebuild()
    before = store.snapshot()
    code = (
        GUARD
        + """
from pathlib import Path
from fastapi.testclient import TestClient
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
policy = AccessPolicy('http://127.0.0.1:8787', [Credential.from_token('p_reader', 'r' * 40)])
headers = {'Authorization': 'Bearer ' + 'r' * 40}
with TestClient(create_app(Path(sys.argv[1]), policy), base_url=policy.origin) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/').status_code == 200
    found = client.post('/v1/collections/search', json={'query': '原创'}, headers=headers).json()
    assert found['ok'] and found['data']['model_requests'] == 0
    ref = found['data']['items'][0]['material_ref']
    assert client.get('/v1/collections/' + ref + '/status', headers=headers).status_code == 200
    assert client.post('/v1/collections/read', json={'material_ref': ref, 'artifact': 'original'}, headers=headers).status_code == 200
    assert client.post('/v1/collections', json={}, headers=headers).status_code == 403
    assert client.get('/v1/jobs/j_' + 'a' * 32, headers=headers).status_code == 403
    assert client.post('/v1/management/models', json={}, headers=headers).status_code == 403
    assert client.post('/v1/management/library/overview', json={}, headers=headers).status_code == 403
    assert client.get('/v1/collections/' + ref + '/frames').status_code == 401
"""
    )
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(store.files.root)],
            env=child_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert not result.stdout
        assert store.snapshot() == before
        assert item["id"] in before["items"]
    finally:
        store.close()
