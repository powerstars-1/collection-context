"""Configurable originals, separate derived/model budgets; no live platform/cloud calls."""
from types import SimpleNamespace
import pytest
from test_context_management import managed, post
from test_context_scheduling import env
from test_context_adaptive_preparation import local
from test_context_public_http import transport
from collection_context.application.contracts import ContextError
from collection_context.infrastructure.download_limits import download_max_bytes, MAX_SOURCE_BYTES
from collection_context.infrastructure.public_http import PublicHTTP
from collection_context.infrastructure import public_http
from collection_context.infrastructure.media import MediaPolicy
from collection_context.processing.models import MAX_INPUT_BYTES
from collection_context.workflows.ingestion import IngestionWorkflow


def test_download_setting_persists_and_old_clients_do_not_reset_it(managed, env):
    default = post(managed, 'preferences', {}).json()['data']
    assert default['max_download_mb'] == 1024
    assert post(managed, 'preferences-save', {'download_media': True, 'max_download_mb': 768}).json()['ok']
    assert post(managed, 'preferences', {}).json()['data']['max_download_mb'] == 768
    post(managed, 'preferences-save', {'download_media': False})
    assert post(managed, 'preferences', {}).json()['data']['max_download_mb'] == 768
    workflow = IngestionWorkflow(env[0], SimpleNamespace())
    assert workflow.downloads.max_bytes == 768_000_000
    assert MediaPolicy().max_source_bytes == MAX_SOURCE_BYTES
    assert MAX_INPUT_BYTES == 32_000_000


@pytest.mark.parametrize('value',[0,-1,2049,True,1.5,'1024',None])
def test_invalid_size_is_rejected_without_mutating_preferences(managed, env, value):
    before = env[0].snapshot()
    result = post(managed, 'preferences-save', {'download_media': True, 'max_download_mb': value})
    assert result.json()['ok'] is False and env[0].snapshot() == before


def test_explicit_size_above_old_128mb_ceiling_is_accepted(monkeypatch):
    transport(monkeypatch, [{'body': b'small-fixture'}])
    assert PublicHTTP(hosts=frozenset({'cdn.example'})).get('https://cdn.example/a',max_bytes=512_000_000).data == b'small-fixture'


def test_129mb_original_saves_prepares_and_reads_with_existing_registry(local):
    store,item,registry,_ = local
    data = b'\x00\x00\x00\x14ftypisom' + bytes(129_000_000-12)
    registry.save_source_media(item['id'], [(data,'video/mp4')],source_asset_hash=item.get('source_asset_hash'), content_hash=item['content_hash'])
    identity = registry.prepare_video(item['id'],data,audio_only=True)
    manifest = registry.load(identity)
    assert manifest['originals'][0]['bytes'] == 129_000_000
    try:
        store.transact(lambda state: state['settings'].update(max_download_mb=1))
        assert registry.source_media(item['id'],item.get('source_asset_hash'))[0][0] == data
    finally:
        # Explicit synthetic blob in this isolated fixture, not a real library.
        store.files.unlink(manifest['originals'][0]['path'])


def test_original_bytes_do_not_consume_frame_and_audio_budget(local,monkeypatch):
    store,item,registry,_ = local
    monkeypatch.setattr('collection_context.processing.inputs.MAX_TOTAL',1_000_000)
    data = bytes(2_000_000)
    identity = registry.prepare_video(item['id'],data,audio_only=True)
    assert registry.load(identity)['originals'][0]['bytes'] == len(data)


def test_large_transfer_rolls_to_disk_and_cleans_the_spool(monkeypatch):
    factory = public_http.tempfile.SpooledTemporaryFile
    spools=[]
    def open_spool(*args,**kwargs):
        spool=factory(*args,**kwargs);spools.append(spool);return spool
    monkeypatch.setattr(public_http.tempfile,'SpooledTemporaryFile',open_spool)
    transport(monkeypatch,[{'body':bytes(9_000_000)}])
    result=PublicHTTP(hosts=frozenset({'cdn.example'})).get('https://cdn.example/a',max_bytes=12_000_000)
    assert len(result.data)==9_000_000 and spools[0]._rolled and spools[0].closed
