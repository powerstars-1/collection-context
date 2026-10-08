"""Extraction acceptance regressions, isolated libraries and synthetic model transports only."""

import pytest
from test_context_management import managed, post
from test_context_model_registry import requests
from test_context_processing_stages import audio, client
from test_context_scheduling import env, prepared

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.model_recovery import ModelRecovery
from collection_context.application.service import ContextService
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.profiles import ModelCatalog


def test_local_media_prepare_is_exposed_through_management_without_paid_submission(env, managed):
    store=env[0]
    item=store.upsert({'native_id':'923','title':'原媒体本地重准备'},kind='saved',scope_id='s_test')['item']
    PreparedInputs(store).save_source_media(item['id'],[(b'local-synthetic','video/mp4')],
        source_asset_hash=item.get('source_asset_hash'),content_hash=item['content_hash'])
    response=post(managed,'media-prepare',{'material_ref':item['id'],'mode':'audio','idempotency_key':'local-prepare'})
    assert response.status_code==200, response.text
    data=response.json()['data']
    assert data['state']=='queued' and data['max_calls']==0 and data['recorded_calls']==0
    status=post(managed,'link-status',{'job_id':data['job_id']}).json()['data']
    assert status['job_id']==data['job_id']


def test_cached_source_without_prepared_input_is_not_reported_as_missing_media(env, managed):
    store=env[0]
    item=store.upsert({'native_id':'926','title':'已下载待准备'},kind='saved',scope_id='s_test')['item']
    PreparedInputs(store).save_source_media(item['id'],[(b'saved-video','video/mp4')],
        source_asset_hash=item.get('source_asset_hash'),content_hash=item['content_hash'])
    response=post(managed,'process-preview',{'material_ref':item['id']})
    assert response.status_code==200
    data=response.json()['data']
    assert data['media_saved'] is True and data['input_id'] is None
    assert data['latest_job'] is None and data['planned_calls_before_reuse'] is None


def test_audio_only_intent_is_skipped_instead_of_failed_preparation(env, monkeypatch):
    store=env[0]
    item=store.upsert({'native_id':'927','title':'只转写音频'},kind='saved',scope_id='s_test')['item']
    identity=PreparedInputs(store).save(item['id'],content_hash=item['content_hash'],originals=[(b'fixture','video/mp4')],
        audio=[audio()],frames=[],coverage={'has_audio':True,'complete':False,'audio_only':True,
            'preparation_errors':{'vision':{'code':'vision_deferred'}}},
        processor_version='fixture',strategy_hash='fixture',kind='video')
    sent=[]
    monkeypatch.setattr(ModelCatalog,'client',lambda self, identity, secret:
        client(self.get(identity)['role'],'完整讲话' if self.get(identity)['role']=='audio' else '本期讲解内容 [a_000000]',sent,
            protocol='chat_audio' if self.get(identity)['role']=='audio' else 'chat'))
    job=env[3].submit(identity,idempotency_key='audio-only',max_calls=2)
    result=env[3].run(job['id'])
    assert result['stages']['vision_preparation_failed']['state']=='not_applicable'
    assert len(sent)==2
    assert 'vision_preparation_failed' not in store.get(item['id'])['artifacts']['audio']['coverage']['missing_stages']


def test_large_ocr_audit_does_not_overflow_status_or_transcript_http(env, managed):
    import json
    store=env[0]
    item=store.upsert({'native_id':'929','title':'已经转写的长视频'},kind='saved',scope_id='s_test')['item']
    coverage={'complete':False,'processing_partial':True,'missing_stages':['summary'],
        'source_coverage':{'selected_frames':183,'ocr_records':[{'text':'详细审计'*500} for _ in range(421)]}}
    store.save_artifact(item['id'],'audio','真实已有转写正文',processor_version='test',
        expected_content_hash=item['content_hash'],coverage=coverage)
    response=managed[0].get('/v1/collections/'+item['id']+'/status')
    assert response.status_code==200, response.text[:200]
    data=response.json()['data']
    assert data['artifacts']['audio']['state']=='ready'
    assert data['artifacts']['audio']['coverage']['source_coverage']['ocr_records_count']==421
    read=managed[0].post('/v1/collections/read',json={'material_ref':item['id'],'artifact':'audio','max_chars':2000},headers=managed[1])
    assert read.status_code==200 and read.json()['data']['text']=='真实已有转写正文'
    assert len(json.dumps(data))<20_000
    assert len(store.get(item['id'])['artifacts']['audio']['coverage']['source_coverage']['ocr_records'])==421


def test_manual_preparation_does_not_get_auto_submitted_to_models(env):
    from test_context_scheduling import enable
    from test_context_media import PNG
    from collection_context.workflows.media_preparation import MediaPreparation
    enable(env)
    item, identity=prepared(env)
    PreparedInputs(env[0]).save_source_media(item['id'],[(PNG,'image/png')],
        source_asset_hash=item.get('source_asset_hash'),content_hash=item['content_hash'])
    flow=MediaPreparation(env[0])
    job=flow.submit(material_ref=item['id'],mode='full',idempotency_key='manual-hold')
    assert env[4].admit_new()==[]
    assert flow.run(job['id'])['state']=='succeeded'
    assert env[4].admit_new()==[]


def test_all_media_calls_fail_skips_paid_summary_and_keeps_original(env, monkeypatch):
    item, identity = prepared(env)
    sent = requests(monkeypatch, lambda _: (_ for _ in ()).throw(ContextError("upstream_rejected", "合成拒绝")))
    job = env[3].submit(identity, idempotency_key="no-evidence", max_calls=2)
    result = env[3].run(job["id"])
    assert len(sent) == 1
    assert result["stages"]["summary"]["error"]["code"] == "no_usable_evidence"
    assert result["stages"]["publish"]["error"]["code"] == "no_extraction_output"
    assert "summary" not in env[0].get(item["id"])["artifacts"]
    assert item["title"] in ContextService(env[0]).read(item["id"], artifact="original")["text"]
    public = ManagementService(env[0]).overview()["jobs"][0]
    assert public["stage_details"]["summary"]["error_code"] == "no_usable_evidence"


def test_preview_discloses_preparation_gaps_without_model_requests(env, managed, monkeypatch):
    store = env[0]
    item = store.upsert({"native_id": "914", "title": "独立音频样例"}, kind="saved", scope_id="s_test")["item"]
    identity = PreparedInputs(store).save(
        item["id"], content_hash=item["content_hash"], originals=[(b"synthetic-source", "video/mp4")],
        audio=[audio()], frames=[], coverage={"has_audio": True, "complete": False,
            "preparation_errors": {"vision": {"code": "frame_limit"}}},
        processor_version="synthetic", strategy_hash="synthetic", kind="video",
    )
    sent = requests(monkeypatch)
    data = post(managed, "process-preview", {"material_ref": item["id"]}).json()["data"]
    assert data["input_id"] == identity and data["audio_segments"] == 1 and data["visual_frames"] == 0
    assert data["preparation_issues"] == [{"role": "vision", "code": "frame_limit"}]
    assert data["has_audio"] is True and data["latest_job"] is None
    assert sent == []


def test_repeated_click_does_not_queue_parallel_extractions(env, managed):
    item, identity = prepared(env)
    args = {"input_ids": [identity], "idempotency_key": "first-click", "max_calls": 2, "fee_confirmed": True}
    first = post(managed, "history", args).json()["data"]
    assert post(managed, "history", args).json()["data"] == first
    assert post(managed, "history", {**args, "idempotency_key": "reopen-dialog"}).json()["error"]["code"] == "processing_already_queued"
    assert len(env[0].snapshot()["jobs"]) == 1
    preview = post(managed, "process-preview", {"material_ref": item["id"]}).json()["data"]
    assert preview["latest_job"]["job_id"] == first["job_ids"][0]
    assert preview["latest_job"]["state"] == "queued"
    assert preview["has_audio"] is False


def test_partial_summary_is_persisted_as_partial_coverage(env, monkeypatch):
    item, identity = prepared(env)
    sent = []
    monkeypatch.setattr(ModelCatalog, "client", lambda self, identity, resolve:
        client(self.get(identity)["role"], "合成正文，没有引用", sent))
    job = env[3].submit(identity, idempotency_key="partial-summary", max_calls=2)
    result = env[3].run(job["id"])
    coverage = ContextService(env[0]).status(item["id"])["artifacts"]["summary"]["coverage"]
    assert result["state"] == "partial" and coverage["processing_partial"] is True
    assert ContextService(env[0]).read(item["id"], artifact="screen")["text"]
    assert ContextService(env[0]).status(item["id"])["artifacts"]["audio"]["state"] == "not_applicable"


def test_retry_does_not_offer_immutable_preparation_failure(env, monkeypatch):
    store = env[0]
    item = store.upsert({"native_id": "915", "title": "缺画面样例"}, kind="saved", scope_id="s_test")["item"]
    identity = PreparedInputs(store).save(
        item["id"], content_hash=item["content_hash"], originals=[(b"synthetic-source", "video/mp4")],
        audio=[audio()], frames=[], coverage={"has_audio": True, "complete": False,
            "preparation_errors": {"vision": {"code": "frame_limit"}}},
        processor_version="synthetic", strategy_hash="synthetic", kind="video",
    )
    sent = requests(monkeypatch)
    job = env[3].submit(identity, idempotency_key="prep-gap", max_calls=2)
    result = env[3].run(job["id"])
    assert result["state"] == "partial"
    before = len(sent)
    recovery = ModelRecovery(store)
    preview = recovery.preview(job["id"])
    gap = next(stage for stage in preview["stages"] if stage["name"] == "vision_preparation_failed")
    assert gap["selectable"] is False
    assert preview["preparation_required"] == ["vision_preparation_failed"]
    assert "vision_preparation_failed" not in preview["reused_stages"]
    with pytest.raises(ContextError) as error:
        recovery.preview(job["id"], ["vision_preparation_failed"])
    assert error.value.code == "invalid_retry_selection"
    assert len(sent) == before
