import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {ArrowLeft,ArrowRight,Bookmark,ChevronRight,Folder,Network,Layers,Lightbulb,PenLine,Pause,Play,Search,Video,X} from 'lucide-react';
import {categories,sceneLayout,moduleCount} from './scene.mjs';
import {labels} from './presentation.mjs';
import {sceneTarget,sceneTravelProgress,interpolateScene} from './motion.mjs';
import {drawSilkCore} from './core-art.mjs';
import './neural.css';

const icons={concepts:Network,methods:Layers,works:Video,ideas:Lightbulb,drafts:PenLine,library:Bookmark};
const names={all:'收藏库',saved:'我的收藏',collection:'收藏夹',creator:'博主作品',liked:'我的喜欢',link:'单条链接'};
export function NeuralWorkspace({allItems,items,selected,source,scope,folderPicker,module,onSource,onExpand,onOverview,onOpen,onLibrary,onSync,query,onQuery,filter,onFilter,initialScroll=0,onScrollPosition,returning=false,detailOpen=false,totalCount,hasMore=false,onLoadMore,busy=false,paused=false,onPauseChange}){
  const stage=useRef(null),[size,setSize]=useState(null),[scroll,setScroll]=useState(initialScroll);
  const lastFilter=useRef([source,scope,query,filter].join('|'));
  const controls=useRef(null),[controlsHeight,setControlsHeight]=useState(92);
  const opened=!!module&&!returning,library=module==='library',layout=sceneLayout(size?.width||1,size?.height||1,opened,detailOpen,controlsHeight);
  const hasExpanded=useRef(!!module),retainedLayout=useRef(layout);
  if(opened)hasExpanded.current=true;
  if(!returning)retainedLayout.current=layout;
  const leafLayout=returning?retainedLayout.current:layout;
  // Measure the final pane width before mounting cards; never animate a guessed position.
  useLayoutEffect(()=>{const measure=()=>{const {clientWidth:width,clientHeight:height}=stage.current;setSize(width&&height?{width,height}:null)};measure();const observer=new ResizeObserver(measure);observer.observe(stage.current);return()=>observer.disconnect()},[]);
  useLayoutEffect(()=>{if(!controls.current)return;const measure=()=>setControlsHeight(controls.current.offsetHeight);measure();const observer=new ResizeObserver(measure);observer.observe(controls.current);return()=>observer.disconnect()},[library,!!size]);
  useLayoutEffect(()=>{const leaves=stage.current?.querySelector('.neural-leaves');if(leaves)leaves.scrollTop=initialScroll},[library]);
  useEffect(()=>{const next=[source,scope,query,filter].join('|');if(lastFilter.current===next)return;lastFilter.current=next;setScroll(0);onScrollPosition?.(0);stage.current?.querySelector('.neural-leaves')?.scrollTo(0,0)},[source,scope,query,filter]);
  return <section className={'neural-stage '+(opened?'expanded':'overview')+(hasExpanded.current?'':' scene-enter')+(size?.height<650?' short':'')} ref={stage} aria-label="创作工作台">
    {size&&<NeuralArt layout={layout} leafLayout={leafLayout} returning={returning} size={size} source={module} opened={opened} items={library?items:[]} selected={selected?.id} scroll={scroll} paused={paused}/>}
    <nav className="scene-breadcrumb" aria-label="画布路径"><button onClick={onOverview} aria-label="返回创作空间"><ArrowLeft size={13}/><span>创作空间 <small>Core</small></span></button>{opened&&<><span>/</span><button onClick={()=>onExpand(module)}>{categories.find(x=>x.id===module)?.name}</button></>}{selected&&<><span>/</span><span className="crumb-title">{selected.title}</span></>}</nav>
    {size&&<button className="core-hit" style={{transform:`translate3d(${layout.core.x}px,${layout.core.y}px,0)`,width:layout.core.r*1.6,height:layout.core.r*1.6}} aria-label="返回空间总览" onClick={onOverview}><span className="core-caption">创作空间 <ArrowRight size={12}/></span></button>}
    {size&&<div className="scene-categories" aria-label="工作台模块">
      {layout.nodes.map(node=>{const Icon=icons[node.id],active=module===node.id;return <button key={node.id} className={'scene-category '+(active?'active':'')} style={{transform:`translate3d(${node.x}px,${node.y}px,0)`,'--node-color':node.color}} aria-label={`展开${node.name}`} aria-pressed={active} onClick={()=>onExpand(node.id)}><span className="category-number"><Icon size={15}/>{node.planned?<small className="category-planned">规划中</small>:totalCount??moduleCount(allItems,node.id)}</span><span className="category-name">{node.name} <small>{node.english}</small></span><span className="category-meta">{node.planned?'模块定位预览':'已保存的外部内容'}</span></button>})}
    </div>}
    {library&&size&&<>
      <div className="scene-find" ref={controls} style={{translate:`${leafLayout.leaf.x}px 0`,top:leafLayout.controlsTop,width:leafLayout.leaf.width}}><div className="scene-library-tools"><button onClick={onLibrary}>打开收藏库 <ArrowRight size={12}/></button><button onClick={onSync}>同步</button></div><label><Search size={13}/><input aria-label="搜索收藏" placeholder="搜索收藏…" value={query} onChange={e=>onQuery(e.target.value)}/>{query&&<button aria-label="清除搜索" onClick={()=>onQuery('')}><X size={12}/></button>}</label><div className="scene-filter-row"><select aria-label="选择资料来源" value={source} onChange={e=>onSource(e.target.value)}>{Object.entries(names).map(([id,name])=><option key={id} value={id}>{id==='all'?'全部来源':name}</option>)}</select><select aria-label="内容状态筛选" value={filter} onChange={e=>onFilter(e.target.value)}><option value="all">全部状态</option><option value="readable">可阅读</option><option value="pending">待处理</option></select></div>{folderPicker}</div>
      <div className="neural-leaves" aria-label="收藏列表" style={{translate:`${leafLayout.leaf.x}px 0`,top:leafLayout.leaf.top-30,width:leafLayout.leaf.width,'--leaf-top':`${leafLayout.leaf.top-30}px`}} onScroll={e=>{setScroll(e.currentTarget.scrollTop);onScrollPosition?.(e.currentTarget.scrollTop)}}>
        {items.map(item=><button key={item.id} className={'neural-leaf '+(selected?.id===item.id?'selected':'')} aria-label={'阅读 '+item.title} aria-current={selected?.id===item.id?'true':undefined} onClick={()=>onOpen(item.id)}><span className="leaf-content"><strong>{item.title}</strong><span><span>{item.author}</span><span className={'status '+item.state}>{labels[item.state]}</span></span></span></button>)}
        {hasMore&&<button className="secondary" disabled={busy} onClick={onLoadMore}>加载更多收藏</button>}
        {busy&&!items.length&&<p className="loading-copy" role="status">正在读取收藏…</p>}
        {!busy&&!items.length&&<div className="scene-empty"><Folder size={22}/><h2>{query?'没有匹配的收藏':'这里还没有资料'}</h2><p>{query?'试试作者、工具或笔记里的关键词。':'打开收藏库添加链接，或连接抖音同步。'}</p><button className="secondary" onClick={query?()=>onQuery(''):onSync}>{query?'清除搜索':'去同步资料'}<ArrowRight size={13}/></button></div>}
      </div>
    </>}
    <div className="scene-signature">CREATIVE<span>WORKSPACE</span></div>
    <div className="scene-bottom"><span className="scene-hint">{!opened?'选择一个模块，进入你的创作空间':library?'选择资料，查看总结与原始内容':'规划预览 · 尚未接入真实数据'} <ChevronRight size={12}/></span></div>
    <button className="scene-motion" aria-label={paused?'播放画布动效':'暂停画布动效'} onClick={()=>onPauseChange?.(!paused)}>{paused?<Play size={12}/>:<Pause size={12}/>}<span>{paused?'播放':'暂停'}</span></button>
  </section>;
}

const tau=Math.PI*2;
function noise(seed){const x=Math.sin(seed*127.1+311.7)*43758.5453123;return x-Math.floor(x)}
function project(point,cos,sin){let [x,y,z]=point;const rx=x*cos+z*sin,rz=z*cos-x*sin;return [rx,y*.88-rz*.47,y*.47+rz*.88]}
function globe(){const points=[],edges=[],n=220;for(let i=0;i<n;i++){const y=1-2*i/(n-1),r=Math.sqrt(1-y*y),a=i*2.399963,ripple=.87+.16*Math.sin(a*3+y*5)+.09*noise(i);points.push([Math.cos(a)*r*ripple,y*(.8+.13*Math.cos(a*2)),Math.sin(a)*r*ripple])}for(let i=0;i<n;i++)for(let j=i+1;j<n;j++){const d=points[i].reduce((s,p,k)=>s+(p-points[j][k])**2,0);if(d<.14)edges.push([i,j])}return {points,edges}}
const satellite=globe();
function drawMesh(ctx,mesh,x,y,r,angle,accent){
  const cos=Math.cos(angle),sin=Math.sin(angle),points=mesh.points.map(point=>project(point,cos,sin));
  const glow=ctx.createRadialGradient(x,y,0,x,y,r*1.7);glow.addColorStop(0,'#b9cbea13');glow.addColorStop(.45,'#a6c0e508');glow.addColorStop(1,'#a6c0e500');ctx.fillStyle=glow;ctx.fillRect(x-r*1.7,y-r*1.7,r*3.4,r*3.4);
  ctx.lineWidth=.55;
  // Batch by depth: a handful of strokes instead of thousands of paint calls per frame.
  const lines=Array.from({length:10},()=>[]),dots=Array.from({length:9},()=>[]);
  for(const edge of mesh.edges){const alpha=Math.max(.05,.15+(points[edge[0]][2]+points[edge[1]][2])*.11);lines[Math.min(9,Math.floor(alpha/.04))].push(edge);}
  lines.forEach((edges,depth)=>{if(!edges.length)return;ctx.strokeStyle=`rgba(190,207,232,${depth*.04+.02})`;ctx.beginPath();for(const [a,b] of edges){const p=points[a],q=points[b];ctx.moveTo(x+p[0]*r,y+p[1]*r);ctx.lineTo(x+q[0]*r,y+q[1]*r);}ctx.stroke();});
  for(let i=0;i<points.length;i+=2)dots[i%7===0?8:Math.min(7,Math.floor((.28+(points[i][2]+1)*.2)*10))].push(points[i]);
  dots.forEach((group,depth)=>{if(!group.length)return;ctx.fillStyle=depth===8?accent:`rgba(221,232,250,${depth/10+.05})`;ctx.beginPath();const radius=depth===8?1.1:.65;for(const p of group){ctx.moveTo(x+p[0]*r+radius,y+p[1]*r);ctx.arc(x+p[0]*r,y+p[1]*r,radius,0,tau);}ctx.fill();});
  for(let i=0;i<7;i++){const p=points[17+i*9],bend=Math.sin(angle+i)*r*.2;ctx.beginPath();ctx.moveTo(x+p[0]*r,y+p[1]*r);ctx.bezierCurveTo(x-r*.1,y+r*.7,x+r*.35+bend,y+r*1.1,x-r*.6+bend,y+r*1.25+i*2);ctx.strokeStyle=accent+'30';ctx.lineWidth=.45;ctx.stroke()}
}
function NeuralArt(props){
  const canvas=useRef(null),live=useRef(props);live.current=props;
  useEffect(()=>{
    const ctx=canvas.current.getContext('2d');let frame,previous=0,time=0,current=null,lastDraw=null,target=null,from=null,started=0,geometryKey='',reverse=false;
    let reportStart=0,paintTotal=0,paintFrames=0;
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
    const render=stamp=>{
      frame=requestAnimationFrame(render);const delta=previous?Math.min(64,stamp-previous):0;previous=stamp;
      const {size,layout,leafLayout,returning,source,opened,items,selected,scroll,paused}=live.current;
      if(document.hidden)return;
      if((paused||reduced.matches)&&lastDraw===live.current&&current===target)return;
      const paintStart=performance.now();
      lastDraw=live.current;
      if(!paused&&!reduced.matches)time+=delta/1000;
      const dpr=Math.min(window.devicePixelRatio||1,2),w=Math.round(size.width*dpr),h=Math.round(size.height*dpr);
      if(canvas.current.width!==w||canvas.current.height!==h){canvas.current.width=w;canvas.current.height=h}
      ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,size.width,size.height);
      const nextKey=`${size.width}:${size.height}:${opened}:${layout.nodes[0].x}:${layout.leaf.x}:${layout.leaf.top}:${layout.leaf.width}`;
      if(nextKey!==geometryKey){geometryKey=nextKey;target=sceneTarget(layout,opened);from=current||target;started=stamp;reverse=!opened;}
      // Time-based interpolation follows the same 420ms easing as DOM cards at any refresh rate.
      const progress=reduced.matches?1:sceneTravelProgress(stamp-started,reverse);
      current=progress===1?target:interpolateScene(from,target,progress);
      const core=current.core;
      for(let i=0;i<28;i++){const a=noise(i+8)*tau+time*.004,r=(1.05+noise(i+19)*.65)*core.r;const x=core.x+Math.cos(a)*r*1.2,y=core.y+Math.sin(a)*r;ctx.fillStyle=`rgba(210,227,251,${.035+noise(i+39)*.1})`;ctx.beginPath();ctx.arc(x,y,noise(i+7)*.55+.25,0,tau);ctx.fill()}
      current.nodes.forEach((node,i)=>{const active=opened&&(source===node.id),nx=node.meshX,ny=node.meshY;const link=ctx.createLinearGradient(core.x,core.y,nx,ny);link.addColorStop(0,node.color+'00');link.addColorStop(.28,node.color+'08');link.addColorStop(.7,node.color+(active?'a0':'40'));link.addColorStop(1,node.color+(active?'d0':'60'));ctx.beginPath();ctx.moveTo(core.x,core.y);ctx.bezierCurveTo(core.x+(nx-core.x)*.55,core.y,nx-50,ny,nx,ny);ctx.strokeStyle=link;ctx.lineWidth=active?1:.65;ctx.stroke();drawMesh(ctx,satellite,nx,ny,node.meshR,time*.11+i,node.color)});
      drawSilkCore(ctx,core.x,core.y,core.r,time);
      if(opened||returning){const active=current.nodes.find(x=>x.id===source)||current.nodes.at(-1);ctx.save();ctx.globalAlpha=current.leafOpacity;ctx.beginPath();ctx.rect(0,122,size.width,Math.max(0,size.height-185));ctx.clip();items.forEach((item,i)=>{const y=current.leaf.top+i*current.leaf.row-scroll,x=current.leaf.x+5,isSelected=item.id===selected;if(y<100||y>size.height-45)return;const start=active.x+(leafLayout.compact?51:67);ctx.beginPath();ctx.moveTo(start,active.y);ctx.bezierCurveTo(start+(x-start)*.38,active.y,x-(x-start)*.3,y,x,y);ctx.strokeStyle=isSelected?active.color:'#889ab458';ctx.lineWidth=isSelected?1.4:.8;ctx.shadowColor=isSelected?active.color:'transparent';ctx.shadowBlur=isSelected?3:0;ctx.stroke();ctx.shadowBlur=0;ctx.fillStyle=isSelected?active.color:'#b2c8e8';ctx.beginPath();ctx.arc(x,y,3,0,tau);ctx.fill()});ctx.restore()}
      paintTotal+=performance.now()-paintStart;paintFrames++;
      if(!reportStart)reportStart=stamp;
      if(stamp-reportStart>=1000){canvas.current.dataset.frameRate=(paintFrames*1000/(stamp-reportStart)).toFixed(1);canvas.current.dataset.paintMs=(paintTotal/paintFrames).toFixed(2);reportStart=stamp;paintTotal=0;paintFrames=0;}
    };
    frame=requestAnimationFrame(render);return()=>cancelAnimationFrame(frame);
  },[]);
  return <canvas ref={canvas} className="neural-art" aria-hidden="true"/>;
}
