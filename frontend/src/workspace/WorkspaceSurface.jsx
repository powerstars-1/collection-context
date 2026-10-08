import {useLayoutEffect,useRef,useState} from 'react';
import {sceneLayout} from './scene.mjs';

export function ScenePanel({canvas,children}){
  return !children?null:canvas?<div className="workspace-panel">{children}</div>:children;
}

// One fixed canvas and floating content columns avoid a second flexbox resize
// animation fighting the scene's own geometry interpolation.
export function WorkspaceSurface({canvas,selection,detail,exit,children}){
  const element=useRef(null),[size,setSize]=useState(null);
  useLayoutEffect(()=>{
    if(!canvas)return;
    const measure=()=>{
      const el=element.current,css=getComputedStyle(el),left=parseFloat(css.paddingLeft),right=parseFloat(css.paddingRight);
      setSize({width:el.clientWidth-left-right,height:el.clientHeight,left});
    };
    measure();const observer=new ResizeObserver(measure);observer.observe(element.current);
    return()=>observer.disconnect();
  },[canvas]);
  const layout=size&&sceneLayout(size.width,size.height,true,detail);
  return <div ref={element} className={'library '+(canvas?'neural-library ':'')+(selection?'has-selection':'')}
    data-workspace-exit={exit||undefined} inert={exit?true:undefined}
    style={canvas&&layout?{'--workspace-panel-x':`${size.left+layout.panel.x}px`,'--workspace-panel-width':`${layout.panel.width}px`}:undefined}>
    {children}
  </div>;
}
