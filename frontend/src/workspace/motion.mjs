export const SCENE_MS=420;
export const EXIT_MS=220;

// Gentle, symmetric acceleration/deceleration; reverse travel has the same rhythm.
// Same cubic-bezier(.35,0,.65,1) as cards and panels.
export function sceneProgress(elapsed){
  const x=Math.max(0,Math.min(1,elapsed/SCENE_MS));
  if(x===0||x===1)return x;
  const bezier=(t,a,b)=>3*(1-t)**2*t*a+3*(1-t)*t*t*b+t**3;
  let low=0,high=1;
  for(let i=0;i<14;i++){const mid=(low+high)/2;if(bezier(mid,.35,.65)<x)low=mid;else high=mid;}
  return bezier((low+high)/2,0,1);
}
export function sceneTravelProgress(elapsed,reverse=false){
  return reverse?1-sceneProgress(SCENE_MS-elapsed):sceneProgress(elapsed);
}
export function sceneTarget(layout,opened){
  return {...layout,leafOpacity:opened?1:0,core:{...layout.core},nodes:layout.nodes.map(node=>({...node,
    meshX:node.x-(opened?(layout.compact?60:91):0),meshY:node.y-(opened?0:64),meshR:opened?(layout.compact?20:29):46,
  }))};
}
export function interpolateScene(from,to,t){
  const mix=(a,b)=>a+(b-a)*t;
  return {...to,leafOpacity:mix(from.leafOpacity,to.leafOpacity),leaf:{...to.leaf,x:mix(from.leaf.x,to.leaf.x),top:mix(from.leaf.top,to.leaf.top),width:mix(from.leaf.width,to.leaf.width)},core:Object.fromEntries(['x','y','r'].map(k=>[k,mix(from.core[k],to.core[k])])),nodes:to.nodes.map((node,i)=>({...node,...Object.fromEntries(['x','y','meshX','meshY','meshR'].map(k=>[k,mix(from.nodes[i][k],node[k])]))}))};
}
export function workspaceExitKind(from,to){
  if(from.page!=='workspace'||!from.module)return '';
  if(to.page==='workspace'&&!to.module)return 'overview';
  if(to.page!=='workspace'||to.module!==from.module)return 'scene';
  return from.id&&!to.id?'reader':'';
}
// A newer navigation cancels the old removal; browser Back uses the same path.
export function routeTransition({read,commit,phase,reduced,schedule=setTimeout,cancel=clearTimeout}){
  let shown=read(),timer;
  return {change(){
    cancel(timer);
    const next=read(),kind=reduced()?'':workspaceExitKind(shown,next);
    const finish=()=>{shown=next;phase('');commit(next);};
    if(!kind){finish();return;}
    phase(kind);timer=schedule(finish,kind==='overview'||kind==='reader'?SCENE_MS:EXIT_MS);
  },dispose(){cancel(timer);}};
}
