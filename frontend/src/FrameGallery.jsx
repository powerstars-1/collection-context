import { useEffect, useRef, useState } from 'react';
import { Dialog } from './ui';

const button = 'rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-50 disabled:opacity-45';

function Frame({frame,image,onOpen,onRetry}) {
  const label = frame.page_number != null ? `原图第 ${frame.page_number} 页` : `关键帧 · 约 ${frame.seconds} 秒`;
  return <figure className="rounded-2xl border border-zinc-200 p-3">
    {image?.error ? <div><p className="text-sm text-rose-700">{image.error}</p><button className={button} onClick={onRetry}>重新读取图片</button></div> : image?.src ?
      <button className="w-full" type="button" onClick={onOpen} aria-label={'打开'+label}>
        <img className="max-h-80 w-full rounded-xl object-contain" src={image.src} alt={label}/>
      </button>:<p role="status" className="text-sm text-zinc-500">正在读取图片…</p>}
    <figcaption className="mt-2 text-xs text-zinc-500">{label}</figcaption>
  </figure>;
}

function dataUrl(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片无法解码，请重新读取。'));reader.readAsDataURL(blob)})}

export function FrameGallery({api,loadImage,materialRef,initiallyOpen=false}) {
  const [open,setOpen] = useState(initiallyOpen);
  const [view,setView]=useState(null);
  const [data,setData] = useState(null);
  const [error,setError] = useState('');
  const [limit,setLimit] = useState(12);
  const [images,setImages]=useState({}),[imageRevision,setImageRevision]=useState(0);
  const imageCache=useRef({});
  useEffect(()=>{
    if(!open)return;
    setError('');setData(null);setLimit(12);setView(null);setImages({});imageCache.current={};
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
  // Image downloads use the same bounded queue as all page requests. Read
  // one at a time so opening a gallery cannot consume every backend slot.
  useEffect(()=>{
    if(!open||!data)return;
    let alive=true;const abort=new AbortController();
    async function read(){for(const frame of data.frames.slice(0,limit)){
      if(!alive)return;
      if(imageCache.current[frame.url])continue;
      let image;
      try{if(!loadImage)throw new Error('图片读取服务未连接。');const blob=await loadImage(frame.url,{signal:abort.signal});if(!alive)return;image={src:await dataUrl(blob)}}
      catch(err){if(!alive||err.name==='AbortError')return;image={error:err.message}}
      if(!alive)return;imageCache.current={...imageCache.current,[frame.url]:image};setImages(imageCache.current);
    }}
    void read();return()=>{alive=false;abort.abort()};
  },[open,data,limit,loadImage,imageRevision]);
  function retry(url){delete imageCache.current[url];setImages({...imageCache.current});setImageRevision(v=>v+1)}
  return <section className="cc-frame-gallery" aria-label="原图核对">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h3 className="text-sm font-semibold text-zinc-900">原图与关键帧</h3>
      {!initiallyOpen&&<button className={button} type="button" onClick={()=>setOpen(!open)}>{open?'收起画面':'查看已保存画面'}</button>}
    </div>
    {open && <>
      {error && <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p>}
      {!data&&!error && <p role="status" className="mt-3 text-sm text-zinc-500">读取画面引用中…</p>}
      {data && <div className="mt-4 grid gap-3 sm:grid-cols-2">{data.frames.slice(0,limit).map((frame,index)=><Frame key={frame.url} frame={frame} image={images[frame.url]} onRetry={()=>retry(frame.url)} onOpen={()=>setView(index)}/>)}</div>}
      {data&&!data.frames.length&&<p className="mt-3 text-sm text-zinc-500">尚未保存图片或关键帧。</p>}
      {data?.frames.length>limit && <button type="button" className={button+' mt-4'} onClick={()=>setLimit(limit+12)}>加载更多图片</button>}
      {view!=null&&data?.frames[view]&&<Dialog title={'图片 '+(view+1)+' / '+data.frames.length} onClose={()=>setView(null)}>{images[data.frames[view].url]?.src?<img className="max-h-[60dvh] w-full object-contain" src={images[data.frames[view].url].src} alt={'图片 '+(view+1)}/>:<p role="status">正在读取图片…</p>}<div className="flex justify-between"><button className={button} disabled={view===0} onClick={()=>setView(view-1)}>上一张</button><button className={button} disabled={view===data.frames.length-1} onClick={()=>{const next=view+1;setView(next);if(next>=limit)setLimit(next+12)}}>下一张</button></div></Dialog>}
    </>}
  </section>;
}
