import test from 'node:test';
import assert from 'node:assert/strict';
import {contentTasks,contentSteps,loadTaskOverview,stepRole} from '../src/management/contentTasks.mjs';
import {parseRoute} from '../src/workspace/structure.mjs';

const item={material_ref:'i_one',title:'教程',media_type:'video',artifact_states:{audio:'ready'},first_observed_at:'2026-10-05T00:00:00Z'};
const prepared={material_ref:'i_one',input_id:'u_one',audio_segments:2,visual_frames:3,has_audio:true};
const job=(id,kind,state,created_at,extra={})=>({job_id:id,material_ref:'i_one',title:'教程',kind,state,created_at,...extra});

test('save, prepare, extract and retries stay in one content row',()=>{
  const jobs=[job('j_add','add','succeeded','2026-10-05T01:00:00Z',{link_result:{download:'ready'}}),job('j_old','process','failed','2026-10-05T02:00:00Z'),job('j_retry','process','running','2026-10-05T03:00:00Z')];
  const result=contentTasks(jobs,[prepared],[item]);
  assert.equal(result.contents.length,1);assert.equal(result.contents[0].jobs.length,3);assert.equal(result.contents[0].state,'active');
});
test('failed link attempts coalesce before and after metadata is saved',()=>{
  const url='https://www.douyin.com/video/718';
  const attempts=[{job_id:'j_a',kind:'add',state:'failed',title:'保存链接',link_url:url,created_at:'2026-10-05T01:00:00Z'},{job_id:'j_b',kind:'add',state:'queued',title:'保存链接',link_url:url,created_at:'2026-10-05T02:00:00Z'}];
  assert.equal(contentTasks(attempts,[],[]).contents.length,1);
  const resolved=contentTasks([...attempts,{...attempts[1],job_id:'j_c',material_ref:'i_one',state:'succeeded'}],[],[{...item,source_url:url}]);
  assert.equal(resolved.contents.length,1);assert.equal(resolved.contents[0].jobs.length,3);
});
test('successful transcript is visible while screen and summary failed',()=>{
  const failed=job('j_process','process','partial','2026-10-05T02:00:00Z',{can_retry:true,stages:{audio_a_000000:'ready',audio_a_000001:'ready',screen_f_000001:'failed',summary:'failed',publish:'partial'},stage_details:{summary:{error_message:'模型组件未就绪'}}});
  const task=contentTasks([failed],[prepared],[item]).contents[0],steps=contentSteps(task);
  assert.equal(steps.find(s=>s.id==='audio').state,'ready');assert.equal(steps.find(s=>s.id==='audio').completed,2);
  assert.equal(steps.find(s=>s.id==='vision').state,'failed');assert.equal(steps.find(s=>s.id==='summary').state,'failed');
  assert.equal(task.state,'attention');assert.equal(steps[1].state,'ready');
});
test('cached media without prepared input asks for preparation, not download',()=>{
  const task=contentTasks([],[],[item]).contents[0];
  const steps=contentSteps(task,{media_saved:true,input_id:null,preparation_issues:[]});
  assert.equal(steps.find(s=>s.id==='media').state,'ready');assert.equal(steps.find(s=>s.id==='prepare_audio').state,'pending');assert.equal(steps.find(s=>s.id==='prepare_vision').state,'pending');
});
test('audio-only task skips vision and does not offer its failure as a current issue',()=>{
  const task=contentTasks([],[],[item]).contents[0];
  const steps=contentSteps(task,{...prepared,visual_frames:0,audio_only:true,preparation_issues:[{role:'vision',code:'vision_deferred'}]});
  assert.equal(steps[2].state,'ready');assert.equal(steps.find(s=>s.id==='vision').state,'not_applicable');
});
test('newly prepared input does not carry old failed extraction progress',()=>{
  const old=job('j_old','process','partial','2026-10-05T01:00:00Z',{stages:{summary:'failed'}}),fresh=job('j_new','add','succeeded','2026-10-05T03:00:00Z',{link_result:{download:'ready'}});
  const task=contentTasks([old,fresh],[prepared],[item]).contents[0];
  assert.equal(task.steps.find(s=>s.id==='summary').state,'pending');
});
test('job pagination crosses the first twenty records without dropping prepared rows',async()=>{
  const offsets=[];
  const result=await loadTaskOverview(async(_,body)=>{
    const offset=body.offset||0;offsets.push(offset);
    return {jobs:[{job_id:'j_'+offset}],prepared:[{material_ref:'i_'+offset}],next_offset:offset===0?40:null,next_prepared_offset:offset===0?20:offset===20?40:null};
  });
  assert.deepEqual(offsets,[0,20,40]);assert.equal(result.jobs.length,3);assert.equal(result.prepared.length,3);
});
test('content task links open their own detail and results open the matching tab',()=>{
  assert.equal(parseRoute('#tasks/i_one').id,'i_one');assert.equal(parseRoute('#library/i_one/audio').tab,'audio');
  assert.equal(stepRole('screen_f_00123'),'vision');assert.equal(stepRole('audio_a_00123'),'audio');
});
test('queued preparation has clocks inside and outside; only actual running stages spin',()=>{
  const queued=job('j_prepare','add','queued','2026-10-05T03:00:00Z',{preparation_mode:'full',stages:{}});
  const waiting=contentTasks([queued],[prepared],[item]).contents[0];
  assert.equal(waiting.state,'active');assert.equal(waiting.running,false);
  assert.equal(waiting.steps.find(s=>s.id==='prepare_audio').state,'queued');
  const running={...queued,state:'running',stages:{prepare_audio:'ready',prepare_vision:'running'},stage_details:{prepare_audio:{progress:{count:2,reused:true}},prepare_vision:{progress:{phase:'ocr',done:10,total:23}}}};
  const working=contentTasks([running],[prepared],[item]).contents[0];
  assert.equal(working.running,true);assert.equal(working.steps.find(s=>s.id==='prepare_vision').state,'running');
  assert.match(working.steps.find(s=>s.id==='prepare_vision').description,/10 \/ 23/);
});
test('audio-only branch retains earlier visual results as available',()=>{
  const task=contentTasks([],[],[{...item,artifact_states:{audio:'ready',screen:'ready'}}]).contents[0];
  const steps=contentSteps(task,{...prepared,audio_only:true},task.item,'audio');
  assert.equal(steps.find(s=>s.id==='prepare_vision').state,'not_applicable');
  assert.equal(steps.find(s=>s.id==='vision').available,true);
});
