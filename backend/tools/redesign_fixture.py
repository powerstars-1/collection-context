"""Synthetic local UI fixture. No platform, private library or model inference access."""
from __future__ import annotations

import argparse
import base64
from pathlib import Path

import uvicorn

from collection_context.application.source_management import SourceManagement
from collection_context.infrastructure.secrets import FileSecrets
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.sources.account import self_account
from collection_context.sources.collections import FolderBatch, folder_page
from collection_context.workflows.connection import ConnectionCatalog

TOKEN = 'synthetic-redesign-fixture-access-not-production-20261003'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8791)
    args = parser.parse_args()
    store = LibraryStore.initialize(args.run / 'synthetic-library')
    secrets = FileSecrets.initialize(args.run / 'synthetic-private')
    batch = FolderBatch()
    batch.accept(folder_page({'status_code': 0, 'collects_list': [
        {'collects_id_str': '11', 'collects_name': 'AI教程'},
        {'collects_id_str': '12', 'collects_name': '创作灵感'}], 'has_more': 0, 'cursor': None}), '0')
    proof = ConnectionCatalog(store).record(self_account({'status_code': 0,
        'user': {'uid': '123', 'sec_uid': 'MS4w-fixture', 'nickname': '隔离测试账号'}}),
        expected_version=None, folders=batch)
    sources = SourceManagement(store)
    scopes = {}
    for kind in ('liked', 'saved', 'collection'):
        scopes[kind] = sources.create_self(kind=kind, collection_id='11' if kind == 'collection' else None,
            connection_version=proof['version'], limit=5, download=True, source_confirmed=True)['scope_id']
    scopes['creator'] = sources.create(creator_url='https://www.douyin.com/user/fixture',
        limit=5, download=False)['scope_id']
    for index, (kind, title) in enumerate([
        ('liked', '喜欢样例：轻快动画的节奏'), ('saved', '收藏样例：UI提示词模板'),
        ('collection', '收藏夹样例：画面文字提取'), ('creator', '博主样例：创作工作流')], start=881):
        item = store.upsert({'native_id': str(index), 'title': title, 'media_type': 'image',
            'text': '这是原创隔离测试内容，不是用户真实收藏。', 'author': '原创测试'},
            kind=kind, scope_id=scopes[kind])['item']
        store.save_artifact(item['id'], 'summary', '原创测试总结：保留来源，记录可复用的方法。',
            processor_version='fixture-v1', expected_content_hash=item['content_hash'])
        if kind == 'saved':
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMQ0bD5DwACRAF4aig0hQAAAABJRU5ErkJggg==')
            PreparedInputs(store).prepare_images(item['id'], [(png, 'image/png')])
    origin = f'http://127.0.0.1:{args.port}'
    credential = Credential.from_token('p_redesign_fixture', TOKEN,
        permissions=frozenset({'collections:read', 'ui:view', 'ui:manage'}))
    app = create_app(store.files.root, AccessPolicy(origin, [credential], local_ui=True), model_secrets=secrets)
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
    finally:
        secrets.close()
        store.close()

if __name__ == '__main__':
    main()
