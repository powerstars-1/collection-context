"""Simple service UI shares one private key; no cloud inference in these tests."""
import json
import pytest
from test_context_model_registry import environment as environment
from test_context_model_setup import setup_page as setup_page
from collection_context.application.contracts import ContextError
from collection_context.application.management import ManagementService
from collection_context.application.model_services import ModelServices
from collection_context.application.model_setup import ModelSetup
from collection_context.application.service import ContextService

@pytest.fixture
def services(environment, monkeypatch):
    monkeypatch.setattr('collection_context.application.model_services.provider_catalog',lambda: [])
    return ModelServices(ModelSetup(environment[0],environment[1]))

def added(services):
    return services.save(service_id=None,name='测试网关',provider='newapi',base_url='https://fixture.invalid',
        api_key='synthetic-key-only',expected_revision=None)

def role(service,model='fixture-text',protocol='pi_chat'):
    return dict(service_id=service,model=model,protocol=protocol,timeout=120,parameters={})

def expected(value):
    return {role:entry['profile_id'] for role,entry in value['assignments'].items()}

def test_one_key_and_atomic_three_roles(services,environment,monkeypatch):
    value=added(services); identity=value['services'][0]['id']
    assert value['services'][0]['base_url']=='https://fixture.invalid/v1'
    monkeypatch.setattr(environment[1],'get',lambda _:pytest.fail('setting must not read a secret'))
    configured=services.assign(assignments={'audio':role(identity,'fixture-audio','transcription'),
        'vision':role(identity,'fixture-image'),'summary':role(identity)},expected_profiles=expected(value))
    profiles=environment[0].snapshot()['model_profiles']
    assert len({profile['credential_ref'] for profile in profiles.values()})==1
    assert 'synthetic-key-only' not in json.dumps(configured)
    assert all(value['profile_id'] for value in configured['assignments'].values())
    before=environment[0].snapshot()
    with pytest.raises(ContextError):
        services.assign(assignments={'summary':role(identity)},expected_profiles=expected(value))
    assert before==environment[0].snapshot()

def test_bad_second_role_cannot_partially_save(services,environment):
    value=added(services); identity=value['services'][0]['id'];before=environment[0].snapshot()
    with pytest.raises(ContextError):
        services.assign(assignments={'summary':role(identity),'vision':role(identity,protocol='transcription')},expected_profiles=expected(value))
    assert before==environment[0].snapshot()

def test_rotation_preserves_old_fixed_profiles(services,environment):
    value=added(services); service=value['services'][0]
    value=services.assign(assignments={'summary':role(service['id'])},expected_profiles=expected(value))
    old=value['assignments']['summary']['profile_id'];profile=environment[0].snapshot()['model_profiles'][old]
    rotated=services.save(service_id=service['id'],name=service['name'],provider='newapi',base_url=service['base_url'],api_key='synthetic-rotated-only',expected_revision=1)
    assert old!=rotated['assignments']['summary']['profile_id']
    assert profile==environment[0].snapshot()['model_profiles'][old]
    assert environment[1].get(profile['credential_ref'])=='synthetic-key-only'

def test_remove_and_malformed_model_list(services,environment,monkeypatch):
    value=added(services);identity=value['services'][0]['id']
    class Response:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,_):return b'[]'
    monkeypatch.setattr('collection_context.application.model_services.direct_transport',lambda *a,**k:Response())
    with pytest.raises(ContextError) as caught:services.model_list(service_id=identity)
    assert caught.value.code=='model_list_failed'
    assert services.remove(service_id=identity)['services']==[]

def test_note_save_is_versioned_and_searchable(environment):
    store=environment[0];item=store.upsert({'native_id':'881','title':'测试备注','media_type':'video'},kind='saved',scope_id='s_saved')['item']
    manager=ManagementService(store)
    value=manager.save_note(material_ref=item['id'],text='我希望复用的关键步骤',expected_version=None)
    assert ContextService(store).read(item['id'],artifact='user_note')['text']=='我希望复用的关键步骤'
    with pytest.raises(ContextError):manager.save_note(material_ref=item['id'],text='旧页面覆盖',expected_version=None)
    assert manager.save_note(material_ref=item['id'],text='更新备注',expected_version=value['version'])['model_requests']==0


def test_sdk_provider_is_not_limited_to_an_application_whitelist(services,environment,monkeypatch):
    entry={'id':'future-sdk-provider','name':'Future SDK','base_url':'https://fixture.invalid/native',
           'source':'sdk','configurable':True,'models':[{'id':'native-image','name':'Native image','input':['text','image']}]}
    monkeypatch.setattr('collection_context.application.model_services.provider_catalog',lambda:[entry])
    assert services.settings()['providers'][0]['id']=='future-sdk-provider'
    value=services.save(service_id=None,name='Future SDK',provider=entry['id'],base_url='',api_key='synthetic-only',expected_revision=None)
    identity=value['services'][0]['id']
    value=services.assign(assignments={'vision':role(identity,'native-image')},expected_profiles=expected(value))
    profile=environment[0].snapshot()['model_profiles'][value['assignments']['vision']['profile_id']]
    assert profile['provider']==entry['id'] and profile['base_url']==entry['base_url']
    client=services.setup.catalog.client(value['assignments']['vision']['profile_id'],environment[1].get)
    assert client.profile.provider==entry['id']
    monkeypatch.setattr(environment[1],'get',lambda _:pytest.fail('SDK catalog must not read the key'))
    assert services.model_list(service_id=identity)['models']==entry['models']


def test_native_provider_rejects_unknown_model_and_text_only_vision_without_saving(services,environment,monkeypatch):
    entry={'id':'native-provider','name':'Native','base_url':'https://fixture.invalid',
           'source':'sdk','configurable':True,'models':[{'id':'text-only','name':'Text','input':['text']}]}
    monkeypatch.setattr('collection_context.application.model_services.provider_catalog',lambda:[entry])
    value=services.save(service_id=None,name='Native',provider=entry['id'],base_url='',api_key='synthetic-only',expected_revision=None)
    identity=value['services'][0]['id'];before=environment[0].snapshot()
    for selected in [role(identity,'unknown'),role(identity,'text-only')]:
        with pytest.raises(ContextError):services.assign(assignments={'vision':selected},expected_profiles=expected(value))
        assert environment[0].snapshot()==before


def test_extra_auth_is_shown_but_not_falsely_saved_as_an_api_key_service(services,environment,monkeypatch):
    entry={'id':'oauth-only','name':'OAuth provider','base_url':'https://fixture.invalid',
           'source':'sdk','configurable':False,'unavailable_reason':'需要登录授权','models':[]}
    monkeypatch.setattr('collection_context.application.model_services.provider_catalog',lambda:[entry])
    assert services.settings()['providers'][0]['unavailable_reason']=='需要登录授权'
    before=environment[0].snapshot()
    with pytest.raises(ContextError) as caught:
        services.save(service_id=None,name='OAuth provider',provider=entry['id'],base_url='',api_key='synthetic-only',expected_revision=None)
    assert caught.value.code=='provider_setup_required' and environment[0].snapshot()==before


@pytest.mark.parametrize('api,address,normalized',[
    ('openai-completions','https://fixture.invalid','https://fixture.invalid/v1'),
    ('openai-completions','https://fixture.invalid/api/v3/chat/completions','https://fixture.invalid/api/v3'),
    ('openai-responses','https://fixture.invalid/v1/responses','https://fixture.invalid/v1'),
    ('anthropic-messages','https://fixture.invalid/v1/messages','https://fixture.invalid'),
    ('google-generative-ai','https://fixture.invalid','https://fixture.invalid/v1beta'),
])
def test_custom_protocol_is_persisted_pinned_and_used_by_sdk(services,environment,api,address,normalized):
    value=services.save(service_id=None,name='自定义接口',provider='custom',base_url=address,
        api_key='synthetic-only',expected_revision=None,api=api)
    service=value['services'][0]
    assert service['base_url']==normalized and service['api']==api
    value=services.assign(assignments={'summary':role(service['id'])},expected_profiles=expected(value))
    profile_id=value['assignments']['summary']['profile_id']
    client=services.setup.catalog.client(profile_id,environment[1].get)
    assert client.profile.api==api
    assert 'synthetic-only' not in json.dumps(value)
    before=environment[0].snapshot()['model_profiles'][profile_id]
    changed=services.save(service_id=service['id'],name=service['name'],provider='custom',base_url='https://fixture.invalid',
        api_key='',expected_revision=1,api='openai-completions' if api!='openai-completions' else 'anthropic-messages')
    assert changed['assignments']['summary']['profile_id']!=profile_id
    assert environment[0].snapshot()['model_profiles'][profile_id]==before


def test_invalid_custom_protocol_and_incompatible_audio_are_rejected(services,environment):
    before=environment[0].snapshot()
    with pytest.raises(ContextError):
        services.save(service_id=None,name='无效协议',provider='custom',base_url='https://fixture.invalid',
            api_key='synthetic-only',expected_revision=None,api='fake-protocol')
    assert environment[0].snapshot()==before
    value=services.save(service_id=None,name='自定义接口',provider='custom',base_url='https://fixture.invalid',
        api_key='synthetic-only',expected_revision=None,api='anthropic-messages')
    before=environment[0].snapshot()
    with pytest.raises(ContextError):
        services.assign(assignments={'audio':role(value['services'][0]['id'],'audio','transcription')},expected_profiles=expected(value))
    assert environment[0].snapshot()==before


def test_service_protocol_cannot_break_assigned_audio(services,environment):
    value=services.save(service_id=None,name='音频服务',provider='custom',base_url='https://fixture.invalid',
        api_key='synthetic-only',expected_revision=None)
    identity=value['services'][0]['id']
    services.assign(assignments={'audio':role(identity,'audio','transcription')},expected_profiles=expected(value))
    before=environment[0].snapshot()
    with pytest.raises(ContextError) as caught:
        services.save(service_id=identity,name='音频服务',provider='custom',base_url='https://fixture.invalid',
            api_key='',expected_revision=1,api='anthropic-messages')
    assert caught.value.code=='model_service_in_use' and environment[0].snapshot()==before


def test_custom_protocol_round_trip_through_real_management_http(setup_page,monkeypatch):
    monkeypatch.setattr('collection_context.application.model_services.provider_catalog',lambda:[])
    client,headers,*_=setup_page
    saved=client.post('/v1/management/model-service-save',headers=headers,json={
        'service_id':None,'name':'HTTP协议样例','provider':'custom','base_url':'https://fixture.invalid',
        'api_key':'synthetic-http-only','expected_revision':None,'api':'anthropic-messages'})
    assert saved.status_code==200
    data=saved.json()['data'];identity=data['services'][0]['id']
    assigned=client.post('/v1/management/model-assignments',headers=headers,json={
        'assignments':{'vision':role(identity,'fixture-vision')},'expected_profiles':expected(data)})
    assert assigned.status_code==200
    settings=client.post('/v1/management/model-services',headers=headers,json={}).json()['data']
    assert settings['services'][0]['api']=='anthropic-messages'
    assert settings['assignments']['vision']['model']=='fixture-vision'
    assert 'synthetic-http-only' not in json.dumps(settings)
