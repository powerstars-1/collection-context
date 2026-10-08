import {useCallback,useEffect,useRef,useState} from 'react';
import {normalizeItem} from './presentation.mjs';
import {useActivity} from '../lib/useActivity';
import {mergeLiveJobs} from '../lib/activity.mjs';

export function useLibrary(api,{query,source,scope,selectedRef,canManage,pollJobs=true}){
  const [rows,setRows]=useState([]),[listing,setListing]=useState(null),[overview,setOverview]=useState(null),[jobs,setJobs]=useState([]);
  const [selected,setSelected]=useState(null),[busy,setBusy]=useState(true),[error,setError]=useState(''),[detailError,setDetailError]=useState(''),[revision,setRevision]=useState(0);
  const epoch=useRef(0),listingRef=useRef(null),loaded=useRef(false),jobSignature=useRef(null);
  const activity=useActivity(api,canManage&&pollJobs);
  const refresh=useCallback(()=>setRevision(v=>v+1),[]);
  const load=useCallback(async(append=false)=>{
    const token=++epoch.current;setBusy(true);setError('');
    try{
      const prior=listingRef.current;
      const result=await api(query.trim()?'/v1/collections/search':'/v1/collections/list',{limit:20,filters:{...(source!=='all'?{source_kinds:[source]}:{}),...(scope?{scope_id:scope}:{})},...(query.trim()?{query:query.trim()}:{}),...(append&&prior?{offset:prior.next_offset,version:prior.version}:{})});
      if(token!==epoch.current)return;
      setRows(old=>append?[...old,...result.items.filter(r=>!old.some(x=>x.material_ref===r.material_ref))]:result.items);
      listingRef.current=result;setListing(result);loaded.current=true;
    }catch(err){if(token===epoch.current)setError(err.message)}finally{if(token===epoch.current)setBusy(false)}
  },[api,query,source,scope]);
  useEffect(()=>{let cancelled=false;const timer=setTimeout(()=>{if(!cancelled)void load()},query?250:0);return()=>{cancelled=true;clearTimeout(timer);epoch.current++}},[load,revision]);
  useEffect(()=>{let alive=true;api('/v1/collections/overview').then(data=>{if(alive)setOverview(data)}).catch(err=>{if(alive)setError(err.message)});return()=>{alive=false}},[api,revision]);
  useEffect(()=>{const data=activity.data;if(!data)return;setJobs(old=>mergeLiveJobs(old,data.jobs));
    if(jobSignature.current!==null&&jobSignature.current!==data.library_revision)refresh();jobSignature.current=data.library_revision;
  },[activity.data,refresh]);
  const acceptJobs=useCallback(next=>setJobs(next),[]);
  useEffect(()=>{
    if(!selectedRef){setSelected(null);setDetailError('');return}
    let alive=true;const abort=new AbortController();setSelected(old=>old?.material_ref===selectedRef?old:null);setDetailError('');
    api('/v1/collections/'+encodeURIComponent(selectedRef)+'/status',undefined,{signal:abort.signal}).then(data=>{if(alive)setSelected(data)}).catch(err=>{if(alive&&err.name!=='AbortError')setDetailError(err.message)});
    return()=>{alive=false;abort.abort()};
  },[api,selectedRef,revision]);
  return {items:rows.map(r=>normalizeItem(r,jobs)),selected:selected?normalizeItem(selected,jobs):null,listing,overview,jobs,busy,error,detailError,refresh,acceptJobs,loadMore:()=>load(true),firstLoad:!loaded.current};
}

export function useArtifact(api,ref,artifact,revision=0,knownState){
  const [result,setResult]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const epoch=useRef(0),current=useRef(null);
  const load=useCallback(async(append=false)=>{
    if(!ref)return;
    const token=++epoch.current;setBusy(true);setError('');if(!append){setResult(null);current.current=null}
    if(['missing','not_applicable'].includes(knownState)){setResult({text:'',state:knownState,next_offset:null,total_chars:0,_readingKey:ref+':'+artifact});setBusy(false);return}
    try{
      const previous=current.current;
      const data=await api('/v1/collections/read',{material_ref:ref,artifact,max_chars:20000,...(append&&previous?{offset:previous.next_offset,version:previous.version}:{})});
      if(token!==epoch.current)return;
      const next={...data,text:append?(previous?.text||'')+data.text:data.text,_readingKey:ref+':'+artifact};current.current=next;setResult(next);
    }catch(err){if(token===epoch.current)setError(err.message)}finally{if(token===epoch.current)setBusy(false)}
  },[api,ref,artifact,knownState]);
  useEffect(()=>{void load();return()=>{epoch.current++}},[load,revision]);
  const matches=result?result._readingKey===ref+':'+artifact:true;
  return {result:matches?result:null,busy:busy||!matches,error,loadMore:()=>load(true),reload:()=>load()};
}
