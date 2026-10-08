// Product-facing tasks are contents. Execution attempts stay inside their task.
export const stepDefinitions=[['original','保存作品'],['media','保存原媒体'],['prepare_audio','准备音频'],['prepare_vision','准备画面'],['audio','音频转写'],['vision','画面提取'],['summary','内容总结'],['publish','保存笔记']];
export const taskStateNames={ready:'已完成',partial:'部分完成',failed:'需重试',blocked:'待处理',pending:'尚未开始',running:'处理中',queued:'等待处理',not_applicable:'跳过'};
const active=job=>['queued','running'].includes(job?.state);
const completed=value=>['ready','reused','completed','not_applicable'].includes(value);
export function stepRole(name){return name==='audio_not_applicable'?'audio':name.startsWith('audio_')?'audio':name.startsWith('screen_')||name.startsWith('vision_')?'vision':name==='summary'?'summary':name==='publish'?'publish':null;}
const sortJobs=jobs=>[...jobs].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')||b.job_id.localeCompare(a.job_id));
export function contentTasks(jobs,prepared,items){
  const map=new Map(items.map(item=>[item.material_ref,{id:item.material_ref,material_ref:item.material_ref,title:item.title,item,jobs:[],prepared:prepared.find(p=>p.material_ref===item.material_ref)}]));
  for(const item of prepared)if(!map.has(item.material_ref))map.set(item.material_ref,{id:item.material_ref,material_ref:item.material_ref,title:item.title,item:null,jobs:[],prepared:item});
  const byUrl=new Map(items.filter(item=>item.source_url).map(item=>[item.source_url,item.material_ref]));
  for(const job of jobs)if(job.link_url&&job.material_ref)byUrl.set(job.link_url,job.material_ref);
  const other=[];
  for(const job of sortJobs(jobs)){
    const ref=job.material_ref||byUrl.get(job.link_url);
    if(!ref){if(job.kind==='add'){const id=job.link_url?'link:'+job.link_url:job.job_id;if(!map.has(id))map.set(id,{id,title:job.title,jobs:[],item:job.link_url?{source_url:job.link_url}:null});map.get(id).jobs.push(job);}else other.push(job);continue;}
    if(!map.has(ref))map.set(ref,{id:ref,material_ref:ref,title:job.title,item:null,jobs:[],prepared:prepared.find(p=>p.material_ref===ref)});
    map.get(ref).jobs.push(job);
  }
  const contents=[...map.values()].map(task=>{
    task.jobs=sortJobs(task.jobs);task.latest=task.jobs.find(job=>job.state==='running')||task.jobs.find(active)||task.jobs[0];
    task.updated_at=task.latest?.created_at||task.item?.first_observed_at||'';
    const steps=contentSteps(task),issue=steps.find(s=>['failed','blocked'].includes(s.state));
    const running=steps.find(s=>['running','queued'].includes(s.state));
    const summary=steps.find(s=>s.id==='summary'),publish=steps.find(s=>s.id==='publish');
    task.steps=steps;task.state=running?'active':issue?'attention':publish?.state==='ready'&&summary?.state==='ready'?'done':'pending';
    task.running=steps.some(step=>step.state==='running');
    task.stateLabel=running?`${running.label}${running.state==='queued'?'等待处理':'中'}`:issue?`${issue.label}需处理`:task.state==='done'?'笔记已生成':summary?.state==='partial'?'部分笔记可读':'待生成笔记';
    task.progress=steps.filter(s=>s.state==='ready'||s.state==='not_applicable').length;
    return task;
  }).sort((a,b)=>b.updated_at.localeCompare(a.updated_at)||a.id.localeCompare(b.id));
  return {contents,other};
}
export function contentSteps(task,preview=task.prepared,status=task.item,mode){
  const latest=task.latest||task.jobs.find(active)||task.jobs[0];
  const process=task.jobs.find(j=>j.kind==='process'&&(!j.input_id||!status?.prepared_input||j.input_id===status.prepared_input))||preview?.latest_job;
  // A newer media preparation supersedes the previous extraction input.
  const useProcess=process&&!(latest?.kind==='add'&&!active(process)&&latest.created_at>process.created_at)?process:null;
  const artifacts=status?.artifacts||Object.fromEntries(Object.entries(status?.artifact_states||{}).map(([k,state])=>[k,{state}]));
  const mediaSaved=preview?.media_saved??status?.media_saved??Boolean(preview?.input_id||task.prepared||task.jobs.some(j=>j.link_result?.download==='ready'));
  const preparation=task.jobs.find(job=>job.kind==='add'&&active(job))||(active(preview?.media_job)?preview.media_job:null)||task.jobs.find(job=>job.kind==='add'&&job.preparation_mode);
  const preparing=active(preparation);
  const audioOnly=(mode||(preparation?.preparation_mode|| (preview?.audio_only?'audio':'full')))==='audio';
  const issues=preview?.preparation_issues?.filter(i=>i.code!=='vision_deferred')||[];
  const rows=[['original','保存作品'],['media','保存原媒体'],['prepare','准备媒体'],['audio','音频转写'],['vision','画面提取'],['summary','内容总结'],['publish','保存笔记']].map(([id,label])=>({id,label,state:'pending',completed:0,total:0,errors:[],retryStages:[]}));
  const row=id=>rows.find(r=>r.id===id);
  row('original').state=task.material_ref?'ready':active(latest)?latest.state:'failed';
  row('media').state=mediaSaved?'ready':preparing?latest.state:latest?.kind==='add'&&['failed','partial','blocked'].includes(latest.state)?'failed':'pending';
  row('prepare').state=preparing&&mediaSaved?latest.state:issues.length||preview?.preparation_error?'blocked':preview?.input_id||task.prepared?'ready':'pending';
  if(issues.length)row('prepare').errors=issues.map(i=>`${i.role==='audio'?'音频':'画面'}准备未完成（${i.code}）`);
  if(preparing&&!mediaSaved)row('prepare').state='pending';
  if(latest?.kind==='add'&&latest.error_message){const target=row(!task.material_ref?'original':mediaSaved?'prepare':'media');target.state='failed';target.errors=[latest.error_message];}
  if(!task.material_ref)row('media').state='pending';
  if(preview?.input_id){row('prepare').description=`${preview.audio_segments||0} 段音频 · ${preview.visual_frames||0} 张画面`;
    if(preview.selection_reduced)row('prepare').description+=' · 代表帧覆盖';}
  for(const id of ['audio','vision','summary','publish']){
    const target=row(id), entries=Object.entries(useProcess?.stages||{}).filter(([name])=>stepRole(name)===id&&!name.endsWith('_preparation_failed'));
    const expected=id==='audio'?preview?.audio_segments:id==='vision'?preview?.visual_frames:1;
    target.total=expected??entries.length;
    target.completed=entries.filter(([,state])=>completed(state)).length;
    target.retryStages=entries.filter(([,state])=>!completed(state)).map(([name])=>name);
    target.errors=[...new Set(entries.map(([name])=>useProcess?.stage_details?.[name]?.error_message).filter(Boolean))];
    const artifact=id==='publish'?['summary','audio','screen','image'].map(kind=>artifacts[kind]).find(value=>['ready','stale'].includes(value?.state)):artifacts[{audio:'audio',vision:'screen',summary:'summary'}[id]];
    target.available=['ready','stale'].includes(artifact?.state);
    target.reused=entries.filter(([name])=>useProcess?.stage_details?.[name]?.reused).length;
    if(id==='audio'&&(preview?.has_audio===false||status?.media_type==='image')||id==='vision'&&audioOnly){target.state='not_applicable';target.description=id==='audio'?'这条内容没有音轨':`本次不处理画面${preview?.visual_frames?`，已有 ${preview.visual_frames} 张画面保留`:''}`;continue;}
    if(entries.some(([,s])=>s==='running'))target.state='running';
    else if(entries.some(([,s])=>['failed','blocked','unknown'].includes(s)))target.state='failed';
    else if(entries.some(([,s])=>s==='partial'))target.state='partial';
    else if(entries.length&&target.completed>=target.total&&target.total>0)target.state='ready';
    else if(useProcess?.state==='queued')target.state='queued';
    else if(entries.length&&active(useProcess))target.state='pending';
    else if(artifact?.state==='ready')target.state=artifact.coverage?.processing_partial?'partial':'ready';
    if(useProcess&&['failed','partial','cancelled','blocked'].includes(useProcess.state)&&target.state==='pending')target.state='blocked';
    if(issues.some(i=>i.role===id)&&!entries.length){target.state='blocked';target.description='先完成上方的媒体准备';}
    const reusable=id==='audio'?preview?.reusable_audio_segments:id==='vision'?preview?.reusable_visual_frames:0;
    if(reusable&&reusable>=target.total&&!entries.length){target.state='ready';target.completed=reusable;target.reused=reusable;target.description='已有有效结果，本次直接复用';}
    else if(target.available&&!['ready','partial'].includes(target.state))target.description='之前的结果仍可阅读，本次进度会单独更新';
    target.job=useProcess;
  }
  if(active(useProcess)&&!preparing&&!rows.some(row=>row.state==='running')){
    const waiting=rows.find(row=>['audio','vision','summary','publish'].includes(row.id)&&row.state==='pending');
    if(waiting){waiting.state='queued';waiting.description='等待后台执行这一步';}
  }
  const preparers=['audio','vision'].map(role=>{
    const name='prepare_'+role,detail=preparation?.stage_details?.[name],state=preparation?.stages?.[name];
    const available=role==='audio'?(preview?.audio_segments>0||preview?.has_audio===false):preview?.visual_frames>0;
    const failure=issues.find(issue=>issue.role===role);
    const value={id:name,label:role==='audio'?'准备音频':'准备画面',state:available?'ready':failure?'failed':'pending',available,total:0,completed:0,errors:failure?[`${role==='audio'?'音频':'画面'}准备未完成（${failure.code}）`]:[],retryStages:[],reused:0};
    if(role==='audio'&&(preview?.has_audio===false||status?.media_type==='image')||role==='vision'&&audioOnly){value.state='not_applicable';value.description=available?'已有输入保留，本次不处理':'本次不处理';return value;}
    if(preparing||state){value.state=state==='cancelled'?'blocked':state|| (preparation.state==='queued'?'queued':role==='audio'?'running':'pending');value.errors=detail?.error_message?[detail.error_message]:[];value.reused=detail?.progress?.reused?1:0;}
    const progress=detail?.progress;
    if(state==='cancelled')value.description='本次已停止，已有输入保留';
    else if(value.state==='running'&&progress?.phase==='ocr')value.description=`画面筛选 ${progress.done||0} / ${progress.total||0}`;
    else if(value.state==='running'&&progress?.phase==='scan')value.description='正在扫描视频画面';
    else if(progress?.count!=null)value.description=`${progress.count} ${role==='audio'?'段音频':'张画面'}${progress.reused?' · 已复用':''}`;
    else if(available)value.description=`${role==='audio'?preview.audio_segments:preview.visual_frames} ${role==='audio'?'段音频':'张画面'}${preparing?' · 已有输入保留':''}`;
    return value;
  });
  return [rows[0],rows[1],...preparers,...rows.filter(row=>['audio','vision','summary','publish'].includes(row.id))];
}

export async function loadTaskOverview(api,signal){
  const first=await api('/v1/management/overview',{}, {signal});
  const jobs=[...first.jobs],prepared=[...first.prepared];
  let jobOffset=first.next_offset,preparedOffset=first.next_prepared_offset;
  while(jobOffset!=null||preparedOffset!=null){
    const offset=Math.min(jobOffset??Infinity,preparedOffset??Infinity),more=await api('/v1/management/overview',{offset},{signal});
    for(const job of more.jobs)if(!jobs.some(j=>j.job_id===job.job_id))jobs.push(job);
    for(const item of more.prepared)if(!prepared.some(p=>p.material_ref===item.material_ref))prepared.push(item);
    if(jobOffset!=null&&offset>=jobOffset)jobOffset=more.next_offset;
    if(preparedOffset!=null&&offset>=preparedOffset)preparedOffset=more.next_prepared_offset;
  }
  return {...first,jobs,prepared};
}
export async function loadContentTaskData(api,signal){
  const [overview,list]=await Promise.all([loadTaskOverview(api,signal),api('/v1/collections/list',{limit:20},{signal})]);
  return {...overview,items:list.items,itemNext:list.next_offset,itemVersion:list.version};
}
