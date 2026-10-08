import {useEffect,useRef,useState} from 'react';
import {Check,Clock,LoaderCircle,AlertCircle,ChevronRight,SkipForward} from 'lucide-react';
import {Dialog,Notice,button,primary,useData,useAction} from '../ui';
import {contentSteps,stepRole,taskStateNames} from './contentTasks.mjs';
import {displayTitle} from '../lib/library-presentation';
import {navigateTo} from '../lib/navigation';

const active=job=>['queued','running'].includes(job?.state);
const icons={ready:Check,partial:AlertCircle,failed:AlertCircle,blocked:Clock,pending:Clock,running:LoaderCircle,queued:Clock,not_applicable:SkipForward};

export function ContentTaskDetail({api,task,onClose,onChanged,onExtract,onRetry,onProgress}){
  const taskRunning=task.jobs.some(active);
  const preview=useData(api,'/v1/management/process-preview',{material_ref:task.material_ref},{poll:value=>active(value?.media_job)||active(value?.latest_job)?3000:false,enabled:!!task.material_ref});
  const status=useData(api,'/v1/collections/'+encodeURIComponent(task.material_ref)+'/status',undefined,{enabled:!!task.material_ref,method:'GET'});
  const wasRunning=useRef(taskRunning),previewRunning=useRef(false);
  useEffect(()=>{if(taskRunning!==wasRunning.current){preview.refresh();status.refresh();wasRunning.current=taskRunning}},[taskRunning]);
  useEffect(()=>{const running=active(preview.data?.media_job)||active(preview.data?.latest_job);if(previewRunning.current&&!running){status.refresh();onChanged()}previewRunning.current=running},[preview.data]);
  const action=useAction(),key=useRef(crypto.randomUUID());
  const [mode,setMode]=useState(null);
  const selectedMode=mode||preview.data?.media_job?.preparation_mode||task.jobs.find(job=>job.kind==='add'&&active(job))?.preparation_mode|| ((preview.data||task.prepared)?.audio_only?'audio':'full');
  const steps=contentSteps(task,preview.data||task.prepared,status.data||task.item,selectedMode);
  const observed=[preview.data?.media_job,preview.data?.latest_job].filter(Boolean);
  useEffect(()=>{for(const job of observed)onProgress?.(job)},[JSON.stringify(observed.map(job=>[job.job_id,job.updated_at,job.state,job.stages,job.stage_details]))]);
  const inProgress=task.jobs.some(active)||active(preview.data?.latest_job)||active(preview.data?.media_job);
  const scopeMismatch=preview.data?.input_id&&selectedMode!==(preview.data.audio_only?'audio':'full');
  async function prepare(choice=selectedMode){await action.run(async()=>{const job=await api('/v1/management/media-prepare',{material_ref:task.material_ref,mode:choice,idempotency_key:crypto.randomUUID()});setMode(choice);onProgress?.(job);onChanged();preview.refresh();action.setNotice('已排队，已有输入和正文继续保留。')})}
  async function saveMedia(){await action.run(async()=>{const job=await api('/v1/management/link-submit',{url:status.data?.source_url||task.item?.source_url,download:true,source_confirmed:true,idempotency_key:key.current});onProgress?.(job);onChanged();preview.refresh();action.setNotice('保存原媒体已排队。')})}
  function renderStep(step){
    const Icon=icons[step.state]||Clock,failed=['failed','blocked','partial'].includes(step.state);
    const canRetry=step.job?.can_retry&&failed&&!inProgress&&(step.id!=='publish'||step.state!=='partial');
    const runtimeFixed=preview.data?.model_runtime_ready&&step.errors.some(message=>message.includes('模型组件未就绪'));
    return <li key={step.id} className={'content-task-step '+step.state}>
      <span className="content-step-icon"><Icon size={16} className={step.state==='running'?'animate-spin':''}/></span>
      <div className="content-step-body"><div className="content-step-title"><h4>{step.label}</h4><span>{step.reused?'已复用':taskStateNames[step.state]||step.state}{step.total>1?` · ${step.completed} / ${step.total}`:''}</span></div>
        {step.description&&<p>{step.description}</p>}
        {step.errors.length>0&&<p className="content-step-error">{runtimeFixed?'上次模型组件未启动，运行环境已修复，可以重试这一步。':step.errors[0]}</p>}
        <div className="content-step-actions">
          {step.id==='original'&&step.state==='failed'&&!inProgress&&task.item?.source_url&&<button className={button} disabled={action.busy} onClick={saveMedia}>重试保存作品</button>}
          {step.id==='media'&&['pending','failed'].includes(step.state)&&task.material_ref&&!inProgress&&<button className={button} disabled={action.busy} onClick={saveMedia}>{step.state==='failed'?'重试保存媒体':'保存原媒体'}</button>}
          {step.id.startsWith('prepare_')&&['blocked','failed','pending'].includes(step.state)&&steps[1].state==='ready'&&!inProgress&&<button className={button} disabled={action.busy} onClick={()=>prepare(step.id==='prepare_audio'?'audio':'full')}>{failed?'重试':'开始'}{step.label}</button>}
          {canRetry&&<button className={button} onClick={()=>onRetry(step.job,step.id)}>重试{step.label}</button>}
          {['audio','vision','summary','publish'].includes(step.id)&&(step.available||['ready','partial'].includes(step.state))&&task.material_ref&&<button className="cc-text-button" onClick={()=>navigateTo('#library/'+encodeURIComponent(task.material_ref)+(step.id==='publish'?'':'/'+({audio:'audio',vision:'screen',summary:'summary'})[step.id]))}>查看{step.id==='publish'?'已保存内容':step.label}<ChevronRight size={13}/></button>}
        </div>
      </div>
    </li>;
  }
  return <Dialog title="内容处理进度" onClose={onClose}>
    <div className="content-task-heading"><h3>{displayTitle(task.title)}</h3><p>{task.stateLabel}</p></div>
    <Notice error>{action.error||status.error||(preview.error&&!/尚未准备原媒体/.test(preview.error)?preview.error:'')}</Notice><Notice>{action.notice}</Notice>
    <div className="task-scope" role="group" aria-label="本次处理范围"><span>处理范围</span><button aria-pressed={selectedMode==='full'} disabled={inProgress||action.busy} onClick={()=>setMode('full')}>音频与画面</button><button aria-pressed={selectedMode==='audio'} disabled={inProgress||action.busy} onClick={()=>setMode('audio')}>仅音频</button></div>
    <div className="task-flow" aria-label="内容处理流程">
      <ol className="content-task-steps task-flow-entry" aria-label="共同输入">{steps.filter(step=>['original','media'].includes(step.id)).map(renderStep)}</ol>
      <div className="task-fork" aria-hidden="true"><span/><span/></div>
      <div className={'task-branches '+(selectedMode==='audio'?'audio-only':'')}>
        <section aria-label="音频分支"><header>音频分支</header><ol className="content-task-steps">{steps.filter(step=>['prepare_audio','audio'].includes(step.id)).map(renderStep)}</ol></section>
        <section aria-label="画面分支"><header>画面分支{selectedMode==='audio'&&<small>本次不处理 · 已有内容保留</small>}</header><ol className="content-task-steps">{steps.filter(step=>['prepare_vision','vision'].includes(step.id)).map(renderStep)}</ol></section>
      </div>
      <div className="task-merge" aria-hidden="true"><span/><span/></div>
      <p className="task-flow-caption">两条分支独立；转圈表示正在执行，时钟表示排队等待。已完成的结果会复用。</p>
      <ol className="content-task-steps task-flow-result" aria-label="汇总结果">{steps.filter(step=>['summary','publish'].includes(step.id)).map(renderStep)}</ol>
    </div>
    <div className="content-task-footer">
      {scopeMismatch&&!inProgress&&<button className={primary} disabled={action.busy} onClick={()=>prepare(selectedMode)}>准备所选内容</button>}
      {task.material_ref&&!inProgress&&!scopeMismatch&&preview.data&&!preview.data.latest_job&&steps.find(step=>step.id==='prepare_audio').state==='ready'&&(selectedMode==='audio'||steps.find(step=>step.id==='prepare_vision').state==='ready')&&<button className={primary} onClick={()=>onExtract({...task,id:task.material_ref,url:status.data?.source_url||task.item?.source_url})}>开始提取内容</button>}
      {inProgress&&<Notice>后台正在处理，可以关闭窗口，进度会自动更新。</Notice>}
      {task.jobs.some(active)&&<button className={button} disabled={action.busy} onClick={()=>action.run(async()=>{for(const job of task.jobs.filter(active))await api('/v1/management/cancel',{job_id:job.job_id});onChanged();preview.refresh()})}>停止后续处理</button>}
      <details><summary>历史记录 · {task.jobs.length} 次运行</summary><div className="content-task-history">{task.jobs.map(job=><p key={job.job_id}><span>{job.kind==='process'?'提取内容':job.link_result?'保存 / 准备媒体':'保存作品'}</span><span>{({succeeded:'完成',partial:'部分完成',failed:'失败',blocked:'受阻',running:'处理中',queued:'等待中',cancelled:'已停止'})[job.state]||job.state}</span><time>{new Date(job.created_at).toLocaleString('zh-CN')}</time></p>)}</div></details>
    </div>
  </Dialog>;
}

export function RetryStepDialog({api,job,role,onClose,onDone}){
  const [plan,setPlan]=useState(null),[error,setError]=useState(''),[reviewed,setReviewed]=useState(false);
  const action=useAction(),key=useRef(crypto.randomUUID());
  useEffect(()=>{let alive=true;
    (async()=>{try{const available=await api('/v1/management/retry-preview',{job_id:job.job_id});
      const stages=available.stages.filter(stage=>stage.selectable&&stepRole(stage.name)===role).map(s=>s.name);
      if(!stages.length)throw new Error('这一步已完成或当前无需重试，请刷新任务查看最新状态。');
      const preview=await api('/v1/management/retry-preview',{job_id:job.job_id,stages});if(alive)setPlan(preview);
    }catch(err){if(alive)setError(err.message)}})();return()=>{alive=false};
  },[api,job.job_id,role]);
  return <Dialog title={'重试'+({audio:'音频转写',vision:'画面提取',summary:'内容总结',publish:'保存笔记'})[role]} onClose={onClose} busy={action.busy}>
    <Notice error>{error||action.error}</Notice>{!error&&!plan&&<Notice>正在读取这一步的处理状态…</Notice>}
    {plan&&<><p>已完成的部分会复用，本次处理这一步的未完成部分，并更新依赖它的总结和笔记。</p><p>最多 {plan.max_calls} 次新模型请求。</p>
      {plan.unknown_calls.length>0&&<label className="flex gap-2"><input type="checkbox" checked={reviewed} onChange={e=>setReviewed(e.target.checked)}/>上次有请求结果不明，我已核对用量并接受可能重复收费。</label>}
      <button className={primary} disabled={action.busy||plan.unknown_calls.length>0&&!reviewed} onClick={()=>action.run(async()=>{const queued=await api('/v1/management/retry',{job_id:job.job_id,stages:plan.selected_stages,preview_token:plan.preview_token,idempotency_key:key.current,max_calls:plan.max_calls,fee_confirmed:true,reviewed_call_ids:reviewed?plan.unknown_calls.map(c=>c.call_id):[],duplicate_charge_confirmed:reviewed});onDone(queued);onClose()})}>{action.busy?'提交中…':'确认重试这一步'}</button>
    </>}
  </Dialog>;
}
