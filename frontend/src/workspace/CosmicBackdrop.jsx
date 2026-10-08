import {useEffect,useRef} from 'react';
import {nebulaPixels,skyStars,drawSky} from './space-art.mjs';

export function CosmicBackdrop({tone='workspace',paused=false}){
  const ref=useRef(null),live=useRef({tone,paused});live.current={tone,paused};
  const repaint=useRef(null);
  useEffect(()=>{
    const canvas=ref.current,container=canvas.parentElement,ctx=canvas.getContext('2d');
    if(!ctx)return;
    const texture=document.createElement('canvas'),sky=texture.getContext('2d'),reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
    let width=0,height=0,stars=[],frame=0,time=0,previous=0,lastPaint=0;
    const pointer={x:0,y:0},offset={x:0,y:0};
    function render(stamp){
      frame=0;if(document.hidden||!width||!height)return;
      const animate=!live.current.paused&&!reduced.matches&&live.current.tone==='workspace';
      const delta=previous?Math.min(80,stamp-previous):0;previous=stamp;
      if(animate)time+=delta/1000;
      if(stamp-lastPaint>=32||!animate){
        const started=performance.now();
        offset.x+=((reduced.matches?0:pointer.x)-offset.x)*.08;offset.y+=((reduced.matches?0:pointer.y)-offset.y)*.08;
        drawSky(ctx,texture,stars,width,height,time,offset);lastPaint=stamp;
        canvas.dataset.paintMs=(performance.now()-started).toFixed(2);
        canvas.dataset.skyTime=time.toFixed(3);
      }
      if(animate)frame=requestAnimationFrame(render);
    }
    function schedule(){previous=0;if(frame)cancelAnimationFrame(frame);frame=requestAnimationFrame(render)}
    function resize(){
      width=container.clientWidth;height=container.clientHeight;if(!width||!height)return;
      const dpr=Math.min(window.devicePixelRatio||1,1.5);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);
      texture.width=Math.min(440,Math.round(width/3));texture.height=Math.max(120,Math.round(texture.width*height/width));
      const image=sky.createImageData(texture.width,texture.height);image.data.set(nebulaPixels(texture.width,texture.height));sky.putImageData(image,0,0);
      stars=skyStars(width,height);canvas.dataset.starCount=String(stars.length);schedule();
    }
    function move(event){if(reduced.matches||live.current.paused||live.current.tone!=='workspace')return;const rect=container.getBoundingClientRect();pointer.x=(event.clientX-rect.left)/width*2-1;pointer.y=(event.clientY-rect.top)/height*2-1}
    function leave(){pointer.x=0;pointer.y=0}
    function visibility(){if(document.hidden){cancelAnimationFrame(frame);frame=0;previous=0}else schedule()}
    const observer=new ResizeObserver(resize);observer.observe(container);resize();repaint.current=schedule;
    container.addEventListener('pointermove',move,{passive:true});container.addEventListener('pointerleave',leave);
    document.addEventListener('visibilitychange',visibility);reduced.addEventListener('change',schedule);
    return()=>{repaint.current=null;cancelAnimationFrame(frame);observer.disconnect();container.removeEventListener('pointermove',move);container.removeEventListener('pointerleave',leave);document.removeEventListener('visibilitychange',visibility);reduced.removeEventListener('change',schedule)};
  },[]);
  useEffect(()=>{repaint.current?.()},[tone,paused]);
  return <canvas ref={ref} className="cosmic-backdrop" data-tone={tone} aria-hidden="true"/>;
}
