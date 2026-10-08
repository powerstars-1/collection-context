import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {ArrowLeft,Copy,ExternalLink,FileText,Maximize2,Minimize2,MoreHorizontal,Sparkles} from 'lucide-react';
import {ReadingContent} from '../ReadingContent';
import {FrameGallery} from '../FrameGallery';
import {useArtifact} from './useLibrary';
import {artifactNames,copyEvidence,sourceNames,readingArtifact} from './presentation.mjs';

export function Reader({api,loadImage,item,focus,view,tab,onTab,onBack,onExpand,onCollapse,onAI,onMore,onProcess:requestProcess,onTasks,onNotice,canManage,draft,onDraft,onDirty,onChanged}){
  const artifact=readingArtifact(item,tab);
  const read=useArtifact(api,item.id,artifact,item.artifacts?.[artifact]?.version||item.library_version,item.artifacts?.[artifact]?.state);
  const [saving,setSaving]=useState(false),[saveError,setSaveError]=useState('');
  const scroll=useRef(null),positions=useRef({});
  const [evidenceTarget,setEvidenceTarget]=useState(null);
  const evidencePages=useRef(0);
  function openEvidence(id){evidencePages.current=0;setEvidenceTarget(id);onTab(id.startsWith('a_')?'audio':'screen')}
  useEffect(()=>{
    if(!evidenceTarget||read.busy||!read.result||read.result.artifact!==artifact||!['audio','screen','image'].includes(artifact))return;
    const node=scroll.current?.querySelector('[data-evidence-id="'+evidenceTarget+'"]');
    if(node){let parent=node;while(parent&&parent!==scroll.current){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement}scroll.current.scrollTop+=node.getBoundingClientRect().top-scroll.current.getBoundingClientRect().top-24;setEvidenceTarget(null)}
    else if(read.result.next_offset!=null&&!read.error&&evidencePages.current<10){evidencePages.current++;void read.loadMore()}
    else {onNotice(read.result.next_offset!=null?'已打开对应内容；该依据位于后文，请点击“继续加载内容”。':'已打开对应内容；当前结果未找到此片段编号，可对照原文查看。');setEvidenceTarget(null)}
  },[evidenceTarget,read.busy,read.result,artifact]);
  useLayoutEffect(()=>{if(scroll.current)scroll.current.scrollTop=positions.current[tab]||0},[tab,focus,view,!!read.result]);
  const current=draft??read.result?.text??'';
  const dirty=tab==='user_note'&&draft!==undefined&&draft!==(read.result?.text||'');
  useEffect(()=>{onDirty(dirty)},[dirty]);
  const summary=item.summary,job=item.job;
  function onProcess(){if(job||item.hasReading)onTasks();else requestProcess()}
  const message=job?.error_message||(item.state==='running'?'后台处理中，完成后会更新结果。':item.state==='unknown'?'上次请求结果待核实，请先查看任务，不要直接重复提交。':item.state==='partial'&&job?.can_retry?'已有正文保留；本次仍有未完成步骤，请查看任务中的具体原因。':summary?'已有总结可以阅读，准确性及覆盖范围以正文说明为准。':item.hasReading?'已有部分正文可读，可以继续补齐。':'原作品已保存；提取后可在这里阅读总结、转写与画面文字。');
  async function save(){setSaving(true);setSaveError('');try{await api('/v1/management/note-save',{material_ref:item.id,text:current,expected_version:item.artifacts?.user_note?.version||null});onDraft(undefined);onDirty(false);await read.reload();onChanged();onNotice('备注已保存。')}catch(err){setSaveError(err.message)}finally{setSaving(false)}}
  async function copy(){try{await navigator.clipboard.writeText(copyEvidence(item,artifact,read.result.text,read.result.next_offset!=null));onNotice('已复制当前加载的内容与来源。')}catch{onNotice('复制失败，请在正文中选择文字复制。')}}
  const tabs=[['summary','总结'],['audio','音频转写'],['screen','画面提取'],['original','平台原文'],['user_note','我的备注']];
  return <section className={'reader-pane reading-surface '+(focus?'reader-expanded':view==='canvas'?'reader-preview':'reader-detail')} aria-label="资料阅读">
    <div className="reader-bar"><button className="text-button reader-back" onClick={onBack}><ArrowLeft size={15}/>{focus?'返回阅读位置':view==='canvas'?'收起详情':'返回列表'}</button><div>{!focus?<button className="secondary compact reader-expand" onClick={onExpand}><Maximize2 size={14}/>展开阅读</button>:<button className="text-button" onClick={onCollapse}><Minimize2 size={14}/>显示列表</button>}{item.url&&<a className="icon" aria-label="打开原作品" href={item.url} target="_blank" rel="noreferrer"><ExternalLink size={16}/></a>}<button className="secondary compact reader-ai" onClick={onAI}><Sparkles size={14}/>交给 AI</button>{canManage&&<button className="icon" aria-label="更多资料操作" onClick={onMore}><MoreHorizontal size={17}/></button>}</div></div>
    <div ref={scroll} className="reader-scroll cc-library-scroll" onScroll={e=>{positions.current[tab]=e.currentTarget.scrollTop}}><article className="reading"><div className="eyebrow">抖音 · {item.author}</div><h1>{item.title}</h1><div className="reading-meta"><span>{item.date}</span><span>{item.sources.map(k=>sourceNames[k]).join(' / ')}</span></div>
      <div className={'processing '+item.state}><div><span className={'status '+item.state}>{item.stateLabel}</span><p>{message}</p></div>{canManage&&<button className={summary?'secondary compact':'primary compact'} onClick={job||item.hasReading?onTasks:onProcess}>{job||item.hasReading?'查看处理进度':'提取内容'}</button>}</div>
      <div className="reader-tabs" role="tablist" aria-label="阅读内容">{tabs.map(([id,label],index)=><button key={id} role="tab" aria-controls="reading-panel" tabIndex={tab===id?0:-1} aria-selected={tab===id} onClick={()=>{setEvidenceTarget(null);onTab(id)}} onKeyDown={event=>{const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;if(next!==null){event.preventDefault();onTab(tabs[next][0]);event.currentTarget.parentElement.querySelectorAll('[role=tab]')[next].focus()}}}>{label}</button>)}</div>
      <div className="reader-section-caption"><span>{{summary:'综合音频与画面整理的要点',audio:'视频中说了什么 · 按音频片段排列',screen:'画面中的文字与内容 · 按出现顺序排列',original:'原作者发布的标题、文案与来源',user_note:'你自己的补充与想法'}[tab]}</span></div>
      <div id="reading-panel" role="tabpanel" aria-label={artifactNames[artifact]}>
        {read.error&&<p className="inline-error" role="alert">{read.error}<button className="text-button" onClick={read.reload}>重试读取</button></p>}
        {read.busy&&!read.result?<p className="loading-copy" role="status">正在读取{artifactNames[artifact]}…</p>:tab==='user_note'?<div className="note-editor"><h2>记下你想怎么用它</h2><p>自己的判断与原作者内容分开保存。</p><textarea aria-label="我的备注" maxLength={32000} value={current} readOnly={!canManage} onChange={e=>onDraft(e.target.value)}/>{saveError&&<p role="alert">{saveError}</p>}{canManage&&<button className="primary" disabled={!dirty||saving||read.busy||read.result?.next_offset!=null} onClick={save}>{saving?'保存中…':'保存备注'}</button>}<span>{dirty?'有未保存的修改':'已保存'}</span></div>:read.result?.text?<><div className="article-tools"><span>{read.result.state==='stale'?'旧结果 · 需要更新':'已保存内容'}{read.result.next_offset!=null?' · 当前只加载部分':''}</span><button className="text-button" onClick={copy}><Copy size={14}/>复制原文与来源</button></div><ReadingContent text={read.result.text} artifact={artifact} omitTitle={item.title} partial={read.result.next_offset!=null} onEvidence={openEvidence}/></>:!read.error&&<div className="artifact-empty"><FileText size={26}/><h2>{read.result?.state==='not_applicable'?'这条作品不适用此类内容':artifactNames[artifact]+'尚未生成'}</h2><p>已有原文和其他结果保留，可以切换页签阅读。</p>{canManage&&read.result?.state!=='not_applicable'&&<button className="secondary" onClick={onProcess}>查看提取方式</button>}</div>}
        {read.result?.next_offset!=null&&<div className="read-more"><p>已加载 {read.result.text.length} / {read.result.total_chars} 字符</p><button className="secondary" disabled={read.busy} onClick={read.loadMore}>继续加载内容</button></div>}
        {['screen','image'].includes(artifact)&&item.layout!=='legacy_douyin_readonly'&&<div className="live-controls"><FrameGallery api={api} loadImage={loadImage} materialRef={item.id}/></div>}
      </div>
      <footer className="evidence-footer"><span><FileText size={14}/>来源与边界</span><p>内容来自保存的作品及模型提取；入库时间不代表点赞或收藏时间。生成的总结不代表人工核验通过。</p>{item.fullTitle!==item.title&&<details><summary>完整标题</summary><p>{item.fullTitle}</p></details>}{item.url&&<a href={item.url} target="_blank" rel="noreferrer">查看原作品 <ExternalLink size={13}/></a>}</footer>
    </article></div>
  </section>;
}
