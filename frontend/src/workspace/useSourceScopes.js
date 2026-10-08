import {useCallback,useEffect,useState} from 'react';
import {loadSourceScopes} from '../management/sourceActions.mjs';

// Local metadata only. No browser login, folder discovery, sync or model request.
export function useSourceScopes(api,enabled){
  const [scopes,setScopes]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
  const refresh=useCallback(()=>setRevision(v=>v+1),[]);
  useEffect(()=>{
    if(!enabled)return;
    let alive=true;setBusy(true);setError('');
    loadSourceScopes(api).then(rows=>{if(alive)setScopes(rows)}).catch(err=>{if(alive)setError(err.message)}).finally(()=>{if(alive)setBusy(false)});
    return()=>{alive=false};
  },[api,enabled,revision]);
  useEffect(()=>{if(!enabled)return;window.addEventListener('collection-context-changed',refresh);return()=>window.removeEventListener('collection-context-changed',refresh)},[enabled,refresh]);
  return {scopes,error,busy,refresh};
}
