"""One media branch failing must not erase the other, or bypass the selected SDK."""
from types import SimpleNamespace

import pytest
from test_context_processing_stages import audio, frame, client

from collection_context.application.contracts import ContextError
from collection_context.application.service import ContextService
from collection_context.library.store import LibraryStore
from collection_context.processing.inputs import PreparedInputs
from collection_context.processing.models import ModelProfile
from collection_context.processing.pi_client import PiModelClient
from collection_context.processing.profiles import ModelCatalog
from collection_context.processing.stages import vision_stage, summary_stage
from collection_context.workflows.extraction import ExtractionWorkflow


@pytest.mark.parametrize('failed_role', ['audio', 'vision', 'both'])
def test_independent_preparation_and_partial_publication(tmp_path, monkeypatch, failed_role):
    store = LibraryStore.initialize(tmp_path / '库')
    try:
        item = store.upsert({'native_id':'57','title':'原创独立分支测试'},kind='saved',scope_id='s_test')['item']
        class Media:
            strategy_hash = 'independent-fixture'
            info = SimpleNamespace(has_audio=True)
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def audio_segments(self):
                if failed_role in {'audio','both'}: raise ContextError('media_decode_failed', 'fixture')
                return [audio()]
            def scan_frames(self):
                if failed_role in {'vision','both'}: raise ContextError('frame_limit', 'fixture')
                return [frame().candidate], {'complete':False}
            def frames(self, candidates, consumer): consumer(frame())
        monkeypatch.setattr('collection_context.processing.inputs.LocalMedia',Media)
        inputs = PreparedInputs(store)
        if failed_role == 'both':
            with pytest.raises(ContextError): inputs.prepare_video(item['id'],b'fixture')
            assert not store.get(item['id']).get('prepared_input')
            return
        identity = inputs.prepare_video(item['id'],b'fixture')
        payload = inputs.load(identity)
        assert bool(payload['audio']) == (failed_role != 'audio')
        assert bool(payload['frames']) == (failed_role != 'vision')
        assert set(payload['coverage']['preparation_errors']) == {failed_role}
        catalog = ModelCatalog(store)
        for role in ('audio','vision','summary'):
            catalog.configure(role=role,base_url='https://fixture.invalid/v1',model=role,
                protocol='chat_audio' if role=='audio' else 'chat',credential_ref='k_'+'1'*32)
        requests=[]
        def resolve(self, profile_id, secret):
            role=self.get(profile_id)['role']
            text={'audio':'真实保留的讲话','vision':'原图文字','summary':'本次仅使用可用证据，存在提取缺口。'}[role]
            return client(role,text,requests,protocol='chat_audio' if role=='audio' else 'chat')
        monkeypatch.setattr(ModelCatalog,'client',resolve)
        workflow=ExtractionWorkflow(store,lambda _: 'synthetic-only')
        job=workflow.submit(identity,idempotency_key='independent-test',max_calls=2)
        result=workflow.run(job['id'])
        assert result['state']=='partial' and len(requests)==2
        assert all(r['model']!=failed_role for r in requests)
        artifacts=store.get(item['id'])['artifacts']
        good='audio' if failed_role=='vision' else 'screen'
        assert artifacts[good]['state']=='ready'
        assert artifacts[good]['coverage']['complete'] is False
        assert failed_role+'_preparation_failed' in artifacts[good]['coverage']['missing_stages']
        assert 'readable' not in artifacts
        text=ContextService(store).read(item['id'],artifact=good)['text']
        assert failed_role+'_preparation_failed' in text
        if failed_role=='audio': assert 'audio_not_applicable' not in text
    finally: store.close()


def test_sdk_stages_keep_pi_adapter_and_selected_protocol(monkeypatch):
    requests=[]
    def invoke(value, timeout):
        requests.append(value)
        return {'text':'原图有参数390 [f_000000]','actual_model':'fixture-pi',
                'usage':{'total_tokens':8},'status':'ready'}
    monkeypatch.setattr('collection_context.processing.pi_client.invoke',invoke)
    sdk=PiModelClient(ModelProfile('https://fixture.invalid/v1','sdk-model','synthetic-only',
        protocol='pi_chat',api='anthropic-messages'))
    vision=vision_stage('i_test',frame(),sdk)
    result=vision.invoke({}).record()
    summary=summary_stage('i_test',{},(vision.name,),sdk,source_coverage={'complete':False})
    summary.invoke({vision.name:result})
    assert len(requests)==2
    assert all(r['profile']['api']=='anthropic-messages' for r in requests)
    assert requests[0]['content'][1]['type']=='image_url'
