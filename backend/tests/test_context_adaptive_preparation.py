"""Local-only selection and preparation regressions; no cloud or source requests."""
from types import SimpleNamespace

import pytest
from test_context_ocr_selection import page, png, result, select
from test_context_processing_stages import audio, frame

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.media import FrameScanner, MediaPolicy, THUMB_BYTES
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.media_preparation import MediaPreparation
from collection_context.application.management import ManagementService


def test_motion_budget_keeps_both_ends_instead_of_failing():
    scanner = FrameScanner(MediaPolicy(sample_fps=10, max_selected_frames=8, adaptive_selection=True))
    for n in range(400):
        scanner.feed(bytes([30 if (n // 10) % 2 else 230]) * THUMB_BYTES)
    selected = scanner.finish(40)
    assert len(selected) <= 8 and scanner.compactions > 0
    assert selected[0].sample_index == 0 and selected[-1].sample_index == 399


def test_adaptive_scan_ignores_single_pixel_compression_noise():
    scanner = FrameScanner(MediaPolicy(sample_fps=10, adaptive_selection=True))
    baseline = bytes([220]) * THUMB_BYTES
    changed = bytearray(baseline); changed[123] = 212
    for n in range(30): scanner.feed(baseline if n % 2 else bytes(changed))
    assert len(scanner.finish(3)) == 2


@pytest.mark.parametrize('limit', [1, 2, 3, 8])
def test_ocr_budget_remains_bounded_and_reports_reduction(limit):
    value = select(policy=MediaPolicy(max_selected_frames=limit, adaptive_selection=True))
    for n in range(25): value.feed(page(n, data=png(color=(n * 7, 50, 60, 255))))
    coverage = value.coverage()
    assert len(value.frames) <= limit and coverage['budget_reductions'] > 0
    retained = {f.candidate.evidence_id for f in value.frames}
    assert all(r['duplicate_of'] in retained for r in value.records if r['duplicate_of'])
    assert coverage['complete'] is False and coverage['budget_skipped_frames'] > 0


def test_near_duplicate_requires_same_text_and_keeps_number_changes():
    text = '这是完整的一段教学说明文字，参数 width = 390，不能遗漏。'
    value = select(outputs=[result(text), result(text), result(text.replace('390','391'))],
                   policy=MediaPolicy(adaptive_selection=True))
    for n in range(3): value.feed(page(n, data=png(color=(40+n,50,60,255))))
    assert [f.candidate.sample_index for f in value.frames] == [0, 2]
    assert value.records[1]['reason'] == 'ocr_near_duplicate'


@pytest.fixture
def local(tmp_path, monkeypatch):
    store = LibraryStore.initialize(tmp_path / 'isolated')
    item = store.upsert({'native_id':'713','title':'本地准备样例'},kind='saved',scope_id='s_test')['item']
    registry = PreparedInputs(store)
    registry.save_source_media(item['id'], [(b'synthetic-video','video/mp4')],
        source_asset_hash=item.get('source_asset_hash'), content_hash=item['content_hash'])
    class Media:
        strategy_hash='synthetic'
        info=SimpleNamespace(has_audio=True, duration_seconds=10)
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def audio_segments(self): return [audio()]
        def scan_frames(self): return [frame().candidate], {'complete':False}
        def frames(self,candidates,consumer):
            consumer(frame())
            raise ContextError('media_decode_incomplete','injected partial decode')
    monkeypatch.setattr('collection_context.processing.inputs.LocalMedia',Media)
    yield store,item,registry,Media
    store.close()


def test_decoded_frames_survive_later_visual_failure(local):
    store,item,registry,_=local
    identity=registry.prepare_video(item['id'],b'synthetic-video')
    payload=registry.load(identity)
    assert len(payload['audio']) == len(payload['frames']) == 1
    assert payload['coverage']['preparation_errors']['vision']['code']=='media_decode_incomplete'


def test_audio_only_never_scans_and_runs_from_saved_source(local,monkeypatch):
    store,item,registry,Media=local
    monkeypatch.setattr(Media,'scan_frames',lambda _:pytest.fail('audio-only scanned video'))
    workflow=MediaPreparation(store)
    args=dict(material_ref=item['id'],mode='audio',idempotency_key='local-one')
    job=workflow.submit(**args)
    assert workflow.submit(**args)['id']==job['id']
    with pytest.raises(ContextError): workflow.submit(**{**args,'idempotency_key':'local-two'})
    done=workflow.run(job['id'])
    assert done['state']=='succeeded' and done['calls']==[]
    assert ManagementService._public_job(done)['link_result']['download']=='ready'
    payload=registry.load(store.get(item['id'])['prepared_input'])
    assert payload['audio'] and not payload['frames'] and payload['coverage']['audio_only']


def test_cancelled_local_preparation_does_not_bind_input(local,monkeypatch):
    store,item,registry,Media=local
    workflow=MediaPreparation(store)
    job=workflow.submit(material_ref=item['id'],mode='audio',idempotency_key='cancel')
    def segments(self):
        workflow.jobs.cancel(job['id'])
        return [audio()]
    monkeypatch.setattr(Media,'audio_segments',segments)
    done=workflow.run(job['id'])
    assert done['state']=='cancelled' and not store.get(item['id']).get('prepared_input')


def test_background_worker_dispatches_local_preparation_without_secrets(local):
    from collection_context.workflows.worker import BackgroundWorker
    from collection_context.workflows.extraction import ExtractionWorkflow
    store,item,_,_=local
    job=MediaPreparation(store).submit(material_ref=item['id'],mode='audio',idempotency_key='worker-local')
    workflow=ExtractionWorkflow(store,lambda _:pytest.fail('local preparation requested a secret'))
    done=BackgroundWorker(workflow).serve(allow_model_calls=True,once=True)
    assert done['handled']==1 and store.snapshot()['jobs'][job['id']]['state']=='succeeded'


def test_hundreds_of_frame_terms_are_balanced(monkeypatch):
    from test_context_media import fake_media, PNG
    from collection_context.infrastructure.media import FrameCandidate
    with fake_media(monkeypatch, policy=MediaPolicy(max_selected_frames=480)) as media:
        # The fixture is short, so this exercises the filter construction only.
        monkeypatch.setattr(media, '_validate_candidate', lambda _: None)
        from collection_context.infrastructure.media import PngFrames
        monkeypatch.setattr(PngFrames, 'finish', lambda _: None)
        seen=[]
        monkeypatch.setattr(media, '_run', lambda executable, arguments, **kwargs: seen.append(arguments))
        media.frames([FrameCandidate(f'f_{n:06}',n,n/2,(),0) for n in range(480)],lambda _:None)
        expression=seen[0][seen[0].index('-vf')+1].split("select='")[1].split("'")[0]
        depth=maximum=0
        for char in expression:
            depth += (char=='(') - (char==')')
            maximum=max(maximum,depth)
        assert depth==0 and maximum<=11
        assert expression.count('eq(n,')==480


def test_local_consumer_time_is_not_counted_as_decoder_timeout(monkeypatch):
    import time
    from test_context_media import fake_media
    with fake_media(monkeypatch, policy=MediaPolicy(command_timeout_seconds=0.3)) as media:
        seen=[]
        def consume(data):
            time.sleep(0.4)
            seen.append(data)
        media._run(media.ffmpeg,['-c',"print('local-fixture')"],max_bytes=100,consumer=consume)
        assert b''.join(seen)==b'local-fixture\n'
