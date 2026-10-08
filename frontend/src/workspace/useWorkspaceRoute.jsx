import {useEffect,useState} from 'react';
import {SCENE_MS,routeTransition} from './motion.mjs';

export function useWorkspaceRoute(read){
  const [location,setLocation]=useState(read),[exit,setExit]=useState('');
  useEffect(()=>{
    const phase=kind=>setExit(kind);
    // CSS starts on the next painted frame, not when navigation was requested.
    // Wait for its actual end so a busy frame cannot cut off the returning panel.
    const schedule=(finish,ms)=>{
      if(ms!==SCENE_MS)return {timer:setTimeout(finish,ms)};
      const job={timer:null,listener:null};
      const done=()=>{clearTimeout(job.timer);document.removeEventListener('transitionend',job.listener);document.removeEventListener('animationend',job.listener);finish()};
      job.listener=event=>{if(event.propertyName==='transform'&&event.target.matches('.neural-stage.overview .scene-category')||event.animationName==='workspace-reader-out'&&event.target.matches('.workspace-panel'))done()};
      document.addEventListener('transitionend',job.listener);document.addEventListener('animationend',job.listener);job.timer=setTimeout(done,SCENE_MS+150);return job;
    };
    const cancel=job=>{if(!job)return;clearTimeout(job.timer);if(job.listener){document.removeEventListener('transitionend',job.listener);document.removeEventListener('animationend',job.listener)}};
    const transition=routeTransition({read,commit:setLocation,phase,schedule,cancel,reduced:()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches});
    const change=()=>transition.change();
    window.addEventListener('hashchange',change);
    return()=>{window.removeEventListener('hashchange',change);transition.dispose();};
  },[read]);
  return [location,exit];
}
