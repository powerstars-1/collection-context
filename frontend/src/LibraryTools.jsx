import { useEffect, useRef, useState } from 'react';
import { Dialog } from './ui';

const button='rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 transition hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-400 disabled:opacity-45';
const endpoint='/v1/management/library/';
const artifactNames={original:'原文',audio:'音频转写',screen:'画面文字',image:'图片提取',summary:'内容总结',user_note:'用户备注',entry:'素材入口卡（保存为备注）'};
const bytes=value=>value<1024?`${value} B`:value<1024**2?`${(value/1024).toFixed(1)} KB`:value<1024**3?`${(value/1024**2).toFixed(2)} MB`:`${(value/1024**3).toFixed(2)} GB`;

export function ItemTools({api,download,item,excluded=false,onChanged,initiallyOpen=false}) {
  const [preview,setPreview]=useState(null);
  const [confirmed,setConfirmed]=useState(false);
  const [mediaScope,setScope]=useState('none');
  const [editKind,setEditKind]=useState('screen');
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
    const value=await perform(()=>api(endpoint+(type==='summary'?'summary-preview':type==='edit'?'edit-preview':type==='export'?'export-preview':'exclusion-preview'),{
      material_ref:item.material_ref,...(type==='summary'?{}:type==='edit'?{artifact:editKind}:type==='export'?{media_scope:mediaScope}:{excluded:!excluded})}));
    if(value)setPreview({type,...value,...(type==='summary'?{idempotency_key:'owner-summary-'+crypto.randomUUID()}:{})});
  }
  async function commit() {
    if(!preview)return;
    const value=await perform(async()=>{
      const payload={material_ref:item.material_ref,preview_token:preview.preview_token,confirmed:true};
      if(preview.type==='summary')return api(endpoint+'summary-confirm',{material_ref:item.material_ref,preview_token:preview.preview_token,idempotency_key:preview.idempotency_key,fee_confirmed:true});
      if(preview.type==='edit')return api(endpoint+'edit-confirm',{...payload,artifact:preview.artifact});
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
      if(value.download)setNotice('文件包已下载。');
      else onChanged();
    }
  }
  return <details open={initiallyOpen||undefined} aria-label="资料操作" className="rounded-2xl border border-zinc-200 bg-white p-4">
    <summary className="cursor-pointer text-sm font-medium text-zinc-700">{excluded?'恢复资料':'更多'}</summary>
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <button type="button" className={button} disabled={busy} onClick={()=>prepare('exclusion')}>{excluded?'恢复':'从收藏库隐藏'}</button>
      {!excluded && <>
        <label className="text-xs text-zinc-500">导出范围 <select aria-label="导出范围" className="rounded-lg border border-zinc-200 p-2" value={mediaScope} disabled={busy} onChange={event=>{setScope(event.target.value);setPreview(null);setConfirmed(false)}}><option value="none">文字与来源</option><option value="all">文字、来源与媒体</option></select></label>
        <button type="button" className={button} disabled={busy} onClick={()=>prepare('export')}>导出</button>
        <button type="button" className={button} disabled={busy} onClick={()=>prepare('summary')}>重新生成总结</button>
        <details><summary className="cursor-pointer text-sm text-zinc-500">高级</summary>
        <label className="text-xs text-zinc-500">核对正文 <select aria-label="核对正文类型" className="rounded-lg border border-zinc-200 p-2" value={editKind} disabled={busy} onChange={event=>{setEditKind(event.target.value);setPreview(null);setConfirmed(false)}}>{Object.entries(artifactNames).map(([kind,name])=><option key={kind} value={kind}>{name}</option>)}</select></label>
        <button type="button" className={button} disabled={busy} onClick={()=>prepare('edit')}>接纳本地修改</button></details>
      </>}
    </div>
    {busy&&<p role="status" className="mt-3 text-sm text-zinc-500">正在校验资料…</p>}
    {error&&<p role="alert" className="mt-3 text-sm text-rose-700">{error}</p>}
    {notice&&<p role="status" className="mt-3 text-xs text-zinc-500">{notice}</p>}
    {preview&&<Dialog title={preview.type==='summary'?'重新生成总结':preview.type==='export'?'导出资料':preview.type==='edit'?'接纳本地修改':preview.excluded?'隐藏资料':'恢复资料'} onClose={()=>setPreview(null)} busy={busy}>
      <p className="text-sm leading-6 text-zinc-600">{preview.type==='summary'?`使用 ${preview.model}，只重做总结，最多 1 次模型请求；费用以模型服务为准。`:preview.type==='edit'?preview.changed?'保存修改并更新索引。':'正文未发生变化。':preview.type==='export'?`${preview.files.length} 个文件 · ${bytes(preview.total_bytes)} · ${preview.omitted_media_files} 个媒体未包含`:preview.excluded?'隐藏后不再出现在收藏库或 AI 检索中，文件会保留。':'恢复后重新出现在收藏库和 AI 检索中。'}</p>
      {preview.type==='summary'&&<>
        <p>使用已保存证据：{preview.source_artifacts.map(kind=>artifactNames[kind]).join('、')||'仅原文'}；仅替换总结与可读内容，用户备注不上传。</p>
        {preview.missing_or_stale.length>0&&<p className="text-amber-800">仍有 {preview.missing_or_stale.length} 类证据缺失或过期，新总结会保留这些缺口。</p>}
      </>}
      {preview.type==='edit'&&<>
        {preview.warning&&<p className="text-amber-800">{preview.warning}</p>}
        <p>以下是修改后正文；原文件已被外部编辑，无法提供可信的修改前逐行对比。</p>
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-zinc-200 bg-white p-3">{preview.text_preview}</pre>
        {preview.preview_truncated&&<p>这里只显示前 2000 字；请在本地核对全文后确认。</p>}
        <p>将标为过期：{preview.invalidated_artifacts.map(kind=>artifactNames[kind]).join('、')||'无'}。人工接纳不代表识别准确率已验证；重新提取不能直接覆盖人工修改。</p>
      </>}
      {preview.type==='export'&&<details><summary className="cursor-pointer">查看导出文件清单</summary><ul className="mt-2 list-inside list-disc">{preview.files.map(file=><li key={file.name}>{file.name} · {bytes(file.bytes)}</li>)}</ul></details>}
      {preview.pending_jobs>0&&<p className="text-amber-800">库中还有 {preview.pending_jobs} 个待处理任务，请先完成或取消后再操作。</p>}
      <div className="mt-3 flex gap-2"><button type="button" className={button} disabled={busy||preview.pending_jobs>0||(preview.type==='edit'&&!preview.changed)} onClick={commit}>{preview.type==='summary'?'确认生成':preview.type==='edit'?'接纳修改':preview.type==='export'?'下载文件包':preview.excluded?'隐藏':'恢复'}</button><button type="button" className={button} disabled={busy} onClick={()=>{setPreview(null);setConfirmed(false)}}>取消</button></div>
      {error&&<p role="alert" className="text-sm text-rose-700">{error}</p>}
    </Dialog>}
  </details>;
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
    <button type="button" className={button} onClick={()=>{if(open){epoch.current++;setBusy(false);setOpen(false)}else{setOpen(true);void load()}}}>{open?'收起':'查看已隐藏资料'}</button>
    {open&&<div className="mt-3 space-y-3">
      {busy&&<p role="status" className="text-xs text-zinc-500">读取排除列表中…</p>}
      {error&&<p role="alert" className="text-xs text-rose-700">{error}</p>}
      {!busy&&!error&&!items.length&&<p className="text-xs text-zinc-500">目前没有隐藏资料。</p>}
      {items.map(item=><div key={item.material_ref}><p className="mb-2 text-sm text-zinc-700">{item.title}</p><ItemTools api={api} download={download} item={item} excluded onChanged={()=>{void load();onChanged()}}/></div>)}
      {result?.next_offset!=null&&<button type="button" className={button} disabled={busy} onClick={()=>load(true)}>加载更多</button>}
    </div>}
  </section>;
}

export function LibrarySpace({api,initiallyOpen=false}) {
  const [open,setOpen]=useState(initiallyOpen);
  const [revision,setRevision]=useState(0);
  const [report,setReport]=useState(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    if(!open)return;
    let alive=true;const abort=new AbortController();setReport(null);setError('');
    api(endpoint+'overview',{}, {signal:abort.signal}).then(value=>{if(alive)setReport(value)})
      .catch(err=>{if(alive)setError(err.message)});
    return ()=>{alive=false;abort.abort()};
  },[api,open,revision]);
  return <section className="rounded-2xl border border-zinc-200 bg-white p-3">
    <div className="flex items-center justify-between"><h2 className="font-medium">资料空间</h2><button type="button" className={button} onClick={()=>{setOpen(true);setRevision(old=>old+1)}}>刷新空间</button></div>
    {open&&<div className="mt-3 text-xs leading-6 text-zinc-500">
      {error&&<p role="alert" className="text-rose-700">{error}</p>}
      {!report&&!error&&<p role="status">读取已登记文件大小中…</p>}
      {report&&<>
        <p>可见 {report.visible_items} 条 · 已排除 {report.excluded_items} 条</p>
        <p>当前文字 {bytes(report.storage.text_bytes)} · 媒体 {bytes(report.storage.media_bytes)}</p>
        <p>资料盘可用 {bytes(report.storage.disk_free_bytes)}</p>
        <details><summary>计量范围</summary><p>当前登记的文字与媒体；不含旧版本和缓存。</p></details>
        {report.integrity_gaps.length>0&&<p className="text-amber-800">计量发现 {report.integrity_gaps.length} 处文件缺口，数值可能不完整。</p>}
        <p>原视频保留，隐藏资料不会释放空间。</p>
      </>}
    </div>}
  </section>;
}
