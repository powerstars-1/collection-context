import { useEffect, useState } from 'react';

const button = 'rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-50 disabled:opacity-45';

function Frame({frame}) {
  const [failed,setFailed] = useState(false);
  const label = frame.page_number != null ? `原图第 ${frame.page_number} 页` : `关键帧 · 约 ${frame.seconds} 秒`;
  return <figure className="rounded-2xl border border-zinc-200 p-3">
    {failed ? <p className="text-sm text-rose-700">这张图暂时无法读取，请刷新核对版本或文件状态。</p> :
      <a href={frame.url} target="_blank" rel="noopener noreferrer" aria-label={'打开'+label}>
        <img className="max-h-80 w-full rounded-xl object-contain" src={frame.url} alt={label} loading="lazy" onError={()=>setFailed(true)}/>
      </a>}
    <figcaption className="mt-2 text-xs text-zinc-500">{label} · {frame.frame_id}</figcaption>
  </figure>;
}

export function FrameGallery({api,materialRef}) {
  const [open,setOpen] = useState(false);
  const [data,setData] = useState(null);
  const [error,setError] = useState('');
  const [limit,setLimit] = useState(12);
  useEffect(()=>{
    if(!open)return;
    setError('');setData(null);setLimit(12);
    let alive=true;
    const abort=new AbortController();
    api(`/v1/collections/${encodeURIComponent(materialRef)}/frames`,undefined,{signal:abort.signal})
      .then(value=>{
        if(!alive)return;
        const prefix=`/v1/collections/${materialRef}/frames/${value.input_id}/`;
        if(!/^[A-Za-z0-9_-]{1,160}$/.test(value.input_id) || !Array.isArray(value.frames) || value.frames.length>240 ||
          value.frames.some(frame=>!/^f_[A-Za-z0-9_-]+$/.test(frame.frame_id) || frame.url!==prefix+frame.frame_id)) {
          throw new Error('画面引用格式不正确，未加载外部资源。');
        }
        setData(value);
      }).catch(err=>{if(alive)setError(err.message)});
    return ()=>{alive=false;abort.abort()};
  },[api,materialRef,open]);
  return <section className="rounded-3xl border border-zinc-200 bg-white p-6" aria-label="原图核对">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h3 className="text-sm font-semibold text-zinc-900">原图与关键帧</h3>
      <button className={button} type="button" onClick={()=>setOpen(!open)}>{open?'收起画面':'查看已保存画面'}</button>
    </div>
    <p className="mt-2 text-xs leading-6 text-zinc-500">只读取已保存的图片，不下载作品、不调用模型。关键帧时间是采样位置，不能证明整段视频没有遗漏。</p>
    {open && <>
      {error && <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p>}
      {!data&&!error && <p role="status" className="mt-3 text-sm text-zinc-500">读取画面引用中…</p>}
      {data && <div className="mt-4 grid gap-3 sm:grid-cols-2">{data.frames.slice(0,limit).map(frame=><Frame key={frame.url} frame={frame}/>)}</div>}
      {data?.frames.length>limit && <button type="button" className={button+' mt-4'} onClick={()=>setLimit(limit+12)}>继续看画面</button>}
    </>}
  </section>;
}
