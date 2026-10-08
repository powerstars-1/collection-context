"""Owner UI contracts using synthetic models, sources and media only."""
from types import SimpleNamespace

import pytest
from test_context_model_registry import environment as environment, configure

from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.source_management import SourceManagement
from collection_context.processing.model_check import submit
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.extraction import ExtractionWorkflow


@pytest.mark.parametrize('role', ['audio', 'vision', 'summary'])
def test_single_probe_uses_normal_executor_and_records_one_call(environment, monkeypatch, role):
    store, secrets, key = environment
    configured = configure(store, key, role, 'synthetic-' + role, protocol='transcription' if role == 'audio' else 'chat')
    sent = []
    def call(*args, **kwargs):
        sent.append((args, kwargs))
        return SimpleNamespace(text='合成接口回复', status='ready', actual_model='synthetic-returned',
            usage={'total_tokens': 9}, upstream_request_id='fixture', elapsed_seconds=0.1)
    monkeypatch.setattr(ModelCatalog, 'client', lambda *args: SimpleNamespace(text=call, image=call, audio=call))
    job = submit(store, role=role, expected_profile_id=configured,
        idempotency_key='original-probe-' + role, allow_model_calls=True)
    assert not sent
    completed = ExtractionWorkflow(store, secrets.get).run(job['id'])
    assert completed['state'] == 'succeeded' and len(sent) == 1 and len(completed['calls']) == 1
    result = ManagementService(store).dispatch('model-check-status', {'job_id': job['id']})
    assert result['data']['probe_result']['text'] == '合成接口回复'
    assert result['data']['calls'][0]['usage'] == {'total_tokens': 9}
    assert not result['data']['can_retry']
    ExtractionWorkflow(store, secrets.get).run(job['id'])
    assert len(sent) == 1


def test_probe_needs_saved_role_and_explicit_authorization(environment):
    store, _, key = environment
    profile = configure(store, key, 'summary', 'synthetic')
    args = dict(role='summary', expected_profile_id=profile, idempotency_key='fixture-probe')
    with pytest.raises(ContextError): submit(store, **args)
    with pytest.raises(ContextError): submit(store, **{**args, 'role': []}, allow_model_calls=True)
    with pytest.raises(ContextError): submit(store, **{**args, 'expected_profile_id': 'p_changed'}, allow_model_calls=True)
    assert not store.snapshot()['jobs']


def test_preferences_persist_without_calls(environment):
    manager = ManagementService(environment[0])
    assert manager.dispatch('preferences', {})['data']['download_media'] is True
    assert manager.dispatch('preferences-save', {'download_media': False})['data']['model_requests'] == 0
    assert manager.dispatch('preferences', {})['data']['download_media'] is False
    assert manager.dispatch('preferences-save', {'download_media': 1})['error']['code'] == 'invalid_argument'
    assert not environment[0].snapshot()['jobs']


def test_source_edit_and_removal_keep_saved_material(environment):
    store = environment[0]
    manager = SourceManagement(store)
    source = manager.create(creator_url='https://www.douyin.com/user/fixture', limit=5, download=False)
    item = store.upsert({'native_id': '881', 'title': '原创来源样例', 'media_type': 'video'},
        kind='creator', scope_id=source['scope_id'])['item']
    updated = manager.edit(config_id=source['config_id'], limit=10, download=True)['scopes'][0]
    assert updated['config_id'] != source['config_id'] and updated['limit'] == 10 and updated['download']
    with pytest.raises(ContextError): manager.remove(config_id=source['config_id'])
    assert manager.remove(config_id=updated['config_id'])['scopes'] == []
    assert store.get(item['id'])['title'] == '原创来源样例'
