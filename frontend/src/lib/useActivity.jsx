import {useEffect,useState} from 'react';
import {createActivityMonitor} from './activity.mjs';
const monitors=new WeakMap();
function monitor(api){if(!monitors.has(api))monitors.set(api,createActivityMonitor(api));return monitors.get(api)}
export function useActivity(api,enabled=true){
  const [state,setState]=useState({data:null,error:''});
  useEffect(()=>enabled?monitor(api).subscribe(setState):undefined,[api,enabled]);
  return {...state,refresh:()=>enabled&&monitor(api).refresh()};
}
