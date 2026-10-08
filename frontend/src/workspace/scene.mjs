// Module membership only; the canvas is not an implemented semantic graph.
import {modules} from './structure.mjs';
export const categories = modules;
export function moduleCount(items,id){return id==='library'?items.length:null;}
export function sceneLayout(width,height,expanded,detail=false,controlsHeight=92){
  const w=Math.max(320,width),h=Math.max(400,height);
  const compact=w<650,top=Math.max(compact?145:120,h*.20),bottom=h-112;
  const overview=compact?[[.29,.19],[.73,.23],[.79,.48],[.73,.74],[.26,.72],[.20,.43]]:[[.37,.18],[.72,.23],[.85,.51],[.69,.77],[.30,.77],[.16,.45]];
  // Panels share the full canvas coordinates; opening one never resizes it.
  const three=detail&&w>850;
  const readerWidth=Math.min(490,Math.max(350,w*.31)),readerX=w-readerWidth;
  const leafWidth=three?Math.min(520,Math.max(250,w*.28)):w*(compact?.48:.45);
  const leafX=three?readerX-24-leafWidth:w*(compact?.50:.53);
  const controlsTop=w<760?57:77;
  return {
    core:{x:expanded?w*(three?.065:.09):w*.51,y:expanded?(top+bottom)/2:h*.48,r:expanded?Math.min(72,w*.09):Math.min(w*.175,h*.26)},
    nodes:categories.map((category,index)=>({...category,
      x:expanded?(compact?w*.25:three?Math.max(170,Math.min(w*.24,leafX*.60)):Math.max(195,w*.29)):w*overview[index][0],
      y:expanded?top+(bottom-top)*index/(categories.length-1):h*overview[index][1],
    })),
    leaf:{x:leafX,top:controlsTop+controlsHeight+18+30,row:88,width:leafWidth},
    controlsTop,
    panel:{x:detail?readerX:leafX,width:detail?readerWidth:leafWidth},
    compact,
  };
}
