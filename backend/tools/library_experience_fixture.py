"""Isolated reading/empty-state UI acceptance. Original test notes; zero model calls."""
import argparse
from pathlib import Path
import uvicorn
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy, Credential
from collection_context.library.store import LibraryStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8832)
    parser.add_argument('--empty', action='store_true')
    args = parser.parse_args()
    store = LibraryStore.initialize(args.run / 'library')
    if not args.empty:
        for number, title, artifacts in [
            ('990101', '原创测试：动画分镜教程', {
                'summary': '## 一句话要点\n先定风格和分镜，再沿用节拍表。\n\n## 操作步骤\n1. **先定风格**，保留样图。\n2. 写分镜，再匹配声音。\n\n|部分|要求|\n|---|---|\n|画面|保持人物一致|\n|声音|按节拍安排|',
                'audio': '## 音频转写\n[00:00–00:05] 先确定风格。\n[00:05–00:10] 再写分镜和音乐。',
                'screen': '## 画面文字\n节拍表：镜头 / 时长 / 画面 / 声音。'}),
            ('990102', '原创测试：有转写，尚未总结', {'audio': '[00:00–00:05] 这是部分完成的测试转写。'}),
            ('990103', '原创测试：已保存，尚未生成', {}),
        ]:
            item = store.upsert({'native_id': number, 'title': title, 'author': '原创验收样例',
                'media_type': 'video', 'body': '原创隔离测试文案，不是用户收藏。'}, kind='link', scope_id='ui_reading_fixture')['item']
            for kind, text in artifacts.items():
                store.save_artifact(item['id'], kind, text, processor_version='ui-fixture-v1', expected_content_hash=item['content_hash'])
    # A separate hostname prevents test sessions replacing the running 127.0.0.1 app cookie.
    origin = f'http://localhost:{args.port}'
    credential = Credential.from_token('p_ui_fixture', 'synthetic-ui-reading-fixture-not-production',
        permissions=frozenset({'collections:read', 'ui:view', 'ui:manage'}))
    try:
        uvicorn.run(create_app(store.files.root, AccessPolicy(origin, [credential], local_ui=True)),
            host='127.0.0.1', port=args.port, log_level='warning')
    finally:
        store.close()


if __name__ == '__main__':
    main()
