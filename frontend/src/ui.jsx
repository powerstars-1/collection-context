import { useEffect, useRef, useState } from 'react';

const buttonBase='cc-button inline-flex items-center justify-center rounded-xl border px-4 py-2 text-sm transition active:scale-[0.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 disabled:cursor-not-allowed';
export const button=buttonBase+' cc-button--secondary border-zinc-200 bg-white text-zinc-700 enabled:hover:bg-zinc-50 disabled:opacity-45';
// Keep variant colors separate: Tailwind stylesheet order, not class order, wins conflicts.
export const primary=buttonBase+' cc-button--primary border-blue-700 bg-blue-700 text-white enabled:hover:bg-blue-800 disabled:border-zinc-200 disabled:bg-zinc-200 disabled:text-zinc-600';
export const input='cc-input w-full rounded-xl border border-zinc-200 bg-white px-3 py-2.5 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50';
export function Notice({error,children}) {return children?<div role={error?'alert':'status'} className={'rounded-xl px-4 py-3 text-sm leading-6 '+(error?'bg-rose-50 text-rose-700':'bg-zinc-50 text-zinc-600')}>{children}</div>:null}
export function Card({title,action,children}) {return <section className="cc-ui-card space-y-4 rounded-3xl border border-zinc-200 bg-white p-6"><div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold text-zinc-900">{title}</h2>{action}</div>{children}</section>}
export function Field({label,children}) {return <label className="cc-field block space-y-2"><span className="block text-sm font-medium text-zinc-700">{label}</span>{children}</label>}
export function Page({title,description,action,children,className=''}) {return <div className={'cc-ui-page mx-auto w-full max-w-5xl space-y-6 px-5 py-7 sm:px-7 '+className}><header className="cc-page-heading flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-2xl font-semibold tracking-tight text-zinc-900">{title}</h1>{description&&<p className="cc-page-description">{description}</p>}</div>{action}</header>{children}</div>}
export function Dialog({title,children,onClose,busy=false,className='',id,dismissOnBackdrop=false}) {
  const ref=useRef(null);
  useEffect(()=>{const previous=document.activeElement;ref.current?.focus();return ()=>previous?.focus?.()},[]);
  function keys(event) {
    if(event.key==='Escape'&&!busy)onClose();
    if(event.key==='Tab') {const nodes=[...ref.current.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary')].filter(node=>node.getClientRects().length);if(!nodes.length)return;
      if(event.shiftKey&&document.activeElement===nodes[0]){event.preventDefault();nodes.at(-1).focus()}
      else if(!event.shiftKey&&(document.activeElement===nodes.at(-1)||document.activeElement===ref.current)){event.preventDefault();nodes[0].focus()}}
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-900/25 p-4" onKeyDown={keys} onClick={event=>{if(dismissOnBackdrop&&!busy&&event.target===event.currentTarget)onClose()}}><section ref={ref} id={id} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title} className={'cc-ui-dialog max-h-[85dvh] w-full max-w-xl space-y-5 overflow-y-auto rounded-3xl bg-white p-6 shadow-xl outline-none '+className}><div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold">{title}</h2><button className={button} disabled={busy} onClick={onClose} aria-label="关闭">关闭</button></div>{children}</section></div>
}
export function useData(api,path,payload={},{poll=false,enabled=true,method='POST',pollMs=5000}={}) {
  const [data,setData]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(true),[version,setVersion]=useState(0);
  const fingerprint=JSON.stringify(payload);
  const polling=useRef(poll);polling.current=poll;const polls=!!poll;
  useEffect(()=>{let alive=true,timer;const abort=new AbortController();
    if(!enabled){setBusy(false);setData(null);setError('');return ()=>{alive=false;abort.abort()}}
    let latest=null,waiting=false,loading=false;
    function nextDelay(){return typeof polling.current==='function'?polling.current(latest):polling.current?pollMs:false}
    async function load(){if(loading)return;if(polls&&document.hidden){waiting=true;return;}loading=true;let backoff=false;try {const value=await api(path,method==='GET'?undefined:JSON.parse(fingerprint),{signal:abort.signal});latest=value;if(alive){setData(value);setError('')}}catch(err){backoff=true;if(alive&&err.name!=='AbortError')setError(err.message)}finally{loading=false;if(alive){setBusy(false);const delay=nextDelay();waiting=!!delay;if(delay&&!document.hidden)timer=setTimeout(load,backoff?60000:delay)}}}
    function visible(){clearTimeout(timer);if(!document.hidden&&waiting)void load()}
    if(polls)document.addEventListener('visibilitychange',visible);
    setBusy(true);void load();return ()=>{alive=false;clearTimeout(timer);abort.abort();document.removeEventListener('visibilitychange',visible)};
  },[api,path,fingerprint,version,polls,enabled,method,pollMs]);
  return {data,error,busy,refresh:()=>setVersion(value=>value+1)};
}
export function useAction() {
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');const lock=useRef(false),alive=useRef(true);
  useEffect(()=>{alive.current=true;return ()=>{alive.current=false}},[]);
  async function run(operation){if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');try{return await operation()}catch(err){if(alive.current)setError(err.message)}finally{lock.current=false;if(alive.current)setBusy(false)}}
  return {busy,error,notice,setNotice,run};
}
