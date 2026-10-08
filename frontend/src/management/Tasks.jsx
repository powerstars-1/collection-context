import {useEffect,useRef,useState} from 'react';
import {ChevronRight,CheckCircle2,Clock,AlertCircle,LoaderCircle} from 'lucide-react';
import {button,primary,Page,Notice,Dialog,useAction} from '../ui';
import {navigateTo,cloudPlan} from '../lib/navigation';
import {displayTitle} from '../lib/library-presentation';
import {contentTasks,loadContentTaskData,taskStateNames,stepRole} from './contentTasks.mjs';
import {ContentTaskDetail,RetryStepDialog} from './ContentTaskDetail';
import {Extraction} from '../workspace/Extraction';
import {parseRoute} from '../workspace/structure.mjs';
import {useActivity} from '../lib/useActivity';
import {mergeLiveJobs} from '../lib/activity.mjs';

// Execution-level diagnostics are retained for history, not the task list.
export function TaskStages({job}){
  return <div>{Object.entries(job.stages||{}).map(([name,state])=><div key={name}><p>{name==='audio_not_applicable'?'音频（无音轨）':({audio:'音频转写',vision:'画面识别',summary:'内容总结',publish:'保存内容'})[stepRole(name)]||name}{name.match(/_[af]_\d+$/)?' · '+name.split('_').at(-1):''} · {state==='not_applicable'?'无需处理':taskStateNames[state]||state}</p>{job.stage_details?.[name]?.error_message&&<p>{job.stage_details[name].error_message}</p>}</div>)}</div>;
}

export function Tasks({api,initialRef='',onJobs}){
  const routeRef=initialRef||(typeof location==='undefined'?'':parseRoute(location.hash).id);
  const [data,setData]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[revision,setRevision]=useState(0);
  const [focusItem,setFocusItem]=useState(null);
  const [filter,setFilter]=useState('all'),[detail,setDetail]=useState(initialRef),[retry,setRetry]=useState(null),[extract,setExtract]=useState(null),[batch,setBatch]=useState(false),[selected,setSelected]=useState([]);
  const action=useAction(),key=useRef(crypto.randomUUID());
  const activity=useActivity(api),completion=useRef(null);
  const refresh=()=>setRevision(n=>n+1);
  useEffect(()=>{if(data?.jobs)onJobs?.(data.jobs)},[data?.jobs,onJobs]);
  function updateJob(job,ref,title){setData(old=>{if(!old)return old;const previous=old.jobs.find(value=>value.job_id===job.job_id);if(previous?.updated_at&&job.updated_at&&previous.updated_at>job.updated_at)return old;return {...old,jobs:[{...job,material_ref:ref,title},...old.jobs.filter(previous=>previous.job_id!==job.job_id)]}})}
  useEffect(()=>{if(routeRef)setDetail(routeRef)},[routeRef]);
  useEffect(()=>{let alive=true;const abort=new AbortController();if(routeRef?.startsWith('i_'))api('/v1/collections/'+encodeURIComponent(routeRef)+'/status',undefined,{signal:abort.signal}).then(item=>{if(alive)setFocusItem(item)}).catch(()=>{});return()=>{alive=false;abort.abort()}},[api,routeRef]);
  useEffect(()=>{let alive=true;const abort=new AbortController();
    async function load(){try{const result=await loadContentTaskData(api,abort.signal);if(alive){setData(old=>({...result,jobs:mergeLiveJobs(result.jobs,old?.jobs||[])}));setError('')}}catch(err){if(alive&&err.name!=='AbortError')setError(err.message)}finally{if(alive)setLoading(false)}}
    void load();return()=>{alive=false;abort.abort()};
  },[api,revision]);
  useEffect(()=>{const live=activity.data;if(!live)return;setData(old=>old?{...old,jobs:mergeLiveJobs(old.jobs,live.jobs),worker:live.worker}:old);
    if(completion.current!==null&&completion.current!==live.completion_revision)refresh();completion.current=live.completion_revision;
  },[activity.data]);
  const itemRows=[...(data?.items||[])];if(focusItem&&!itemRows.some(item=>item.material_ref===focusItem.material_ref))itemRows.push(focusItem);
  const grouped=contentTasks(data?.jobs||[],data?.prepared||[],itemRows);
  const contents=(data?grouped.contents:[]).filter(task=>filter==='all'||task.state===filter);
  const current=data&&grouped.contents.find(task=>task.id===detail||detail?.startsWith('link:')&&task.jobs.some(job=>job.link_url===detail.slice(5)));
  const candidates=data?.prepared||[],chosen=candidates.filter(item=>selected.includes(item.input_id)),plan=cloudPlan(chosen);
  const counts=Object.fromEntries(['all','active','pending','attention','done'].map(state=>[state,state==='all'?grouped.contents.length:grouped.contents.filter(t=>t.state===state).length]));
  const filters={all:'全部',active:'处理中',pending:'待提取',attention:'需处理',done:'已完成'};
  return <Page title="内容任务" description="每条内容一个任务，查看进度并继续未完成的步骤。" className="content-task-page" action={<div className="flex gap-2"><button className={button} onClick={refresh}>刷新</button><button className={primary} onClick={()=>{setSelected([]);key.current=crypto.randomUUID();setBatch(true)}}>批量提取</button></div>}>
    <Notice error>{error||action.error}</Notice><Notice>{action.notice}</Notice>
    {data?.worker?.online===false&&<Notice error>后台未运行，任务已保存。请重新打开应用继续处理。</Notice>}
    <div className="content-task-filters" role="group" aria-label="筛选内容任务">{Object.entries(filters).map(([id,label])=><button key={id} aria-pressed={filter===id} onClick={()=>setFilter(id)}>{label}<span>{counts[id]}</span></button>)}</div>
    <section className="content-task-list" aria-label="内容任务列表">
      {loading&&!data&&<Notice>正在读取内容任务…</Notice>}
      {!loading&&!contents.length&&<div className="content-task-empty">{filter==='all'?'还没有内容。保存链接或同步收藏后，会在这里显示处理进度。':'这个分类暂时没有内容任务。'}</div>}
      {contents.map(task=>{const Icon=task.running?LoaderCircle:task.state==='attention'?AlertCircle:task.state==='done'?CheckCircle2:Clock;
        return <article key={task.id} className={'content-task-row '+task.state}>
          <Icon size={20} className={task.running?'animate-spin':''}/>
          <button className="content-task-open" onClick={()=>setDetail(task.id)}><h2>{displayTitle(task.title)}</h2><p><span className="content-task-state">{task.stateLabel}</span><span>{task.progress} / {task.steps.length} 步</span></p><div className="content-task-track" aria-label="步骤进度">{task.steps.map(step=><span key={step.id} className={step.state} title={`${step.label}：${taskStateNames[step.state]}`}/>)}</div></button>
          <button className="content-task-detail-button" onClick={()=>setDetail(task.id)}>查看进度<ChevronRight size={16}/></button>
        </article>;
      })}
    </section>
    {data?.itemNext!=null&&<p className="cc-inline-help">尚未处理的更多收藏，请在收藏库选择内容开始提取。</p>}
    {grouped.other.length>0&&<details className="content-task-system"><summary>同步与系统记录 · {grouped.other.length}</summary><div>{grouped.other.map(job=><article key={job.job_id}><span>{job.title}</span><span>{({succeeded:'已完成',partial:'部分完成',failed:'失败',blocked:'需处理',running:'处理中',queued:'等待处理',cancelled:'已取消'})[job.state]||job.state}</span>{job.kind==='sync'&&<button className="cc-text-button" onClick={()=>navigateTo('#sync')}>查看同步来源</button>}{job.error_message&&<p>{job.error_message}</p>}</article>)}</div></details>}
    {current&&!retry&&!extract&&<ContentTaskDetail key={current.id} api={api} task={current} onClose={()=>setDetail('')} onChanged={refresh} onProgress={job=>updateJob(job,current.material_ref,current.title)} onExtract={setExtract} onRetry={(job,role)=>setRetry({job,role})}/>}
    {retry&&<RetryStepDialog api={api} job={retry.job} role={retry.role} onClose={()=>setRetry(null)} onDone={job=>{updateJob(job,current?.material_ref||retry.job.material_ref,current?.title||retry.job.title);refresh();action.setNotice('这一步已排队，完成后会更新同一条内容。')}}/>}
    {extract&&<Extraction api={api} item={extract} onClose={()=>{setExtract(null);refresh()}} onChanged={refresh} onNotice={action.setNotice} onTasks={()=>{setDetail(extract.material_ref);setExtract(null)}} onSettings={()=>{setExtract(null);navigateTo('#settings/models')}}/>}
    {batch&&<Dialog title="批量提取" onClose={()=>setBatch(false)} busy={action.busy}><Notice error>{action.error}</Notice>{candidates.map(item=><label key={item.input_id} className="flex gap-3 py-3"><input type="checkbox" checked={selected.includes(item.input_id)} disabled={item.planned_calls_before_reuse==null} onChange={()=>setSelected(old=>old.includes(item.input_id)?old.filter(id=>id!==item.input_id):old.length<20?[...old,item.input_id]:old)}/><span>{displayTitle(item.title)}<small className="block">{item.audio_segments} 段音频 · {item.visual_frames} 张画面</small></span></label>)}{!candidates.length&&<Notice>还没有准备好的媒体。</Notice>}<p>已选 {chosen.length} 条{plan?` · 最多 ${plan.total} 次模型请求`:''}</p><button className={primary} disabled={action.busy||!plan||data?.worker?.capabilities?.model_calls!==true} onClick={()=>action.run(async()=>{await api('/v1/management/history',{input_ids:chosen.map(i=>i.input_id),idempotency_key:key.current,max_calls:plan.perItem,fee_confirmed:true});setBatch(false);refresh()})}>确认开始提取</button></Dialog>}
  </Page>;
}
