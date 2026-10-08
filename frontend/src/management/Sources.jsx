import {useEffect,useRef,useState} from 'react';
import {button,primary,input,Page,Field,Notice,Dialog,useData,useAction} from '../ui';
import {navigateTo} from '../lib/navigation';
import {sourceResult,libraryUrl} from '../lib/library-presentation';
import {UserRound,RefreshCw,ChevronRight,Settings2,Trash2,History,MoreHorizontal,Plus,Bookmark,Heart,Folder,Video} from 'lucide-react';
import {ensureSource,canSyncSource,syncAllSources,loadSourceScopes} from './sourceActions.mjs';
import {useActivity} from '../lib/useActivity';

const labels={liked:'我的喜欢',saved:'我的收藏',collection:'收藏夹',creator:'博主作品'};
const sourceIcons={liked:Heart,saved:Bookmark,collection:Folder,creator:Video};
export const managementApi=(api,action,value={})=>api('/v1/management/'+action,value);

export function Sources({api}){
  const activity=useActivity(api),source=useData(api,'/v1/management/sources'),connection=useData(api,'/v1/management/connection');
  const run=useData(api,'/v1/management/connection-run',{}, {poll:value=>value?.active?3000:false});
  const action=useAction();
  const [adding,setAdding]=useState(false),[addTab,setAddTab]=useState('account'),[limit,setLimit]=useState(5),[download,setDownload]=useState(true);
  const [creator,setCreator]=useState(''),[resolvedCreator,setResolvedCreator]=useState(null);
  const [more,setMore]=useState([]),[next,setNext]=useState(null),[edit,setEdit]=useState(null),[history,setHistory]=useState(null),[remove,setRemove]=useState(null);
  const sourceRevision=useRef(null),connectionWasActive=useRef(false),seenRun=useRef(null);
  const connected=connection.data?.state==='verified',active=run.data?.active;
  const folders=connection.data?.folders||[];
  const scopes=[...(source.data?.scopes||[]),...more.filter(row=>!source.data?.scopes.some(scope=>scope.scope_id===row.scope_id))];
  const choices=[{value:'saved',name:'我的收藏',kind:'saved'},{value:'liked',name:'我的喜欢',kind:'liked'},...folders.map(folder=>({value:'folder:'+folder.collection_id,name:'收藏夹 · '+folder.name,kind:'collection',collection_id:folder.collection_id}))];
  const available=choices.filter(choice=>!scopes.some(scope=>scope.account_matches!==false&&scope.kind===choice.kind&&(choice.kind!=='collection'||scope.collection_id===choice.collection_id)));
  function refreshSources(){setMore([]);source.refresh();}
  useEffect(()=>{
    const data=activity.data;if(!data)return;
    if(sourceRevision.current!==null&&sourceRevision.current!==data.source_revision)refreshSources();
    sourceRevision.current=data.source_revision;
    if(data.connection_active&&!connectionWasActive.current)run.refresh();
    connectionWasActive.current=data.connection_active;
  },[activity.data]);
  useEffect(()=>{let alive=true;managementApi(api,'preferences').then(value=>{if(alive)setDownload(value.download_media)}).catch(()=>{});return()=>{alive=false}},[api]);
  useEffect(()=>{
    if(run.data?.state!=='completed'||seenRun.current===run.data.run_id)return;
    seenRun.current=run.data.run_id;
    if(run.data.mode==='creator'){setResolvedCreator(run.data.resolved_creator);action.setNotice('博主主页已解析，确认后即可添加来源。');}
    else{connection.refresh();refreshSources();action.setNotice(run.data.mode==='logout'?'已退出抖音，已有资料已保留。':'已连接抖音，可以添加同步来源。');}
  },[run.data?.state,run.data?.run_id]);
  useEffect(()=>{if(!more.length)setNext(source.data?.next_offset??null)},[source.data]);
  async function start(mode){await action.run(async()=>{await managementApi(api,'connection-start',{mode,source_confirmed:true});run.refresh();action.setNotice(mode==='folders'?'正在获取收藏夹。':'正在识别抖音账号；已登录时会自动连接。')})}
  async function logout(){await action.run(async()=>{await managementApi(api,'connection-logout');run.refresh();action.setNotice('正在退出抖音登录…')})}
  async function openAdd(){
    if(adding){setAdding(false);return;}
    setAdding(true);
    if(next!=null)await action.run(async()=>{const all=await loadSourceScopes(api);setMore(all.filter(row=>!source.data?.scopes.some(scope=>scope.scope_id===row.scope_id)));setNext(null);});
  }
  async function submit(scope){
    if(active)throw new Error('请等待当前账号操作完成。');
    if(!canSyncSource(scope,connected))throw new Error(scope.pending_count?'这个来源正在同步，请等待完成。':'请先连接此来源对应的抖音账号。');
    if(source.data?.worker?.online!==true)throw new Error('后台未运行，请重新打开应用。');
    return managementApi(api,'source-submit',{config_id:scope.config_id,idempotency_key:crypto.randomUUID(),source_confirmed:true});
  }
  async function add(choice){await action.run(async()=>{
    try{await ensureSource(api,choice.value,scopes,connection.data,{limit:Number(limit),download},true);action.setNotice(choice.name+'已添加，点击“立即同步”即可更新。');}
    finally{refreshSources();}
  })}
  async function syncOne(scope){await action.run(async()=>{try{await submit(scope);action.setNotice((scope.title||labels[scope.kind])+'已排队同步。');}finally{refreshSources();}})}
  async function syncAll(){await action.run(async()=>{
    try{
      const result=await syncAllSources(api,connected);
      action.setNotice(result.queued+' 个来源已排队。'+(result.skipped?' '+result.skipped+' 个来源正在同步或暂不可用，已跳过。':'')+(result.failed.length?' '+result.failed.length+' 个来源未提交：'+result.failed.map(row=>row.title+'（'+row.message+'）').join('；'):''));
    }finally{refreshSources();}
  })}
  async function timer(scope){await action.run(async()=>{
    const enabled=!scope.timer?.enabled;
    await managementApi(api,'source-timer',{config_id:!enabled&&scope.timer?scope.timer.config_id:scope.config_id,enabled,interval_minutes:scope.timer?.interval_minutes||60,reset_blocked:enabled&&!!scope.timer?.blocked_job_id,source_confirmed:enabled});
    refreshSources();action.setNotice(enabled?'自动同步已开启，按卡片显示的间隔执行。':'自动同步已关闭，仍可点击“立即同步”。');
  })}
  async function editSave(){await action.run(async()=>{await managementApi(api,'source-edit',{config_id:edit.config_id,limit:Number(edit.limit),download:edit.download});setEdit(null);refreshSources();action.setNotice('来源设置已保存，自动同步已暂停，可在卡片上重新开启。')})}
  async function removeSource(){await action.run(async()=>{await managementApi(api,'source-remove',{config_id:remove.config_id});setRemove(null);refreshSources();action.setNotice('同步来源已移除，已有资料保留；需要时可以重新添加。')})}
  async function addCreator(event){
    event.preventDefault();
    await action.run(async()=>{
      if(!resolvedCreator){await managementApi(api,'creator-resolve',{creator_url:creator,source_confirmed:true});run.refresh();action.setNotice('正在解析博主主页…');return;}
      await managementApi(api,'source-create',{creator_url:resolvedCreator.creator_url,limit:Number(limit),download});
      setCreator('');setResolvedCreator(null);refreshSources();action.setNotice('博主来源已添加，点击“立即同步”即可更新。');
    });
  }
  const writing=action.busy||active||source.busy;
  return <Page title="同步来源" description="添加来源后，可以立即同步，或开启自动同步。" className="cc-sources-page" action={<button className={button} onClick={()=>navigateTo('/')}>返回收藏库</button>}>
    <Notice error>{source.error||connection.error||run.error||action.error}</Notice><Notice>{action.notice}</Notice>
    <section className="cc-sync-account" aria-label="抖音账号">
      <span className="cc-account-icon"><UserRound size={22}/></span>
      <div className="cc-account-copy"><h2>{connected?connection.data.display_name:'连接你的抖音'}</h2><p>{active?(run.data?.mode==='logout'?'正在退出登录…':connected?'正在获取收藏夹…':'请在打开的窗口登录，完成后自动识别账号。'):connected?'已连接 · 可添加喜欢、收藏和收藏夹':'连接后可以添加喜欢、收藏和指定收藏夹。'}</p></div>
      <div className="cc-result-actions">{active?(run.data?.mode==='logout'?<button className={button} disabled>退出中…</button>:<button className={button} disabled={action.busy} onClick={()=>action.run(async()=>{await managementApi(api,'connection-cancel',{run_id:run.data.run_id});run.refresh()})}>取消连接</button>):<>{connected?<button className="cc-text-button" disabled={action.busy||run.busy||run.data?.headless===true} onClick={()=>start('folders')}><RefreshCw size={14}/>刷新收藏夹</button>:<button className={primary} disabled={action.busy||run.busy||run.data?.enabled===false||run.data?.headless===true} onClick={()=>start('login')}>连接抖音</button>}{connected&&<button className="cc-text-button" disabled={action.busy||run.busy} onClick={logout}>退出登录</button>}</>}</div>
    </section>
    {run.data?.state==='failed'&&(!connected||run.data?.mode==='logout')&&<Notice error>{run.data?.mode==='logout'?'退出登录未完成，请重试。':'未能连接抖音。'}<details><summary>查看详情</summary>{run.data.error_code}</details></Notice>}
    {connected&&connection.data.error_code&&<Notice>账号已连接，收藏夹暂未获取。仍可添加喜欢或收藏。</Notice>}
    {run.data?.enabled===false&&<Notice error>当前运行环境未启用抖音连接，请使用完整应用启动。</Notice>}
    {run.data?.headless===true&&!connected&&<Notice>当前是无桌面服务，请先通过已配置的登录方式完成抖音授权。</Notice>}
    <section className="cc-source-list" aria-label="已添加的来源">
      <div className="cc-section-heading"><div><h2>已添加的来源</h2>{source.data&&<span className="cc-inline-help">{source.data.total_scopes} 个来源</span>}</div><div className="cc-result-actions"><button className={button} aria-haspopup="dialog" aria-controls={adding?'source-add-dialog':undefined} disabled={action.busy} onClick={openAdd}><Plus size={15}/>添加来源</button><button className={primary} disabled={writing||!scopes.some(scope=>canSyncSource(scope,connected))||source.data?.worker?.online!==true} onClick={syncAll}>同步全部</button></div></div>
      {adding&&<Dialog id="source-add-dialog" className="cc-source-add-dialog" title="添加来源" onClose={()=>setAdding(false)} busy={action.busy} dismissOnBackdrop>
        <Notice error>{action.error}</Notice><Notice>{action.notice}</Notice>
        <div className="cc-evidence-tabs" role="tablist" aria-label="来源类型"><button role="tab" id="source-account-tab" aria-controls="source-account-panel" aria-selected={addTab==='account'} onClick={()=>setAddTab('account')}>账号内容</button><button role="tab" id="source-creator-tab" aria-controls="source-creator-panel" aria-selected={addTab==='creator'} onClick={()=>setAddTab('creator')}>博主作品</button></div>
        {addTab==='account'?<div id="source-account-panel" role="tabpanel" aria-labelledby="source-account-tab">
          {!connected?<p className="cc-inline-help">先连接抖音，即可添加喜欢、收藏和收藏夹。</p>:<>{available.map(choice=>{const Icon=sourceIcons[choice.kind];return <div className="cc-source-add-row" key={choice.value}><span><Icon size={17}/>{choice.name}</span><button className={button} disabled={writing||next!=null} onClick={()=>add(choice)}>添加</button></div>})}
          {!available.length&&<p className="cc-inline-help">账号来源已全部添加。</p>}
          {next!=null&&<p className="cc-inline-help">正在核对已有来源…</p>}
          {!folders.length&&<p className="cc-inline-help">{connection.data.folders_observed?'账号没有可选收藏夹。':'收藏夹尚未获取，可关闭弹窗后刷新收藏夹。'}</p>}
          {connection.data.folders_observed&&!connection.data.complete&&<p className="cc-inline-help">收藏夹列表尚未完整获取，可刷新继续检查。</p>}
        </>}</div>:<form id="source-creator-panel" role="tabpanel" aria-labelledby="source-creator-tab" className="cc-creator-form" onSubmit={addCreator}><Field label="博主主页链接"><input className={input} value={creator} onChange={event=>{setCreator(event.target.value);setResolvedCreator(null)}} placeholder="粘贴博主主页或分享链接" required maxLength={8192} disabled={active}/></Field><button className={primary} disabled={writing}>{resolvedCreator?'确认添加':active&&run.data?.mode==='creator'?'解析中…':'解析博主'}</button>{resolvedCreator&&<p className="cc-inline-help">{resolvedCreator.display_name||'已确认博主主页'}</p>}</form>}
        <details className="cc-source-add-options"><summary>新来源的同步设置</summary><div className="cc-options-fields"><Field label="每次最多同步"><select className={input} value={limit} onChange={event=>setLimit(event.target.value)}>{[5,10,20].map(value=><option key={value} value={value}>{value} 条</option>)}</select></Field><label><input type="checkbox" checked={download} onChange={event=>setDownload(event.target.checked)}/>保存原视频 / 图片</label><button className="cc-text-button" onClick={()=>navigateTo('/settings?tab=processing')}>新内容自动生成笔记设置 <ChevronRight size={14}/></button></div></details>
      </Dialog>}
      {source.busy&&!source.data&&<Notice>正在读取来源…</Notice>}
      {!source.busy&&!scopes.length&&!source.error&&<div className="cc-source-empty"><Folder size={24}/><h3>还没有同步来源</h3><p>添加喜欢、收藏、收藏夹或博主，然后点击“立即同步”。</p><button className={primary} aria-haspopup="dialog" onClick={openAdd}>添加第一个来源</button></div>}
      {scopes.map(scope=><SourceResultCard key={scope.scope_id} scope={scope} disabled={writing} canSync={canSyncSource(scope,connected)&&source.data?.worker?.online===true} canAutomate={(scope.kind==='creator'||connected)&&scope.account_matches!==false} onContent={()=>navigateTo(libraryUrl(scope.kind,scope.scope_id))} onHistory={()=>setHistory(scope)} onEdit={()=>setEdit({...scope})} onSync={()=>syncOne(scope)} onTimer={()=>timer(scope)} onRemove={()=>setRemove(scope)}/>)}
      {next!=null&&<div className="cc-source-pagination"><button className={button} disabled={action.busy||source.busy} onClick={()=>action.run(async()=>{const data=await managementApi(api,'sources',{offset:next});setMore(old=>[...old,...data.scopes]);setNext(data.next_offset)})}>加载更多来源</button></div>}
    </section>
    {edit&&<Dialog title="同步设置" onClose={()=>setEdit(null)} busy={action.busy}><Field label="每次同步数量"><input className={input} type="number" min={1} max={20} value={edit.limit} onChange={event=>setEdit({...edit,limit:event.target.value})}/></Field><label className="flex gap-2 text-sm"><input type="checkbox" checked={edit.download} onChange={event=>setEdit({...edit,download:event.target.checked})}/>保存原视频 / 图片</label><Notice error>{action.error}</Notice><p className="text-sm text-zinc-500">保存后自动同步暂停，可在来源卡片重新开启。</p><button className={primary} disabled={action.busy} onClick={editSave}>保存设置</button></Dialog>}
    {remove&&<Dialog title="移除同步来源" onClose={()=>setRemove(null)} busy={action.busy}><p>移除“{remove.title||labels[remove.kind]}”后，将停止此来源的自动同步。已保存的资料和历史记录保留，需要时可以重新添加来源。</p><Notice error>{action.error}</Notice><div className="cc-result-actions"><button className={button} disabled={action.busy} onClick={()=>setRemove(null)}>保留来源</button><button className={primary} disabled={action.busy} onClick={removeSource}>移除来源</button></div></Dialog>}
    {history&&<SourceHistory api={api} scope={history} onClose={()=>setHistory(null)}/>}
  </Page>;
}

export function SourceResultCard({scope,disabled,canSync,canAutomate,onContent,onHistory,onSync,onTimer,onEdit,onRemove}){
  const result=sourceResult(scope),report=scope.latest_report,media=scope.download_report,Icon=sourceIcons[scope.kind]||Folder;
  const count=report?.committed_count??scope.committed_count,interval=scope.timer?.interval_minutes||60;
  const automatic=scope.timer?.enabled,paused=automatic&&!!scope.timer?.blocked_job_id;
  const mode=paused?'自动同步暂停，需处理上次问题':automatic?(interval===60?'每小时自动同步':'每 '+interval+' 分钟自动同步'):'仅手动同步';
  function menuAction(event,operation){event.currentTarget.closest('details')?.removeAttribute('open');operation();}
  return <article className="cc-sync-result cc-source-card" aria-label={scope.title||labels[scope.kind]}>
    <div className="cc-source-card-line">
      <div className="cc-sync-result-main"><div className="cc-sync-result-heading"><Icon size={18}/><h3>{scope.title||labels[scope.kind]}</h3><span className={'cc-status cc-status-'+result.tone}>{result.label}</span></div><p className="cc-source-mode">{mode}</p></div>
      <div className="cc-result-actions"><label className="cc-source-auto"><span>自动同步</span><button type="button" role="switch" className="cc-source-toggle" aria-label={(scope.title||labels[scope.kind])+'自动同步'} aria-checked={!!automatic} disabled={disabled||(!automatic&&!canAutomate)} onClick={onTimer}><span/></button></label><button className={button} disabled={disabled||!canSync} onClick={onSync}><RefreshCw size={14}/>{scope.pending_count?'正在同步':'立即同步'}</button><details className="cc-source-more"><summary aria-label={(scope.title||labels[scope.kind])+'更多操作'}><MoreHorizontal size={16}/>更多</summary><div>
        <button onClick={event=>menuAction(event,onContent)}><Bookmark size={15}/>查看内容</button>
        <button onClick={event=>menuAction(event,onHistory)}><History size={15}/>同步记录{scope.history_count?' '+scope.history_count:''}</button>
        <button disabled={disabled||scope.pending_count>0} onClick={event=>menuAction(event,onEdit)}><Settings2 size={15}/>同步设置</button>
        <button className="cc-menu-danger" disabled={disabled||scope.pending_count>0} onClick={event=>menuAction(event,onRemove)}><Trash2 size={15}/>移除来源</button>
      </div></details></div>
    </div>
    <p className="cc-source-outcome">{count!=null?'本轮 '+count+' 条已保存':'尚未同步'}{report?.new_count>0?' · '+report.new_count+' 条新增':''}{report?.existing_count>0?' · '+report.existing_count+' 条已有内容':''}{media?' · '+media.saved_count+' 条原媒体已保存':''}{media?.failed_count?' · '+media.failed_count+' 条原媒体未保存':''}</p>
    {media?.preparation_pending_count>0&&<p className="cc-inline-help">本轮有 {media.preparation_pending_count} 条未完成提取准备，可在内容任务查看。</p>}
    {report?.limit_reached&&<p className="cc-inline-help">本轮仅检查最近 {report.limit} 条。</p>}
    {(scope.latest_job?.error_code||media?.failed_count>0)&&<details className="cc-result-warning"><summary>查看原因</summary><p>{media?.error_message||report?.error_message||'这一轮尚未完成，请查看同步记录。'}</p></details>}
    {scope.account_matches===false&&<p className="cc-inline-help">这个来源属于之前连接的账号。</p>}
  </article>;
}

function SourceHistory({api,scope,onClose}){
  const data=useData(api,'/v1/management/source-history',{scope_id:scope.scope_id});
  const [more,setMore]=useState([]),action=useAction();
  const rows=[...(data.data?.runs||[]),...more],next=more.length?more.at(-1).next:data.data?.next_offset;
  return <Dialog title={scope.title+' · 同步记录'} onClose={onClose}><Notice error>{data.error||action.error}</Notice>
    {data.busy&&!data.data&&<Notice>正在读取各次同步…</Notice>}
    {rows.map(run=><article className="cc-sync-history-row" key={run.job_id}><div><time>{new Date(run.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}</time><strong>{({succeeded:'本轮完成',partial:'有未完成项',failed:'失败',blocked:'需处理',queued:'已排队',running:'同步中',cancelled:'已停止'})[run.state]}</strong></div><p>{run.committed_count} 条入库 · 本轮上限 {run.limit} 条{run.download?` · ${run.saved_count} 条原媒体已保存`:''}{run.failed_count?` · ${run.failed_count} 条未保存`:''}</p>{run.error_message&&<p className="cc-sync-history-error">{run.error_message}</p>}</article>)}
    {data.data&&!rows.length&&<p>还没有同步记录。</p>}
    {next!=null&&<button className={button} disabled={action.busy} onClick={()=>action.run(async()=>{const page=await managementApi(api,'source-history',{scope_id:scope.scope_id,offset:next});setMore(old=>[...old,...page.runs.map(row=>({...row,next:page.next_offset}))])})}>更早的记录</button>}
  </Dialog>;
}
