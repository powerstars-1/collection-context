"""Actual bundled SDK request against a localhost SSE fixture, never a cloud model."""
import base64
import json
import threading
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from test_context_media import PNG
from collection_context.processing.models import ModelProfile
from collection_context.processing.pi_client import PiModelClient,invoke

def test_launchd_minimal_path_can_resolve_installed_node(monkeypatch):
    import sys
    from collection_context.processing.pi_client import node_runtime
    if sys.platform!='darwin':pytest.skip('macOS launchd behavior')
    monkeypatch.setenv('PATH','/usr/bin:/bin')
    monkeypatch.delenv('COLLECTION_CONTEXT_NODE',raising=False)
    if not node_runtime():pytest.skip('No user/Homebrew Node installed')
    assert invoke({'action':'catalog'})

def test_catalog_is_offline_and_has_image_capabilities():
    providers=invoke({'action':'catalog'})
    runtime=Path(__file__).parents[1]/'model-runtime'
    result=subprocess.run(['node','--input-type=module','-e',
        "import {builtinProviders} from '@earendil-works/pi-ai/providers/all'; console.log(JSON.stringify(builtinProviders().map(p=>p.id)))"],
        cwd=runtime,capture_output=True,check=True)
    assert {p['id'] for p in providers}==set(json.loads(result.stdout))
    assert {'anthropic','google','openai','deepseek','xiaomi-token-plan-cn','qwen-token-plan-cn'}<={p['id'] for p in providers}
    assert any('image' in m['input'] for p in providers for m in p['models'])
    assert all(p['source']=='sdk' and 'configurable' in p for p in providers)


@pytest.mark.parametrize('provider,api,model,path',[
    ('anthropic',None,'claude-haiku-4-5','/v1/messages'),
    ('google',None,'gemini-2.5-flash','/models/gemini-2.5-flash:streamGenerateContent?alt=sse'),
    ('openai',None,'gpt-4o-mini','/responses'),
    (None,'anthropic-messages','custom-claude','/v1/messages'),
    (None,'google-generative-ai','custom-gemini','/models/custom-gemini:streamGenerateContent?alt=sse'),
    (None,'openai-responses','custom-responses','/responses'),
])
def test_native_sdk_routes_each_provider_to_its_own_protocol(provider,api,model,path):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path,{key.lower():value for key,value in self.headers.items()},body))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            if provider=='anthropic' or api=='anthropic-messages':
                events=[
                    {'type':'message_start','message':{'id':'m_test','type':'message','role':'assistant','model':model,'content':[], 'usage':{'input_tokens':4,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'原生接口正常'}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':2}},
                    {'type':'message_stop'},
                ]
            elif provider=='google' or api=='google-generative-ai':
                events=[{'candidates':[{'content':{'role':'model','parts':[{'text':'原生接口正常'}]},'finishReason':'STOP'}],
                         'modelVersion':model,'usageMetadata':{'promptTokenCount':4,'candidatesTokenCount':2,'totalTokenCount':6}}]
            else:
                item={'type':'message','id':'item_test','role':'assistant','content':[{'type':'output_text','text':'原生接口正常','annotations':[]}]}
                events=[
                    {'type':'response.created','response':{'id':'r_test','model':model}},
                    {'type':'response.output_item.added','output_index':0,'item':{**item,'content':[]}},
                    {'type':'response.content_part.added','output_index':0,'content_index':0,'part':{'type':'output_text','text':''}},
                    {'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'原生接口正常'},
                    {'type':'response.output_item.done','output_index':0,'item':item},
                    {'type':'response.completed','response':{'id':'r_test','model':model,'status':'completed','output':[item], 'usage':{'input_tokens':4,'output_tokens':2,'total_tokens':6}}},
                ]
            for event in events:
                self.wfile.write((('event: '+event['type']+'\n' if 'type' in event else '')+'data: '+json.dumps(event)+'\n\n').encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=PiModelClient(ModelProfile(f'http://127.0.0.1:{server.server_port}',model,'synthetic-only',protocol='pi_chat',provider=provider,api=api,timeout=10))
        result=client._chat([{'type':'text','text':'原创原生协议样例'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(PNG).decode()}}])
        assert result.text=='原生接口正常' and len(seen)==1 and seen[0][0].split('?')[0]==path.split('?')[0]
        assert result.usage['input_tokens']==4 and result.usage['output_tokens']==2
        if provider=='anthropic' or api=='anthropic-messages':
            assert seen[0][1]['x-api-key']=='synthetic-only'
            assert seen[0][2]['messages'][0]['content'][1]['type']=='image'
        elif provider=='google' or api=='google-generative-ai':
            assert seen[0][1]['x-goog-api-key']=='synthetic-only'
            assert 'inlineData' in seen[0][2]['contents'][0]['parts'][1]
        else:assert seen[0][1]['authorization']=='Bearer synthetic-only'
    finally:server.shutdown();server.server_close();thread.join(timeout=2)

@pytest.mark.parametrize('with_usage',[True,False])
def test_sdk_streaming_text_and_image_preserves_actual_usage(with_usage):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path,self.headers.get('Authorization'),value))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('x-request-id','fixture-request');self.end_headers()
            self.wfile.write(('data: '+json.dumps({'id':'test-id','model':'actual-fixture','choices':[{'index':0,'delta':{'role':'assistant','content':'测试已完成'},'finish_reason':None}]})+'\n\n').encode())
            payload={'id':'test-id','model':'actual-fixture','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}
            if with_usage:payload['usage']={'prompt_tokens':12,'completion_tokens':4,'total_tokens':16,'prompt_tokens_details':{'cached_tokens':3}}
            self.wfile.write(('data: '+json.dumps(payload)+'\n\ndata: [DONE]\n\n').encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=PiModelClient(ModelProfile(f'http://127.0.0.1:{server.server_port}/v1','fixture-image','synthetic-only',protocol='pi_chat',timeout=10,parameters={'temperature':0.2}))
        result=client._chat([{'type':'text','text':'识别测试图'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(PNG).decode()}}])
        assert len(seen)==1 and seen[0][0]=='/v1/chat/completions'
        assert seen[0][1]=='Bearer synthetic-only'
        assert seen[0][2]['model']=='fixture-image' and seen[0][2]['temperature']==0.2
        assert seen[0][2]['messages'][0]['content'][1]['type']=='image_url'
        assert result.text=='测试已完成' and result.actual_model=='actual-fixture'
        assert result.upstream_request_id=='fixture-request'
        assert result.usage==({'prompt_tokens':12,'completion_tokens':4,'total_tokens':16,'prompt_tokens_details':{'cached_tokens':3}} if with_usage else None)
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
