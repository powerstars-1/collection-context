import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {createRequire} from 'node:module';

const compiled=await build({stdin:{contents:"export {SourceResultCard} from './src/management/Sources.jsx';export {renderToStaticMarkup} from 'react-dom/server';export {createElement} from 'react';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
const module={exports:{}};
new Function('require','module','exports',compiled.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
const {SourceResultCard,renderToStaticMarkup,createElement}=module.exports;
const scope={kind:'saved',title:'我的收藏',account_matches:true,pending_count:0,timer:null,latest_job:{state:'succeeded'},latest_report:{committed_count:5},history_count:2};
const show=(value,props={})=>renderToStaticMarkup(createElement(SourceResultCard,{scope:value,canSync:true,canAutomate:true,...props}));

test('one source card owns its manual sync, independent timer and saved results',()=>{
  const html=show(scope);
  assert.match(html,/仅手动同步/);assert.match(html,/role="switch"[^>]*aria-checked="false"/);
  assert.match(html,/立即同步/);assert.match(html,/本轮 5 条已保存/);
  assert.match(html,/同步记录.*2/);assert.match(html,/移除来源/);assert.match(html,/查看内容/);
  assert.doesNotMatch(html,/type="checkbox"|只保存选择|再次同步/);
});

test('a running source keeps its real timer separate and cannot be edited or removed',()=>{
  const html=show({...scope,pending_count:1,timer:{enabled:true,interval_minutes:60},latest_job:{state:'running'}},{canSync:false});
  assert.match(html,/每小时自动同步/);assert.match(html,/role="switch"[^>]*aria-checked="true"/);
  assert.match(html,/disabled=""[^>]*>.*?正在同步/);
  assert.match(html,/disabled=""[^>]*>.*?同步设置/);assert.match(html,/disabled=""[^>]*>.*?移除来源/);
});

test('login loss prevents starting a timer but still permits stopping an existing one',()=>{
  const off=show(scope,{canAutomate:false,canSync:false});
  assert.match(off,/role="switch"[^>]*disabled=""/);
  const on=show({...scope,timer:{enabled:true,interval_minutes:60}},{canAutomate:false,canSync:false});
  assert.doesNotMatch(on,/role="switch"[^>]*disabled=""/);
});
