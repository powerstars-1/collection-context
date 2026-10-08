import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {searchProviders,modelsForRole,supportsRole,defaultProtocol,serviceSupportsRole,modelEndpoint,modelApiOptions} from '../src/management/modelCatalog.js';

test('supplier search consumes every SDK entry without a frontend whitelist',()=>{
  const providers=[{id:'new-sdk-provider',name:'未来供应商'},{id:'anthropic',name:'Anthropic'}];
  assert.deepEqual(searchProviders(providers,''),providers);
  assert.equal(searchProviders(providers,' future').length,0);
  assert.equal(searchProviders(providers,'未来')[0].id,'new-sdk-provider');
  assert.equal(searchProviders(providers,' ANTHROPIC ')[0].id,'anthropic');
});

test('role selection filters visual models and does not mistake a chat catalog for ASR',()=>{
  const provider={source:'sdk',models:[{id:'text',input:['text']},{id:'image',input:['text','image']}],audio_protocols:[],audio_models:[]};
  assert.deepEqual(modelsForRole(provider,'vision').map(model=>model.id),['image']);
  assert.equal(modelsForRole(provider,'summary').length,2);
  assert.equal(supportsRole(provider,'audio'),false);
  assert.deepEqual(modelsForRole(provider,'audio'),[]);
  assert.equal(defaultProtocol({...provider,audio_protocols:['chat_audio']},'audio'),'chat_audio');
  assert.equal(supportsRole({source:'compatible',audio_protocols:['transcription']},'audio'),true);
});

test('settings uses an inline two-tab form and complete searchable provider catalog',()=>{
  const source=readFileSync(new URL('../src/management/ModelSettings.jsx',import.meta.url),'utf8');
  assert.match(source,/searchProviders\(providers,''\)/);
  assert.match(source,/选择供应商/);
  assert.match(source,/onChange=\{chooseProvider\}/);
  assert.match(source,/provider\.configurable===false/);
  assert.match(source,/服务商接入/);
  assert.match(source,/自定义接入/);
  assert.match(source,/role="tablist"/);
  assert.match(source,/role="tabpanel"/);
  assert.match(source,/ModelSelect/);
  assert.match(source,/保存供应商/);
  assert.doesNotMatch(source,/添加服务|<Dialog|provider:'newapi'/);
  assert.match(source,/selected\?\.source==='compatible'/);
  assert.doesNotMatch(source,/lg:grid-cols-3|aria-pressed=\{chosen\}/);
});

test('custom protocols expose matching request paths without doubling gateway prefixes',()=>{
  assert.equal(modelApiOptions.length,4);
  assert.equal(modelEndpoint('https://gateway.invalid','openai-completions'),'https://gateway.invalid/v1/chat/completions');
  assert.equal(modelEndpoint('https://gateway.invalid/api/v3','openai-responses'),'https://gateway.invalid/api/v3/responses');
  assert.equal(modelEndpoint('https://gateway.invalid/v1/messages','anthropic-messages'),'https://gateway.invalid/v1/messages');
  assert.equal(modelEndpoint('https://gateway.invalid/v1/chat/completions','openai-completions'),'https://gateway.invalid/v1/chat/completions');
  assert.equal(modelEndpoint('https://gateway.invalid','google-generative-ai'),'https://gateway.invalid/v1beta/models/{模型}:streamGenerateContent');
  assert.equal(modelEndpoint('not-a-url','openai-completions'),'');
});

test('non-audio custom protocols cannot be advertised as audio services',()=>{
  const provider={source:'compatible',audio_protocols:['transcription'],models:[]};
  assert.equal(serviceSupportsRole({api:'anthropic-messages'},provider,'audio'),false);
  assert.equal(serviceSupportsRole({api:'openai-responses'},provider,'audio'),false);
  assert.equal(serviceSupportsRole({},provider,'audio'),true);
  assert.equal(serviceSupportsRole({api:'google-generative-ai'},provider,'vision'),true);
});

test('searchable selectors provide selection, disabled reasons, dismissal and keyboard navigation',()=>{
  const source=readFileSync(new URL('../src/management/ModelSelect.jsx',import.meta.url),'utf8');
  for(const pattern of [/role="combobox"/,/role="listbox"/,/role="option"/,/aria-selected/,/aria-disabled/,/Escape/,/ArrowDown/,/ArrowUp/,/pointerdown/,/搜索/,/没有匹配/])assert.match(source,pattern);
});
