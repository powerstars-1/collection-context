import { useEffect, useRef, useState } from 'react';

const button='rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 transition hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-400 disabled:opacity-45';
const endpoint='/v1/management/library/';
const bytes=value=>value<1024?`${value} B`:value<1024**2?`${(value/1024).toFixed(1)} KB`:value<1024**3?`${(value/1024**2).toFixed(2)} MB`:`${(value/1024**3).toFixed(2)} GB`;

export function ItemTools({api,download,item,excluded=false,onChanged}) {
  const [preview,setPreview]=useState(null);
  const [confirmed,setConfirmed]=useState(false);
  const [mediaScope,setScope]=useState('none');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const epoch=useRef(0);
  useEffect(()=>()=>{epoch.current++},[]);
  async function perform(action) {
    const revision=++epoch.current;setBusy(true);setError('');setNotice('');
    try {
      const result=await action();
      if(revision===epoch.current)return result;
    } catch(err) {if(revision===epoch.current)setError(err.message)}
    finally {if(revision===epoch.current)setBusy(false)}
  }
  async function prepare(type) {
    setPreview(null);setConfirmed(false);
    const value=await perform(()=>api(endpoint+(type==='export'?'export-preview':'exclusion-preview'),{
      material_ref:item.material_ref,...(type==='export'?{media_scope:mediaScope}:{excluded:!excluded})}));
    if(value)setPreview({type,...value});
  }
  async function commit() {
    if(!preview||!confirmed)return;
    const value=await perform(async()=>{
      const payload={material_ref:item.material_ref,preview_token:preview.preview_token,confirmed:true};
      if(preview.type==='export') {
        const blob=await download(endpoint+'export',{...payload,media_scope:preview.media_scope});
        const url=URL.createObjectURL(blob);
        const link=document.createElement('a');link.href=url;link.download='collection-'+item.material_ref+'.zip';
        document.body.append(link);link.click();link.remove();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
        return {download:true};
      }
      return api(endpoint+'exclusion-confirm',{...payload,excluded:preview.excluded});
    });
    if(value) {
      setPreview(null);setConfirmed(false);
      if(value.download)setNotice('导出已交给浏览器下载。这是单资料包，不是整库备份。');
      else onChanged();
    }
  }
  return <section aria-label="资料管理" className="rounded-2xl border border-zinc-200 bg-white p-4">
    <h3 className="text-sm font-semibold text-zinc-900">资料管理</h3>
    <p className="mt-2 text-xs leading-6 text-zinc-500">排除会隐藏搜索、读取和兴趣线索，文件仍保留；恢复会重新索引。操作不调用模型。</p>
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <button type="button" className={button} disabled={busy} onClick={()=>prepare('exclusion')}>{excluded?'预览恢复':'预览排除'}</button>
      {!excluded && <>
        <label className="text-xs text-zinc-500">导出范围 <select aria-label="导出范围" className="rounded-lg border border-zinc-200 p-2" value={mediaScope} disabled={busy} onChange={event=>{setScope(event.target.value);setPreview(null);setConfirmed(false)}}><option value="none">文字与来源</option><option value="all">文字、来源与媒体</option></select></label>
        <button type="button" className={button} disabled={busy} onClick={()=>prepare('export')}>预览导出</button>
      </>}
    </div>
    {busy&&<p role="status" className="mt-3 text-sm text-zinc-500">正在校验资料…</p>}
    {error&&<p role="alert" className="mt-3 text-sm text-rose-700">{error}</p>}
    {notice&&<p role="status" className="mt-3 text-xs text-zinc-500">{notice}</p>}
    {preview&&<div className="mt-3 rounded-xl bg-zinc-50 p-3 text-xs leading-6 text-zinc-600">
      <p>{preview.type==='export'?`${preview.files.length} 个文件 · ${bytes(preview.total_bytes)} · 不含凭据 · ${preview.omitted_media_files} 个媒体未包含`:preview.excluded?'确认后此资料将不再提供给 AI，原文件不会删除。':'确认后此资料恢复搜索和读取，原文件不会删除。'}</p>
      {preview.type==='export'&&<details><summary className="cursor-pointer">查看导出文件清单</summary><ul className="mt-2 list-inside list-disc">{preview.files.map(file=><li key={file.name}>{file.name} · {bytes(file.bytes)}</li>)}</ul></details>}
      {preview.pending_jobs>0&&<p className="text-amber-800">库中还有 {preview.pending_jobs} 个待处理任务，请先完成或取消后再操作。</p>}
      <label className="mt-2 flex items-center gap-2"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>我已核对范围并确认操作</label>
      <div className="mt-3 flex gap-2"><button type="button" className={button} disabled={busy||!confirmed||preview.pending_jobs>0} onClick={commit}>{preview.type==='export'?'确认下载':preview.excluded?'确认排除':'确认恢复'}</button><button type="button" className={button} disabled={busy} onClick={()=>{setPreview(null);setConfirmed(false)}}>取消</button></div>
    </div>}
  </section>;
}

export function ExcludedItems({api,download,onChanged}) {
  const [open,setOpen]=useState(false);
  const [result,setResult]=useState(null);
  const [items,setItems]=useState([]);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const epoch=useRef(0);
  useEffect(()=>()=>{epoch.current++},[]);
  async function load(append=false) {
    const revision=++epoch.current;setBusy(true);setError('');
    try {
      const value=await api(endpoint+'excluded',append?{offset:result.next_offset,version:result.version}:{});
      if(revision!==epoch.current)return;
      setResult(value);setItems(old=>append?[...old,...value.items]:value.items);
    } catch(err) {if(revision===epoch.current)setError(err.message)}
    finally {if(revision===epoch.current)setBusy(false)}
  }
  return <section className="rounded-2xl border border-zinc-200 bg-white p-3">
    <button type="button" className={button} onClick={()=>{if(open){epoch.current++;setBusy(false);setOpen(false)}else{setOpen(true);void load()}}}>{open?'收起排除列表':'已排除资料'}</button>
    {open&&<div className="mt-3 space-y-3">
      {busy&&<p role="status" className="text-xs text-zinc-500">读取排除列表中…</p>}
      {error&&<p role="alert" className="text-xs text-rose-700">{error}</p>}
      {!busy&&!error&&!items.length&&<p className="text-xs text-zinc-500">目前没有排除的资料。</p>}
      {items.map(item=><div key={item.material_ref}><p className="mb-2 text-sm text-zinc-700">{item.title}</p><ItemTools api={api} download={download} item={item} excluded onChanged={()=>{void load();onChanged()}}/></div>)}
      {result?.next_offset!=null&&<button type="button" className={button} disabled={busy} onClick={()=>load(true)}>继续查看排除资料</button>}
    </div>}
  </section>;
}

export function LibrarySpace({api}) {
  const [open,setOpen]=useState(false);
  const [report,setReport]=useState(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    if(!open)return;
    let alive=true;const abort=new AbortController();setReport(null);setError('');
    api(endpoint+'overview',{}, {signal:abort.signal}).then(value=>{if(alive)setReport(value)})
      .catch(err=>{if(alive)setError(err.message)});
    return ()=>{alive=false;abort.abort()};
  },[api,open]);
  return <section className="rounded-2xl border border-zinc-200 bg-white p-3">
    <button type="button" className={button} onClick={()=>setOpen(!open)}>{open?'收起空间计量':'查看资料空间'}</button>
    {open&&<div className="mt-3 text-xs leading-6 text-zinc-500">
      {error&&<p role="alert" className="text-rose-700">{error}</p>}
      {!report&&!error&&<p role="status">读取已登记文件大小中…</p>}
      {report&&<>
        <p>可见 {report.visible_items} 条 · 已排除 {report.excluded_items} 条</p>
        <p>当前文字 {bytes(report.storage.text_bytes)} · 媒体 {bytes(report.storage.media_bytes)}</p>
        <p>资料盘可用 {bytes(report.storage.disk_free_bytes)}</p>
        <p>只计当前登记文件；不含旧版本、未登记文件、缓存和凭据。文件大小不是内容正确性核验。</p>
        {report.integrity_gaps.length>0&&<p className="text-amber-800">计量发现 {report.integrity_gaps.length} 处文件缺口，数值可能不完整。</p>}
        <p>原视频不会自动删除；排除不会释放磁盘空间。</p>
      </>}
    </div>}
  </section>;
}
