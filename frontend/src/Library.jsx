// MaterialLibrary business adapter; original WorkbenchLayout and MaterialList are reused.
import { useEffect, useRef, useState } from 'react';
import { WorkbenchLayout } from './components/workbench/WorkbenchLayout';
import { MaterialList } from './components/workbench/MaterialList';
import { FrameGallery } from './FrameGallery';
import { ItemTools, ExcludedItems, LibrarySpace } from './LibraryTools';

export const sourceLabels = {liked:'喜欢',saved:'收藏',collection:'收藏夹',creator:'博主作品',link:'单条链接'};
const artifactLabels = {original:'原文',audio:'转写',screen:'画面文字',summary:'总结',readable:'可读全文',image:'图片理解',user_note:'备注'};
const stateLabels = {ready:'已保存 · 精度需核对',missing:'尚未提取',not_applicable:'不适用',stale:'输入已变化',unavailable:'文件需核对'};
const button = 'rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 transition hover:bg-zinc-50 disabled:opacity-45';
const safeSource = (url) => {
  try {
    const parsed = new URL(url);
    if(parsed.protocol!=='https:'||parsed.username||parsed.password||parsed.port||parsed.search||parsed.hash)return null;
    const accepted = ['www.douyin.com','douyin.com'].includes(parsed.hostname)
      ? /^\/(video|note)\/\d+\/?$/.test(parsed.pathname)
      : parsed.hostname==='v.douyin.com' && /^\/[A-Za-z0-9_-]+\/?$/.test(parsed.pathname);
    return accepted ? parsed.href : null;
  } catch { return null; }
};

function SourceAction({item}) {
  const [copied,setCopied] = useState(false);
  const url = safeSource(item.source_url);
  if (!url) return null;
  return <div className="flex flex-wrap items-center gap-2">
    <a className={button} href={url} target="_blank" rel="noopener noreferrer">打开原作品</a>
    <button type="button" className={button} onClick={async()=>{try {await navigator.clipboard.writeText(url);setCopied(true)} catch {setCopied(false)}}}>{copied?'已复制':'复制来源链接'}</button>
  </div>;
}

function Evidence({api,download,item,canManage,onChanged}) {
  const [artifact,setArtifact] = useState('original');
  const [status,setStatus] = useState(null);
  const [read,setRead] = useState(null);
  const [text,setText] = useState('');
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(()=>{
    let alive = true;
    api('/v1/collections/'+encodeURIComponent(item.material_ref)+'/status')
      .then(data=>{if(alive)setStatus(data)})
      .catch(err=>{if(alive)setError(err.message)});
    return ()=>{alive=false;generation.current++};
  },[api,item.material_ref]);
  async function load(append=false) {
    const epoch = ++generation.current;
    setBusy(true);setError('');
    if (!append) {setText('');setRead(null)}
    try {
      const data = await api('/v1/collections/read',{material_ref:item.material_ref,artifact,
        ...(append && read ? {offset:read.next_offset,version:read.version}:{})});
      if (epoch !== generation.current) return;
      setRead(data);setText(old=>append?old+data.text:data.text);
    } catch(err) {if(epoch===generation.current)setError(err.message)}
    finally {if(epoch===generation.current)setBusy(false)}
  }
  useEffect(()=>{void load();return ()=>{generation.current++}},[artifact,item.material_ref]);
  return <div className="space-y-4">
    <div className="rounded-3xl border border-zinc-200 bg-white p-6">
      <div className="text-xs text-zinc-400">资料详情 · 来源与证据</div>
      <h2 className="mt-2 text-xl font-semibold text-zinc-900">{item.title || '无标题资料'}</h2>
      <p className="mt-2 text-sm text-zinc-500">{item.author || '作者未知'} · {item.media_type === 'video' ? '视频':item.media_type==='image'?'图文':'类型未知'} · 内容不自动等同于你的观点</p>
      <div className="mt-4 flex flex-wrap gap-2">{[...new Set((item.relations||[]).map(r=>r.kind))].map(kind=><span key={kind} className="rounded-lg bg-zinc-100 px-2 py-1 text-xs text-zinc-600">{sourceLabels[kind]||kind}</span>)}</div>
      <div className="mt-4"><SourceAction item={item}/></div>
      <p className="mt-4 break-all text-xs text-zinc-400">引用：{item.material_ref}</p>
    </div>
    <div className="rounded-3xl border border-zinc-200 bg-white p-6">
      <div role="tablist" aria-label="资料内容" className="flex flex-wrap gap-2">{Object.entries(artifactLabels).map(([key,label])=><button
        key={key} type="button" role="tab" aria-selected={artifact===key} onClick={()=>setArtifact(key)}
        className={artifact===key?'rounded-xl bg-zinc-900 px-3 py-2 text-sm text-white':button}>{label}</button>)}</div>
      <p className="mt-4 text-xs leading-6 text-zinc-500">{status ? Object.entries(status.artifacts).filter(([,entry])=>entry.state!=='ready').map(([key,entry])=>`${artifactLabels[key]||key}：${stateLabels[entry.state]||entry.state}`).join(' / ') : '正在读取处理状态…'}</p>
      <div role="status" aria-live="polite" className="mt-4 text-sm text-zinc-500">{busy?'读取中…':read?`${stateLabels[read.state]||read.state} · ${read.total_chars} 字符`:''}</div>
      {error && <p role="alert" className="mt-3 rounded-xl bg-rose-50 p-3 text-sm text-rose-700">{error}</p>}
      {read?.warnings?.length>0 && <p className="mt-3 text-xs leading-6 text-amber-800">{read.warnings.join(' ')}</p>}
      <pre id="evidence-text" className="mt-4 whitespace-pre-wrap break-words font-sans text-sm leading-7 text-zinc-700">{text || (!busy && !error ? '此项目前没有可读内容。缺失不代表原作品没有信息。':'')}</pre>
      {read?.next_offset!=null && <button type="button" className={button+' mt-4'} disabled={busy} onClick={()=>load(true)}>继续读取</button>}
    </div>
    {status?.layout==='legacy_douyin_readonly'?<p className="text-xs leading-6 text-zinc-500">旧库模式提供已有文字读取；原始媒体和关键帧保留在原目录。</p>:status&&<FrameGallery api={api} materialRef={item.material_ref}/>}
    {canManage&&<ItemTools api={api} download={download} item={item} onChanged={onChanged}/>}
    {read && <details className="rounded-2xl border border-zinc-200 bg-white p-4"><summary className="cursor-pointer text-sm text-zinc-500">查看当前引用响应 JSON</summary><pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs text-zinc-600">{JSON.stringify(read,null,2)}</pre></details>}
  </div>;
}

export function Library({api,download,canManage=false,sourceKind=''}) {
  const [query,setQuery] = useState(new URLSearchParams(location.search).get('q')||'');
  const [committedQuery,setCommittedQuery] = useState(query);
  const [items,setItems] = useState([]);
  const [listing,setListing] = useState(null);
  const [selectedId,setSelectedId] = useState(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const epoch = useRef(0);
  const selectEpoch = useRef(0);
  useEffect(()=>()=>{selectEpoch.current++},[]);
  async function load(append=false) {
    const revision = ++epoch.current;
    setBusy(true);setError('');
    if(!append) {setSelectedId(null);setItems([]);setListing(null)}
    const filters = sourceKind ? {source_kinds:[sourceKind]}:{};
    try {
      const data = await api(committedQuery?'/v1/collections/search':'/v1/collections/list',
        {limit:20,filters,...(committedQuery?{query:committedQuery}:{}),
          ...(append&&listing?{offset:listing.next_offset,version:listing.version}:{})});
      if(revision!==epoch.current)return;
      setItems(old=>append?[...old,...data.items]:data.items);setListing(data);
    } catch(err) {if(revision===epoch.current)setError(err.message)}
    finally {if(revision===epoch.current)setBusy(false)}
  }
  useEffect(()=>{void load();return ()=>{epoch.current++}},[committedQuery,sourceKind]);
  useEffect(()=>{
    const ref = new URLSearchParams(location.search).get('ref');
    if (!ref || !/^(?:[A-Za-z0-9_-]{1,160}|m1:[A-Za-z0-9_-]{1,1397})$/.test(ref)) return;
    const revision=++selectEpoch.current;
    api('/v1/collections/'+encodeURIComponent(ref)+'/status').then(item=>{
      if(revision!==selectEpoch.current)return;
      setItems(old=>old.some(row=>row.material_ref===ref)?old:[item,...old]);setSelectedId(ref);
    }).catch(err=>{if(revision===selectEpoch.current)setError(err.message)});
  },[api]);
  const item = items.find(row=>row.material_ref===selectedId);
  const materials = items.map(row=>({id:row.material_ref,title:row.title||'无标题资料',platform:'抖音',mediaType:row.media_type==='video'?'视频':row.media_type==='image'?'图文':'类型未知',authorName:row.author||'作者未知',
    sources:[...new Set((row.relations||[]).map(rel=>sourceLabels[rel.kind]||rel.kind))].join(' / '),
    status:row.artifact_states?Object.entries(row.artifact_states).filter(([kind,state])=>kind!=='original'&&state==='ready').map(([kind])=>artifactLabels[kind]).join(' / ')||'原文已保存 · 提取待处理':'检索已命中 · 状态见详情'}));
  return <WorkbenchLayout title="收藏库" description="从喜欢和收藏中找回教程，让你的 AI 有据可答。"
    middle={<div className="space-y-3">
      <form onSubmit={event=>{event.preventDefault();const next=query.trim();next===committedQuery?void load():setCommittedQuery(next)}} className="space-y-3">
        <input id="library-query" aria-label="搜索已有资料" className="w-full rounded-xl border border-zinc-200 px-3 py-2 text-sm" placeholder="搜索标题、作者或已提取的内容" value={query} onChange={event=>setQuery(event.target.value)}/>
        <div className="flex items-center gap-2"><button type="submit" className="rounded-xl bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-45" disabled={busy}>搜索收藏</button><button type="button" className={button} disabled={busy} onClick={()=>{setQuery('');committedQuery?setCommittedQuery(''):void load()}}>重置</button><span className="ml-auto text-xs text-zinc-400">{listing?.total_items??listing?.total_matches??'—'} 条</span></div>
      </form>
      <p className="text-xs leading-5 text-zinc-400">{listing?.layout==='legacy_douyin_readonly'?'旧资料只读浏览，按资料引用排序。处理完整性和实际点赞时间未知。':'只读已有资料，不访问抖音或请求模型。列表按首次发现排序，不能当作实际点赞时间。'}</p>
      {listing?.coverage?.partial&&<p role="status" className="rounded-xl bg-amber-50 p-3 text-sm text-amber-800">部分旧文件或附件无法读取，当前结果未覆盖全部资料。</p>}
      {error && <p role="alert" className="rounded-xl bg-rose-50 p-3 text-sm text-rose-700">{error}</p>}
      {busy && <p role="status" className="rounded-2xl border border-zinc-200 bg-zinc-50 p-4 text-sm text-zinc-500">正在读取资料…</p>}
      {!busy&&!items.length&&!error && <div className="rounded-2xl border border-dashed border-zinc-200 bg-white p-4 text-sm text-zinc-500">{listing?.layout==='legacy_douyin_readonly'?'当前范围没有匹配资料。请调整关键词或来源筛选，或核对旧目录中的素材卡。':'当前范围没有资料。换一个关键词，或先去同步页添加作品。'}</div>}
      <MaterialList materials={materials} selectedIds={selectedId?[selectedId]:[]} onToggle={setSelectedId}/>
      {listing?.next_offset!=null && <button type="button" className={button} disabled={busy} onClick={()=>load(true)}>继续查看</button>}
      {canManage&&<ExcludedItems api={api} download={download} onChanged={()=>load()}/>}
      {canManage&&<LibrarySpace api={api}/>}
    </div>}
    main={item?<Evidence key={item.material_ref} item={item} api={api} download={download} canManage={canManage} onChanged={()=>load()}/>:<div className="rounded-3xl border border-zinc-200 bg-white p-6"><div className="text-xs text-zinc-400">你的收藏上下文</div><h2 className="mt-2 text-lg font-semibold text-zinc-900">选一条资料，查看完整内容。</h2><p className="mt-3 text-sm leading-7 text-zinc-500">原文、转写、画面文字和总结分开保存与阅读。每一条回答都能回到来源，缺失项和处理状态如实显示。</p><p className="mt-4 text-xs text-zinc-400">喜欢与收藏是兴趣线索，不自动等同于你的观点或已掌握的知识。</p></div>}
  />;
}
