"""Branch input identity, preparation reuse and truthful progress; no cloud calls."""
from types import SimpleNamespace

import pytest
from test_context_adaptive_preparation import local
from test_context_processing_stages import audio, frame
from test_context_scheduling import env
from test_context_model_registry import requests

from collection_context.application.management import ManagementService
from collection_context.application.service import ContextService
from collection_context.processing.inputs import PreparedInputs
from collection_context.workflows.media_preparation import MediaPreparation


def test_adding_frames_keeps_identical_transcript_current(local):
    store,item,registry,_=local
    old=registry.prepare_video(item['id'],b'synthetic-video',audio_only=True)
    artifact=store.save_artifact(item['id'],'audio','已经完成的转写',processor_version='fixture',expected_content_hash=item['content_hash'])
    new=registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'synthetic-video','video/mp4')],
        audio=[audio()],frames=[frame()],coverage={'has_audio':True,'complete':False},processor_version='fixture',strategy_hash='new-frames',kind='video')
    assert new!=old
    current=store.get(item['id'])['artifacts']['audio']
    assert current['version']==artifact['version'] and current['state']=='ready' and current['prepared_input']==new
    assert ContextService(store).read(item['id'],artifact='audio')['text']=='已经完成的转写'


def test_preparing_missing_frames_reuses_audio_without_decoding_it(local,monkeypatch):
    store,item,registry,Media=local
    registry.prepare_video(item['id'],b'synthetic-video',audio_only=True)
    store.save_artifact(item['id'],'audio','原转写保留',processor_version='fixture',expected_content_hash=item['content_hash'])
    monkeypatch.setattr(Media,'audio_segments',lambda _:pytest.fail('decoded existing audio again'))
    monkeypatch.setattr(Media,'frames',lambda _,candidates,consumer:consumer(frame()))
    flow=MediaPreparation(store);job=flow.submit(material_ref=item['id'],mode='full',idempotency_key='fill-vision')
    done=flow.run(job['id'])
    assert done['state']=='succeeded'
    assert done['stages']['prepare_audio']['result']['progress']['reused'] is True
    assert done['stages']['prepare_vision']['state']=='ready'
    assert store.get(item['id'])['artifacts']['audio']['state']=='ready'


def test_switch_to_audio_only_keeps_prepared_frames_and_skips_decoder(local,monkeypatch):
    store,item,registry,Media=local
    registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'synthetic-video','video/mp4')],audio=[audio()],frames=[frame()],
        coverage={'has_audio':True,'complete':False},processor_version='local_media_v5',strategy_hash='original',kind='video')
    store.save_artifact(item['id'],'screen','之前提取的画面',processor_version='fixture',expected_content_hash=item['content_hash'])
    monkeypatch.setattr('collection_context.processing.inputs.LocalMedia',lambda *a,**kw:pytest.fail('restarted decoder for scope switch'))
    flow=MediaPreparation(store);job=flow.submit(material_ref=item['id'],mode='audio',idempotency_key='scope-audio')
    assert flow.run(job['id'])['state']=='succeeded'
    current=store.get(item['id']);payload=registry.load(current['prepared_input'])
    assert payload['coverage']['audio_only'] is True and len(payload['frames'])==1
    assert current['artifacts']['screen']['state']=='ready'


def test_repeat_full_preparation_reuses_both_inputs(local,monkeypatch):
    store,item,registry,Media=local
    registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'synthetic-video','video/mp4')],audio=[audio()],frames=[frame()],
        coverage={'has_audio':True,'complete':False},processor_version='local_media_v5',strategy_hash='original',kind='video')
    monkeypatch.setattr('collection_context.processing.inputs.LocalMedia',lambda *a,**kw:pytest.fail('prepared completed input again'))
    flow=MediaPreparation(store);job=flow.submit(material_ref=item['id'],mode='full',idempotency_key='reuse-both')
    done=flow.run(job['id'])
    assert done['state']=='succeeded' and not done['calls']
    assert all(done['stages'][name]['result']['progress']['reused'] for name in ('prepare_audio','prepare_vision'))


def test_progress_is_recorded_while_frame_scan_is_executing(local,monkeypatch):
    store,item,registry,Media=local;flow=MediaPreparation(store)
    job=flow.submit(material_ref=item['id'],mode='full',idempotency_key='progress')
    def scan(self):
        state=flow.jobs.get(job['id'])['stages']
        assert state['prepare_audio']['state']=='ready'
        assert state['prepare_vision']['state']=='running'
        return [frame().candidate], {'complete':False}
    monkeypatch.setattr(Media,'scan_frames',scan)
    monkeypatch.setattr(Media,'frames',lambda _,candidates,consumer:consumer(frame()))
    assert flow.run(job['id'])['state']=='succeeded'


def test_changed_original_never_rebinds_old_transcript(local):
    store,item,registry,_=local
    registry.prepare_video(item['id'],b'synthetic-video',audio_only=True)
    store.save_artifact(item['id'],'audio','旧视频的转写',processor_version='fixture',expected_content_hash=item['content_hash'])
    registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'different-video','video/mp4')],audio=[audio()],frames=[],
        coverage={'has_audio':True,'complete':False,'preparation_errors':{'vision':{'code':'vision_deferred'}}},processor_version='fixture',strategy_hash='other-video',kind='video')
    assert store.get(item['id'])['artifacts']['audio']['state']=='stale'


def test_partial_preparation_reports_failed_branch_and_keeps_success(local):
    store,item,_,_=local
    flow=MediaPreparation(store);job=flow.submit(material_ref=item['id'],mode='full',idempotency_key='partial-prepare')
    result=flow.run(job['id'])
    assert result['state']=='partial'
    assert result['stages']['prepare_audio']['state']=='ready'
    assert result['stages']['prepare_vision']['state']=='failed'
    assert result['stages']['prepare_vision']['error']['code']=='media_decode_incomplete'
    assert store.get(item['id'])['prepared_input']


def test_reuse_estimate_and_actual_execution_do_not_repeat_audio(env,monkeypatch):
    store=env[0];item=store.upsert({'native_id':'936','title':'先音频后画面'},kind='saved',scope_id='s_test')['item']
    registry=PreparedInputs(store)
    old=registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'same','video/mp4')],audio=[audio()],frames=[],
        coverage={'has_audio':True,'audio_only':True,'complete':False,'preparation_errors':{'vision':{'code':'vision_deferred'}}},processor_version='fixture',strategy_hash='audio',kind='video')
    sent=requests(monkeypatch)
    old_job=env[3].submit(old,idempotency_key='before',max_calls=2);env[3].run(old_job['id'])
    new=registry.save(item['id'],content_hash=item['content_hash'],originals=[(b'same','video/mp4')],audio=[audio()],frames=[frame()],
        coverage={'has_audio':True,'complete':False},processor_version='fixture',strategy_hash='full',kind='video')
    preview=ManagementService(store)._prepared({'material_ref':item['id'],'input_id':new})
    assert preview['reusable_audio_segments']==1 and preview['planned_new_calls']==2
    before=len(sent);job=env[3].submit(new,idempotency_key='after',max_calls=2);done=env[3].run(job['id'])
    assert len(sent)-before==2 and done['stages']['audio_a_000000']['reused'] is True


def test_audio_scope_with_retained_frames_does_not_schedule_vision(env):
    store=env[0];item=store.upsert({'native_id':'937','title':'只跑音频保留画面'},kind='saved',scope_id='s_test')['item']
    identity=PreparedInputs(store).save(item['id'],content_hash=item['content_hash'],originals=[(b'source','video/mp4')],audio=[audio()],frames=[frame()],
        coverage={'has_audio':True,'audio_only':True,'complete':False,'preparation_errors':{'vision':{'code':'vision_deferred'}}},processor_version='fixture',strategy_hash='only-audio',kind='video')
    plan=env[3].prepare_plan(identity)
    assert not any(stage['name'].startswith('screen_') for stage in plan['plan'])
    assert set(plan['extraction']['model_profiles'])=={'audio','summary'}
