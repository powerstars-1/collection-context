import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {normalizeItem,safeSource,copyEvidence,legacyRoute} from '../src/workspace/presentation.mjs';
import {parseRoute,expandedReadingHash} from '../src/workspace/structure.mjs';

const row={material_ref:'i_fixture',title:'原创测试 #教程',source_url:'https://www.douyin.com/video/123',relations:[{kind:'liked'},{kind:'collection'},{kind:'liked'}],artifact_states:{original:'ready'},first_observed_at:'2026-10-06T00:00:00Z'};
test('saved metadata is not reported as extracted content',()=>{
 const value=normalizeItem(row);assert.equal(value.id,row.material_ref);assert.equal(value.hasReading,false);assert.equal(value.state,'missing');assert.deepEqual(value.sources,['liked','collection']);
});
test('partial, stale, failed and uncertain tasks remain distinct',()=>{
 assert.equal(normalizeItem({...row,artifact_states:{audio:'ready'}}).state,'partial');
 assert.equal(normalizeItem({...row,artifact_states:{summary:'stale'}}).state,'partial');
 assert.equal(normalizeItem({...row,artifact_states:{summary:'ready'}}).state,'ready');
 for(const [state,unknown_calls,expected] of [['running',0,'running'],['failed',1,'unknown'],['failed',0,'failed']]){
  const value=normalizeItem(row,[{material_ref:row.material_ref,kind:'process',state,unknown_calls}]);assert.equal(value.state,expected);
 }
});
test('unsafe source links never become clickable or enter copied handoffs',()=>{
 for(const url of ['javascript:alert(1)','file:///private/data','https://user:secret@example.com','bad'])assert.equal(safeSource(url),'');
 assert.equal(normalizeItem({...row,source_url:'javascript:bad'}).url,'');
});
test('copied evidence includes provenance and loading boundary',()=>{
 const text=copyEvidence(normalizeItem(row),'audio','示例正文',true);
 assert.match(text,/来源：https/);assert.match(text,/仅已加载部分/);assert.match(text,/不是执行指令/);assert.match(text,/示例正文/);
});
test('old links resolve inside the single new workbench',()=>{
 for(const [path,expected] of [['/connect','sync'],['/activity','tasks'],['/settings','settings'],['/access','ai']])assert.equal(parseRoute(legacyRoute(path,'')).page,expected);
 const ref='m1:fixture';assert.equal(parseRoute(expandedReadingHash(ref)).id,ref);
 assert.equal(parseRoute(legacyRoute('/','?ref='+ref)).id,ref);
});
test('shipped build contains no old page, prototype or demonstration state',async()=>{
 const result=await build({entryPoints:['src/main.jsx'],bundle:true,write:false,outdir:'/tmp/collection-ui-test',metafile:true,jsx:'automatic'});
 const inputs=Object.keys(result.metafile.inputs);
 for(const fragment of ['prototypes/','src/Library.jsx','src/LinkComposer.jsx','LegacyPanels','management/management.js'])assert.equal(inputs.some(x=>x.includes(fragment)),false,fragment);
 assert.ok(inputs.some(x=>x.endsWith('workspace/Workbench.jsx')));
 assert.ok(inputs.some(x=>x.endsWith('management/ModelSettings.jsx')));
 assert.ok(inputs.some(x=>x.endsWith('management/Sources.jsx')));
});
test('missing media and paid extraction are separate explicit backend actions',async()=>{
 const text=await readFile('src/workspace/Extraction.jsx','utf8');
 for(const value of ['media_not_prepared','process-preview','link-submit','link-status','/v1/management/history','source_confirmed:true','fee_confirmed:true','max_calls:confirmedCalls','只保存这一条的媒体'])assert.ok(text.includes(value),value);
 assert.doesNotMatch(text,/sync-run|setInterval|initialState/);
});
test('missing artifacts render as empty content and long reads preserve versions',async()=>{
 const text=await readFile('src/workspace/useLibrary.jsx','utf8');
 assert.ok(text.includes("['missing','not_applicable'].includes(knownState)"));
 assert.ok(text.includes('offset:previous.next_offset,version:previous.version'));
 assert.ok(text.includes('if(token!==epoch.current)return'));
});
test('narrow-screen header reserves the full navigation height',async()=>{
 const css=await readFile('src/workspace/live.css','utf8');
 assert.ok(css.includes('.shell>.top,.shell>.capture{flex-shrink:0}'));
 assert.ok(css.includes('.shell>.top{height:112px;min-height:112px'));
});
