import test from 'node:test';
import assert from 'node:assert/strict';
import {ensureSource,canSyncSource,syncAllSources} from '../src/management/sourceActions.mjs';
const scope={kind:'saved',config_id:'y_old',scope_id:'s_saved',limit:5,download:true,account_matches:true};
test('existing source with unchanged options is reused for the next sync',async()=>{
  const value=await ensureSource(()=>assert.fail('must not create source again'),'saved',[scope],{version:'v_one'},{limit:5,download:true});
  assert.equal(value,scope);
});
test('refresh without changing options preserves per-source settings',async()=>{
  const saved={...scope,limit:10,download:false};
  const value=await ensureSource(()=>assert.fail('refresh must not overwrite source options'),'saved',[saved],{version:'v_one'},{limit:5,download:true},true);
  assert.equal(value.limit,10);assert.equal(value.download,false);
});
test('changed options update the source and use its new immutable config',async()=>{
  const seen=[];
  const value=await ensureSource(async(path,args)=>{seen.push([path,args]);return {updated_source:{...scope,config_id:'y_new',limit:10}}},'saved',[scope],{version:'v_one'},{limit:10,download:true});
  assert.equal(seen[0][0],'/v1/management/source-edit');assert.equal(value.config_id,'y_new');
});
test('new folder is created once; scopes from another account are not reused',async()=>{
  const seen=[];const api=async(path,args)=>{seen.push([path,args]);return {config_id:'y_new'}};
  await ensureSource(api,'folder:folder_1',[],{version:'v_one'},{limit:5,download:true});
  assert.equal(seen[0][1].collection_id,'folder_1');
  await ensureSource(api,'saved',[{...scope,account_matches:false}],{version:'v_two'},{limit:5,download:true});
  assert.equal(seen[1][0],'/v1/management/self-source-create');
});

test('sync all reads every page, skips active/old-account scopes and continues after one submission fails',async()=>{
  const calls=[];
  const api=async(path,args)=>{
    calls.push([path,args]);
    if(path.endsWith('/sources'))return {worker:{online:true},scopes:args.offset===0?
      [{...scope,config_id:'first'},{...scope,config_id:'running',pending_count:1},{...scope,config_id:'old',account_matches:false}]:
      [{...scope,config_id:'fails'},{...scope,config_id:'last'}],next_offset:args.offset===0?20:null};
    if(args.config_id==='fails')throw new Error('来源暂不可用');
    return {job_id:'j_'+args.config_id};
  };
  const result=await syncAllSources(api,true);
  assert.deepEqual(calls.filter(([p])=>p.endsWith('/sources')).map(([,v])=>v.offset),[0,20]);
  assert.deepEqual(calls.filter(([p])=>p.endsWith('/source-submit')).map(([,v])=>v.config_id),['first','fails','last']);
  assert.equal(result.queued,2);assert.equal(result.skipped,2);assert.equal(result.failed.length,1);
  assert.equal(new Set(calls.filter(([p])=>p.endsWith('/source-submit')).map(([,v])=>v.idempotency_key)).size,3);
});

test('public creator sources can sync without private login; private and pending sources cannot',()=>{
  assert.equal(canSyncSource(scope,false),false);
  assert.equal(canSyncSource({...scope,kind:'creator'},false),true);
  assert.equal(canSyncSource({...scope,latest_job:{state:'queued'}},true),false);
});

test('an offline worker or broken pagination never submits a partial batch',async()=>{
  await assert.rejects(syncAllSources(async path=>{assert.ok(path.endsWith('/sources'));return {worker:{online:false},scopes:[]};},true),/后台未运行/);
  await assert.rejects(syncAllSources(async path=>{assert.ok(path.endsWith('/sources'));return {worker:{online:true},scopes:[scope],next_offset:0};},true),/来源分页异常/);
});
