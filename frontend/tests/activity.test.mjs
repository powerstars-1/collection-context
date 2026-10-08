import test from 'node:test';
import assert from 'node:assert/strict';
import {createActivityMonitor,activityDelay,mergeLiveJobs} from '../src/lib/activity.mjs';
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
function fixture(api){
  let time=100000,id=0;const timers=new Map(),listeners=new Map();
  const doc={hidden:false,addEventListener:(k,fn)=>listeners.set(k,fn),removeEventListener:k=>listeners.delete(k)};
  const options={document:doc,events:null,channel:null,locks:null,now:()=>time,schedule:(fn,ms)=>{timers.set(++id,{fn,at:time+ms});return id},cancel:id=>timers.delete(id)};
  return {monitor:createActivityMonitor(api,options),doc,timers,async advance(ms){const end=time+ms;for(;;){const entry=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];if(!entry||entry[1].at>end)break;time=entry[1].at;timers.delete(entry[0]);entry[1].fn();await flush()}time=end},visibility(hidden){doc.hidden=hidden;listeners.get('visibilitychange')?.()}};
}
test('idle has one shared check per 30 seconds, not one loop per component',async()=>{
  let calls=0;const f=fixture(async()=>{calls++;return {jobs:[],active_count:0}});
  const a=f.monitor.subscribe(()=>{}),b=f.monitor.subscribe(()=>{});await flush();assert.equal(calls,1);
  await f.advance(60000);assert.equal(calls,3);a();b();assert.equal(f.timers.size,0);f.monitor.dispose();
});
test('running uses 5 seconds, completion returns to idle and updates all consumers',async()=>{
  let calls=0,running=true;const seen=[];const f=fixture(async()=>{calls++;return {jobs:[],active_count:running?1:0}});
  f.monitor.subscribe(v=>seen.push(v.data));await flush();await f.advance(15000);assert.equal(calls,4);
  running=false;await f.advance(5000);assert.equal(calls,5);assert.equal(seen.at(-1).active_count,0);
  await f.advance(29000);assert.equal(calls,5);f.monitor.dispose();
});
test('hidden tabs perform no background polling and resume on visibility',async()=>{
  let calls=0;const f=fixture(async()=>{calls++;return {jobs:[],active_count:1}});f.monitor.subscribe(()=>{});await flush();
  f.visibility(true);await f.advance(120000);assert.equal(calls,1);
  f.visibility(false);await flush();assert.equal(calls,2);f.monitor.dispose();
});
test('errors back off for a minute rather than repeatedly hitting the limiter',async()=>{
  let calls=0;const f=fixture(async()=>{calls++;throw new Error('rate_limited')});f.monitor.subscribe(()=>{});await flush();
  await f.advance(59000);assert.equal(calls,1);await f.advance(1000);assert.equal(calls,2);f.monitor.dispose();
});
test('inflight refresh is coalesced and no new writes are issued',async()=>{
  let resolve,calls=0;const f=fixture(async(path,body)=>{assert.equal(path,'/v1/management/activity');assert.deepEqual(body,{});calls++;return await new Promise(r=>resolve=r)});
  f.monitor.subscribe(()=>{});f.monitor.subscribe(()=>{});f.monitor.refresh();assert.equal(calls,1);
  resolve({jobs:[],active_count:0});await flush();await f.advance(150);assert.equal(calls,2);resolve({jobs:[],active_count:0});await flush();f.monitor.dispose();
});
test('live updates keep history and refuse stale progress',()=>{
  const old=[{job_id:'old',state:'succeeded'},{job_id:'live',updated_at:'2026-10-06T01:02',state:'succeeded'}];
  assert.equal(mergeLiveJobs(old,[{job_id:'live',updated_at:'2026-10-06T01:01',state:'running'}]).find(x=>x.job_id==='live').state,'succeeded');
  assert.equal(mergeLiveJobs(old,[{job_id:'new',state:'queued'}]).length,3);
  assert.equal(activityDelay({connection_active:true}),5000);
});
test('two visible tabs share an activity request through lock and ephemeral broadcast',async()=>{
  let queue=Promise.resolve(),calls=0;const channels=[];
  const locks={request(_,read){const result=queue.then(read);queue=result.catch(()=>{});return result}};
  function channel(){const c={postMessage(data){for(const other of channels)if(other!==c)other.onmessage?.({data})},close(){}};channels.push(c);return c}
  const common={document:{hidden:false},events:null,locks,now:()=>100000,schedule:()=>1,cancel:()=>{}};
  const api=async()=>{calls++;return {jobs:[],active_count:0}};
  const a=createActivityMonitor(api,{...common,channel:channel()}),b=createActivityMonitor(api,{...common,channel:channel()});
  let left,right;a.subscribe(s=>left=s);b.subscribe(s=>right=s);await flush();
  assert.equal(calls,1);assert.deepEqual(left.data,right.data);a.dispose();b.dispose();
});
test('pages no longer continuously paginate history or poll idle account state',async()=>{
  const {readFile}=await import('node:fs/promises');
  const source=await readFile(new URL('../src/management/Sources.jsx',import.meta.url),'utf8');
  const library=await readFile(new URL('../src/workspace/useLibrary.jsx',import.meta.url),'utf8');
  const task=await readFile(new URL('../src/management/Tasks.jsx',import.meta.url),'utf8');
  assert.doesNotMatch(library,/loadTaskOverview|setTimeout\(poll/);
  assert.doesNotMatch(task,/setTimeout\(load/);
  assert.match(source,/connection-run'.*poll:value=>value\?\.active\?3000:false/);
  assert.doesNotMatch(source,/connection',\{\}, \{poll:true/);
});
