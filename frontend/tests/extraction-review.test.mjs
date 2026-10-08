import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {normalizeItem,readingArtifact} from '../src/workspace/presentation.mjs';

const row={material_ref:'i_example',title:'图文测试',media_type:'image',artifacts:{summary:{state:'ready'},screen:{state:'ready'},image:{state:'missing'}}};
test('image posts read the actual published screen artifact; legacy image text remains available',()=>{
  assert.equal(readingArtifact(normalizeItem(row),'screen'),'screen');
  assert.equal(readingArtifact(normalizeItem({...row,artifacts:{image:{state:'ready'}}}),'screen'),'image');
  assert.equal(readingArtifact(normalizeItem(row),'audio'),'audio');
});
test('a readable summary does not hide failed or partial processing',()=>{
  for(const state of ['partial','failed','blocked','cancelled']){
    const item=normalizeItem(row,[{kind:'process',material_ref:row.material_ref,state,can_retry:true}]);
    assert.equal(item.state,'partial');assert.equal(item.hasReading,true);assert.match(item.stateLabel,/待完成/);
  }
  const item=normalizeItem({...row,artifacts:{summary:{state:'ready',coverage:{processing_partial:true}}}});
  assert.equal(item.state,'partial');
  assert.equal(normalizeItem(row,[{kind:'process',material_ref:row.material_ref,state:'running'}]).stateLabel,'处理中');
});
test('preparation warnings and existing tasks are shown before a fresh paid submission',async()=>{
  const text=await readFile('src/workspace/Extraction.jsx','utf8');
  for(const part of ['preparation_issues','preparation_error','latest_job','needsTask','查看已有任务','没有音轨','onNotice?.'])assert.ok(text.includes(part),part);
});
test('audio-only and full local preparation have separate explicit actions',async()=>{
  const text=await readFile('src/workspace/Extraction.jsx','utf8');
  for(const part of ['media-prepare',"prepare('audio')","prepare('full')",'只转写音频','重新准备画面','selection_reduced'])assert.ok(text.includes(part),part);
});
test('retry preview failures end the loading state inside the dialog',async()=>{
  const text=await readFile('src/management/ContentTaskDetail.jsx','utf8');
  assert.ok(text.includes('setError(err.message)'));
  assert.ok(text.includes('{error||action.error}'));
  assert.ok(text.includes('!error&&!plan'));
  assert.ok(text.includes('plan&&'));
});
const compiled=await build({stdin:{contents:"export {TaskStages} from './src/management/Tasks.jsx';export {renderToStaticMarkup} from 'react-dom/server';export {createElement} from 'react';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
const module={exports:{}};new Function('require','module','exports',compiled.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
test('task details render per-stage errors and distinguish no audio from failure',()=>{
  const {TaskStages,renderToStaticMarkup,createElement}=module.exports;
  const html=renderToStaticMarkup(createElement(TaskStages,{job:{stages:{audio_not_applicable:'not_applicable',screen_f_000001:'failed'},stage_details:{screen_f_000001:{error_message:'模型服务拒绝请求'}}}}));
  assert.match(html,/无音轨/);assert.match(html,/无需处理/);assert.match(html,/000001/);assert.match(html,/模型服务拒绝请求/);
});
