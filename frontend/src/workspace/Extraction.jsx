import {useEffect,useRef,useState} from 'react';
import {Dialog,Notice,button,primary,useData} from '../ui';
import {activeJob} from './presentation.mjs';

// A persisted single-link job prepares missing media. Cloud work is a second,
// explicitly reviewed action; closing the dialog never loses or cancels the job.
export function Extraction({api,item,onClose,onChanged,onSettings,onTasks,onNotice}){
  const [plan,setPlan]=useState(null),[missing,setMissing]=useState(false),[job,setJob]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [revision,setRevision]=useState(0),key=useRef(crypto.randomUUID()),lock=useRef(false);
  useEffect(()=>{let alive=true;setError('');setBusy(true);setMissing(false);setPlan(null);
    api('/v1/management/process-preview',{material_ref:item.id}).then(data=>{if(alive){setPlan(data);if(activeJob(data.media_job))setJob(data.media_job)}}).catch(err=>{if(alive){if(err.code==='media_not_prepared')setMissing(true);else setError(err.message)}}).finally(()=>{if(alive)setBusy(false)});
    return()=>{alive=false};
  },[api,item.id,revision]);
  const progress=useData(api,'/v1/management/link-status',{job_id:job?.job_id},{enabled:activeJob(job),poll:data=>activeJob(data)?3000:false});
  useEffect(()=>{const data=progress.data;if(!data||data.job_id!==job?.job_id)return;setJob(data);if(!activeJob(data)){key.current=crypto.randomUUID();onChanged();if(data.link_result?.download==='ready')setRevision(v=>v+1);else setError(data.error_message||'这条资料的媒体准备未完成，请到任务中查看原因。')}},[progress.data]);
  useEffect(()=>{if(progress.error)setError(progress.error)},[progress.error]);
  async function run(operation){if(lock.current)return;lock.current=true;setBusy(true);setError('');try{await operation()}catch(err){setError(err.message)}finally{lock.current=false;setBusy(false)}}
  const configured=plan&&['summary',...(plan.audio_segments?['audio']:[]),...(plan.visual_frames&&!plan.audio_only?['vision']:[])].every(role=>plan.models?.[role]);
  const existing=plan?.latest_job;
  const needsTask=activeJob(existing)||existing?.unknown_calls>0||existing?.can_retry;
  const confirmedCalls=plan?.planned_new_calls??plan?.planned_calls_before_reuse;
  async function prepare(mode){await run(async()=>{const result=await api('/v1/management/media-prepare',{material_ref:item.id,mode,idempotency_key:crypto.randomUUID()});setJob(result);setPlan(null);onChanged()})}
  return <Dialog title={missing?'保存这一条的原媒体':'提取这条内容'} onClose={onClose} busy={busy}>
    <p>{item.title}</p><Notice error>{error}</Notice>
    {busy&&!plan&&!missing&&<Notice>正在读取处理计划…</Notice>}
    {missing&&!activeJob(job)&&<><p className="help">先为这一条保存视频 / 图片并准备音频和关键帧。不会重新同步收藏夹，也不会在这一步调用付费模型。</p><button className={primary} disabled={busy||!item.url} onClick={()=>run(async()=>{const result=await api('/v1/management/link-submit',{url:item.url,download:true,idempotency_key:key.current,source_confirmed:true});setJob(result);if(!activeJob(result))setRevision(v=>v+1)})}>只保存这一条的媒体</button></>}
    {activeJob(job)&&<><Notice>正在保存并准备原媒体，可以关闭窗口继续浏览。任务已由后台保存。</Notice><button className={button} onClick={onTasks}>查看任务</button></>}
    {plan&&<><div className="confirm-steps"><p>音频 {plan.audio_segments} 段 · 已保存画面 {plan.visual_frames} 张</p><p>最多 {plan.planned_new_calls??plan.planned_calls_before_reuse??'待确定'} 次新模型请求。{plan.reusable_audio_segments?` ${plan.reusable_audio_segments} 段已有转写会复用。`:''}{plan.reusable_visual_frames?` ${plan.reusable_visual_frames} 张已有画面结果会复用。`:''}</p></div><details><summary>使用的模型</summary>{Object.entries(plan.models||{}).map(([role,model])=><p key={role}>{({audio:'转写',vision:'画面',summary:'总结'})[role]}：{model}</p>)}</details>
      {plan.has_audio===false&&<Notice>这条作品没有音轨，跳过音频转写，只提取画面并总结。</Notice>}
      {plan.audio_only&&<Notice>本次只转写音频，不等待画面识别。之后可以补充画面。</Notice>}
      {plan.selection_reduced&&<Notice>画面较多，已按时间分布保留代表帧；原视频完整保留。总结会注明覆盖范围。</Notice>}
      {(plan.preparation_issues||[]).filter(issue=>issue.code!=='vision_deferred').map(issue=><Notice key={issue.role} error>{issue.role==='audio'?'音频':'画面'}准备未完成（{issue.code}）。本次只提取已准备好的部分，总结会保留缺口说明。</Notice>)}
      {!activeJob(existing)&&!activeJob(job)&&<div className="flex flex-wrap gap-2">
        {(plan.audio_only||plan.preparation_issues?.length>0||plan.media_saved&&!plan.input_id)&&<button className={button} disabled={busy} onClick={()=>prepare('full')}>{plan.input_id?'重新准备画面':'准备已保存的媒体'}</button>}
        {plan.has_audio!==false&&!plan.audio_only&&<button className={button} disabled={busy} onClick={()=>prepare('audio')}>只转写音频</button>}
      </div>}
      {plan.media_saved&&!plan.input_id&&<Notice>原媒体已保存，先准备音频与画面即可继续，不需要重新下载。</Notice>}
      {plan.preparation_error&&<Notice error>已保存的媒体输入暂时不可用（{plan.preparation_error}），本次不会请求模型。</Notice>}
      <p className="model-warning">将音频和选定画面发送到你配置的模型服务。费用以供应商为准；用户备注不上传。</p>
      {needsTask?<><Notice>{activeJob(existing)?'这条内容已经在处理，不必重复提交。':'这条内容有未完成步骤，请查看当前进度并重试对应步骤。'}</Notice><button className={primary} onClick={onTasks}>查看已有任务</button></>:!configured?<button className={primary} onClick={onSettings}>先配置所需模型</button>:<><Notice>{!plan.model_execution_enabled?'后台未开启模型执行，请检查启动设置。':null}</Notice><button className={primary} disabled={busy||activeJob(job)||!plan.input_id||!plan.model_execution_enabled||confirmedCalls==null} onClick={()=>run(async()=>{await api('/v1/management/history',{input_ids:[plan.input_id],idempotency_key:key.current+'-extract',max_calls:confirmedCalls,fee_confirmed:true});onChanged();onNotice?.('提取已排队，当前进度在这里查看。');onTasks()})}>{busy?'提交中…':'确认开始提取'}</button></>}
    </>}
  </Dialog>;
}
