import assert from 'node:assert/strict';
import test from 'node:test';
import {agentSetupView} from '../src/lib/agent-setup.js';

function fixture({legacy=false,windows=false}={}) {
  const command=windows?'C:\\Program Files\\收藏上下文\\CollectionContext.exe':'/Applications/收藏 上下文/CollectionContext';
  const workspace=windows?'D:\\资料 空格\\收藏库':'/Volumes/资料 空格/收藏库';
  const common=['--workspace',workspace,...(legacy?['--legacy-vault']:[])];
  return {
    library_mode:legacy?'legacy_readonly':'managed',runtime_kind:'frozen_console',
    mcp:{available:true,configuration:{mcpServers:{'collection-context':{command,args:['mcp',...common]}}},reason:null,same_computer_only:true},
    cli:{available:true,examples:[{action:'search',command,args:['cli',...common,'search','--query','UI 提示词','--limit','3']}]},
    http:{base_url:'http://127.0.0.1:18798',authentication:'Authorization: Bearer <专用只读产品口令>',
      read_paths:[{method:'POST',path:'/v1/collections/search'},{method:'POST',path:'/v1/collections/read'},{method:'GET',path:'/v1/collections/{material_ref}/status'}],
      same_computer_only:true,remote_note:'当前地址仅同机可访问。'},
    read_only_tools:['search_collections','read_collection','collection_status'],model_requests:0,platform_requests:0,
  };
}

for(const legacy of [false,true])for(const windows of [false,true]) {
  test(`setup JSON preserves actual Unicode/space argv; legacy=${legacy}, Windows=${windows}`,()=>{
    const data=fixture({legacy,windows});
    const before=structuredClone(data);
    const view=agentSetupView(data,'http://127.0.0.1:18798',legacy);
    assert.deepEqual(JSON.parse(view.mcp.text),data.mcp.configuration);
    assert.deepEqual(JSON.parse(view.cli.text),data.cli.examples);
    assert.deepEqual(data,before);
    assert.equal(view.libraryMode,data.library_mode);
    assert.equal(view.tools.length,3);
    assert.equal(view.mcp.text.includes('--legacy-vault'),legacy);
    assert.match(view.http.example,/^POST http:\/\/127\.0\.0\.1:18798\/v1\/collections\/search\n/);
    assert.match(view.http.example,/Bearer <专用只读产品口令>/);
    assert.match(view.http.pathsText,/GET \/v1\/collections\/\{material_ref\}\/status$/);
    assert.equal(view.http.sameComputerOnly,true);
  });
}

test('source developer PYTHONPATH is retained without loading process environment',()=>{
  const data=fixture();
  data.runtime_kind='development_source';data.mcp.reason='需要现有 Python；尚未验证宿主接入。';
  const env={PYTHONPATH:'/开发 目录/backend/src'};
  data.mcp.configuration.mcpServers['collection-context'].env=env;
  data.cli.examples[0].env=env;
  const view=agentSetupView(data,'http://127.0.0.1:18798');
  assert.deepEqual(JSON.parse(view.mcp.text).mcpServers['collection-context'].env,env);
  assert.deepEqual(JSON.parse(view.cli.text)[0].env,env);
  assert.match(view.runtimeLabel,/开发源码环境.*Python/);
  assert.equal(view.mcp.reason,data.mcp.reason);
});

test('missing console leaves HTTP available and never invents fallback commands',()=>{
  const data=fixture();
  data.runtime_kind='unavailable';
  data.mcp={available:false,configuration:null,reason:'console companion missing',same_computer_only:true};
  data.cli={available:false,examples:[]};
  const view=agentSetupView(data,'http://127.0.0.1:18798');
  assert.equal(view.mcp.available,false);assert.equal(view.mcp.text,'');
  assert.equal(view.mcp.reason,'console companion missing');
  assert.equal(view.cli.available,false);assert.equal(view.cli.text,'[]');
  assert.match(view.http.example,/127\.0\.0\.1:18798/);
  assert.doesNotMatch(JSON.stringify(view),/collection-context-mcp/);
});

test('authorized HTTPS origin and readonly metadata are preserved without proxy guessing',()=>{
  const data=fixture();data.http.base_url='https://library.example.test/';
  data.http.same_computer_only=false;data.http.remote_note='访问范围以部署配置为准。';
  const view=agentSetupView(data,'https://library.example.test');
  assert.equal(view.http.baseUrl,'https://library.example.test');
  assert.match(view.http.example,/^POST https:\/\/library\.example\.test\//);
  assert.equal(view.http.remoteNote,data.http.remote_note);
});

const changes=[
  ['unknown runtime',d=>{d.runtime_kind='made_up_installer'}],
  ['wrong library mode',d=>{d.library_mode='legacy_readonly'}],
  ['paid count',d=>{d.model_requests=1}],
  ['platform count',d=>{d.platform_requests=1}],
  ['unknown tool',d=>{d.read_only_tools[0]='delete_collection'}],
  ['duplicate tool',d=>{d.read_only_tools[0]='read_collection'}],
  ['remote MCP mode',d=>{d.mcp.same_computer_only=false}],
  ['bare guessed command',d=>{d.mcp.configuration.mcpServers['collection-context'].command='collection-context-mcp'}],
  ['shell string args',d=>{d.mcp.configuration.mcpServers['collection-context'].args='--workspace /somewhere'}],
  ['arbitrary server fields',d=>{d.mcp.configuration.mcpServers['collection-context'].apiKey='synthetic-secret'}],
  ['model environment key',d=>{d.mcp.configuration.mcpServers['collection-context'].env={MODEL_API_KEY:'synthetic-secret'}}],
  ['CLI token environment',d=>{d.cli.examples[0].env={TOKEN:'synthetic-secret'}}],
  ['CLI extra secret field',d=>{d.cli.examples[0].token='synthetic-secret'}],
  ['multiple servers',d=>{d.mcp.configuration.mcpServers.other={command:'/bin/sh',args:[]}}],
  ['missing MCP configuration',d=>{d.mcp.configuration=null}],
  ['unavailable with configuration',d=>{d.mcp.available=false}],
  ['available CLI without examples',d=>{d.cli.examples=[]}],
  ['unavailable CLI with examples',d=>{d.cli.available=false}],
  ['HTTP secret auth',d=>{d.http.authentication='Authorization: Bearer synthetic-secret'}],
  ['off-origin HTTP',d=>{d.http.base_url='https://outside.invalid'}],
  ['username in HTTP',d=>{d.http.base_url='http://user@127.0.0.1:18798'}],
  ['query in HTTP',d=>{d.http.base_url='http://127.0.0.1:18798/?token=synthetic-secret'}],
  ['hash in HTTP',d=>{d.http.base_url='http://127.0.0.1:18798/#token=synthetic-secret'}],
  ['relative HTTP',d=>{d.http.base_url='/'}],
  ['inexact status route',d=>{d.http.read_paths[2].path='/v1/collections/status'}],
  ['management route',d=>{d.http.read_paths[1]={method:'POST',path:'/v1/management/library/delete'}}],
];
for(const [name,change] of changes)test(`invalid setup fails closed: ${name}`,()=>{
  const data=fixture();change(data);
  assert.throws(()=>agentSetupView(data,'http://127.0.0.1:18798'));
});

test('unrelated envelope metadata cannot enter displayed or copied setup',()=>{
  const data=fixture();data.session_token='synthetic-hidden-session';data.owner_key='synthetic-hidden-owner';
  const view=agentSetupView(data,'http://127.0.0.1:18798');
  assert.doesNotMatch(JSON.stringify(view),/synthetic-hidden/);
});
