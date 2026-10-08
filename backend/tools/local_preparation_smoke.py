"""Read one saved source; prepare only in a disposable library. No network/models."""
import argparse
import json
import tempfile
import time
from pathlib import Path

from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--reuse-only', action='store_true', help='Verify reuse in a disposable library without running decoders/OCR/models')
    args = parser.parse_args()
    source = LibraryStore(args.library)
    try:
        item = source.get(args.ref)
        media = PreparedInputs(source).source_media(args.ref, item.get('source_asset_hash'))
        assert media, 'No saved source'
        before = item.get('prepared_input')
        with tempfile.TemporaryDirectory(prefix='context-local-preparation-') as folder:
            isolated = LibraryStore.initialize(Path(folder).resolve() / 'library')
            try:
                fixture = isolated.upsert({'native_id':'718','title':'Local saved-source preparation test'},
                                          kind='saved',scope_id='s_test')['item']
                registry = PreparedInputs(isolated, runtime_dir=args.runtime)
                if args.reuse_only:
                    from collection_context.workflows.media_preparation import MediaPreparation
                    import collection_context.processing.inputs as input_module
                    def forbid_decoder(*args, **kwargs):
                        raise AssertionError('Reuse-only verification unexpectedly started a decoder')
                    input_module.LocalMedia = input_module._RuntimeLocalMedia = forbid_decoder
                    original_registry = PreparedInputs(source)
                    original_payload = original_registry.load(item['prepared_input'])
                    audio = original_registry.audio(original_payload)
                    frames = original_registry.frames(original_payload)
                    registry.save_source_media(fixture['id'],media,source_asset_hash=fixture.get('source_asset_hash'),content_hash=fixture['content_hash'])
                    full = registry.save(fixture['id'],content_hash=fixture['content_hash'],originals=media,audio=audio,frames=frames,
                        coverage=original_payload['coverage'],processor_version=original_payload['processor_version'],strategy_hash=original_payload['strategy_hash'],kind='video')
                    flow=MediaPreparation(isolated,args.runtime)
                    first=flow.submit(material_ref=fixture['id'],mode='full',idempotency_key='cache-history')
                    assert flow.run(first['id'])['state']=='succeeded'
                    # Reproduce the old audio-only manifest while keeping the full history.
                    registry.save(fixture['id'],content_hash=fixture['content_hash'],originals=media,audio=audio,frames=[],
                        coverage={'has_audio':True,'audio_only':True,'complete':False,'preparation_errors':{'vision':{'code':'vision_deferred'}}},
                        processor_version=original_payload['processor_version'],strategy_hash='audio-only-smoke',kind='video')
                    artifact=isolated.save_artifact(fixture['id'],'audio','原转写应保留',processor_version='test',expected_content_hash=fixture['content_hash'])
                    started=time.monotonic()
                    next_job=flow.submit(material_ref=fixture['id'],mode='full',idempotency_key='recover-full')
                    done=flow.run(next_job['id'])
                    assert done['state']=='succeeded'
                    current=isolated.get(fixture['id']);payload=registry.load(current['prepared_input'])
                    assert current['artifacts']['audio']['version']==artifact['version'] and current['artifacts']['audio']['state']=='ready'
                    assert len(payload['frames'])==len(frames)
                    assert all(done['stages'][name]['result']['progress']['reused'] for name in ('prepare_audio','prepare_vision'))
                    print(json.dumps({'reuse_only':True,'seconds':round(time.monotonic()-started,2),'audio_segments':len(audio),
                        'frames_recovered':len(frames),'audio_version_preserved':True,'model_calls':len(done['calls'])}),flush=True)
                    continue_run=False
                else:
                    continue_run=True
                for audio_only in ((True, False) if continue_run else ()):
                    started = time.monotonic()
                    identity = registry.prepare_video(fixture['id'],media[0][0],mime_type=media[0][1],audio_only=audio_only)
                    result = registry.load(identity)
                    coverage = result['coverage']
                    print(json.dumps({'audio_only':audio_only,'seconds':round(time.monotonic()-started,2),
                        'audio_segments':len(result['audio']),'frames':len(result['frames']),
                        'candidate_events':coverage.get('candidate_events'),
                        'candidate_compactions':coverage.get('candidate_compactions'),
                        'ocr_candidates':coverage.get('ocr_candidates'),
                        'ocr_duplicates':coverage.get('ocr_duplicate_frames'),
                        'budget_reductions':coverage.get('budget_reductions'),
                        'preparation_errors':coverage.get('preparation_errors'),
                        'scan_state':coverage.get('scan_state')},ensure_ascii=False),flush=True)
            finally:
                isolated.close()
        assert source.get(args.ref).get('prepared_input') == before
        print('Original library input unchanged; zero model calls.',flush=True)
    finally:
        source.close()


if __name__ == '__main__':
    main()
