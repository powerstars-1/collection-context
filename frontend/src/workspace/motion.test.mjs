import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {SCENE_MS,EXIT_MS,sceneProgress,sceneTravelProgress,sceneTarget,interpolateScene,workspaceExitKind,routeTransition} from './motion.mjs';
import {sceneLayout} from './scene.mjs';
const home={page:'workspace',module:'',id:''},library={...home,module:'library'},reader={...library,id:'depth'};
test('420ms 固定时长，60/120Hz 采样均单调收敛且不会无限追赶',()=>{
  for(const fps of [60,120]){let prior=0;for(let ms=0;ms<=SCENE_MS+100;ms+=1000/fps){const p=sceneProgress(ms);assert.ok(p>=prior&&p<=1);prior=p;}assert.equal(prior,1);}
  assert.equal(sceneProgress(-10),0);assert.equal(sceneProgress(SCENE_MS),1);
});
test('卡片、图形中心及半径共用时间进度，输入几何不被修改',()=>{
  const a=sceneTarget(sceneLayout(1200,800,false),false),b=sceneTarget(sceneLayout(1200,800,true),true),before=structuredClone(a);
  assert.deepEqual(interpolateScene(a,b,1),b);assert.deepEqual(interpolateScene(a,b,0).core,a.core);
  const mid=interpolateScene(a,b,.5);assert.equal(mid.nodes[0].meshX,(a.nodes[0].meshX+b.nodes[0].meshX)/2);assert.deepEqual(a,before);
});
test('关闭列表、关闭阅读面板和切换模块都有对应退出，普通页面不延迟',()=>{
  assert.equal(workspaceExitKind(library,home),'overview');assert.equal(workspaceExitKind(reader,library),'reader');
  assert.equal(workspaceExitKind(reader,{...home,module:'ideas'}),'scene');assert.equal(workspaceExitKind(home,library),'');
  assert.equal(workspaceExitKind({page:'library',id:'depth'},home),'');assert.equal(workspaceExitKind(reader,reader),'');
});
function harness(initial=reader){let target=initial,id=0;const jobs=new Map(),commits=[],phases=[],delays=[];const transition=routeTransition({read:()=>target,commit:v=>commits.push(v),phase:v=>phases.push(v),reduced:()=>false,schedule:(fn,ms)=>{delays.push(ms);jobs.set(++id,fn);return id},cancel:id=>jobs.delete(id)});return {jobs,commits,phases,delays,transition,go(next){target=next;transition.change()},flush(){for(const [id,fn] of [...jobs]){jobs.delete(id);fn()}}};}
test('返回时保留旧内容 420ms，滑出后才卸载',()=>{const h=harness();h.go(library);assert.deepEqual(h.phases,['reader']);assert.equal(h.commits.length,0);h.flush();assert.deepEqual(h.commits,[library]);assert.equal(h.phases.at(-1),'');});
test('连续切换或浏览器回退取消旧目标，不出现过期页面',()=>{const h=harness();h.go(home);h.go({page:'ideas',module:'',id:''});assert.equal(h.jobs.size,1);h.flush();assert.equal(h.commits.length,1);assert.equal(h.commits[0].page,'ideas');const r=harness();r.go(home);r.go(reader);r.flush();assert.deepEqual(r.commits,[reader]);});
test('卸载清理定时器；减弱动态效果立即返回',()=>{const h=harness();h.go(home);h.transition.dispose();assert.equal(h.jobs.size,0);let next=reader;const commits=[];const t=routeTransition({read:()=>next,commit:v=>commits.push(v),phase:()=>{},reduced:()=>true,schedule:()=>assert.fail('must not wait')});next=home;t.change();assert.deepEqual(commits,[home]);t.dispose();});
test('展示层实际接入双向动画，退出期间禁用旧内容点击',async()=>{const app=(await readFile(new URL('./Workbench.jsx',import.meta.url),'utf8'))+(await readFile(new URL('./WorkspaceSurface.jsx',import.meta.url),'utf8')),css=await readFile(new URL('./workspace-library.css',import.meta.url),'utf8');assert.match(app,/useWorkspaceRoute\(route\)/);assert.match(app,/inert=\{exit\?true:undefined\}/);assert.match(css,/workspace-panel-out \.22s/);assert.match(css,/workspace-panel-in \.42s/);assert.match(css,/prefers-reduced-motion:reduce/);});
test('返回总览沿展开的原路径和时间曲线逆放，位置和半径逐点对应',()=>{
  const home=sceneTarget(sceneLayout(1300,720,false),false),expanded=sceneTarget(sceneLayout(760,720,true),true);
  for(const elapsed of [0,70,140,210,280,350,SCENE_MS]){
    const forward=interpolateScene(home,expanded,sceneProgress(SCENE_MS-elapsed));
    const backward=interpolateScene(expanded,home,sceneTravelProgress(elapsed,true));
    for(const key of ['x','y','r'])assert.ok(Math.abs(forward.core[key]-backward.core[key])<1e-8);
    forward.nodes.forEach((node,i)=>{for(const key of ['x','y','meshX','meshY','meshR'])assert.ok(Math.abs(node[key]-backward.nodes[i][key])<1e-8)});
  }
});
test('选题池返回时保持挂载直到 420ms 位移动画结束，而不是先淡出再跳回',()=>{
  const h=harness({...home,module:'ideas'});h.go(home);assert.deepEqual(h.phases,['overview']);assert.deepEqual(h.delays,[SCENE_MS]);assert.equal(h.commits.length,0);h.flush();assert.deepEqual(h.commits,[home]);
  const r=harness();r.go(library);assert.deepEqual(r.delays,[SCENE_MS]);
});
test('返回阶段画布复用原组件，右侧面板固定原位退出，不参与画布宽度',async()=>{
  const app=await readFile(new URL('./Workbench.jsx',import.meta.url),'utf8'),css=await readFile(new URL('./workspace-library.css',import.meta.url),'utf8');
  assert.match(app,/returning=\{workspaceExit==='overview'\}/);
  assert.match(css,/\.workspace-panel\{position:absolute/);
  assert.match(css,/workspace-reader-out \.42s cubic-bezier\(\.35,0,\.65,1\) both/);
});
test('双向起步和收尾温和，半程约半距离，不再 93% 对 7%',()=>{
  for(const reverse of [false,true]){
    assert.ok(Math.abs(sceneTravelProgress(SCENE_MS*.5,reverse)-.5)<.001);
    const quarter=sceneTravelProgress(SCENE_MS*.25,reverse);assert.ok(quarter>.1&&quarter<.25);
    assert.ok(sceneTravelProgress(8,reverse)<.004);
    assert.ok(1-sceneTravelProgress(SCENE_MS-8,reverse)<.004);
  }
});
test('卡片按压不缩放，点击反馈只改材质，入场不缩放定位层',async()=>{
  const css=await readFile(new URL('./neural.css',import.meta.url),'utf8');
  assert.match(css,/\.scene-category:active\{scale:1;background-color:/);
  assert.doesNotMatch(css,/\.scene-category:active\{[^}]*scale:\.98/);
  assert.match(css,/@keyframes scene-card-reveal\{from\{opacity:0\}to\{opacity:1\}\}/);
});
test('两栏三栏的连线端点与模块共同移动，关闭逐点逆向',()=>{
  const a=sceneTarget(sceneLayout(1634,1065,true),true),b=sceneTarget(sceneLayout(1634,1065,true,true),true);
  for(const ms of [0,70,140,210,280,350,SCENE_MS]){
    const next=interpolateScene(a,b,sceneProgress(ms));
    const reverse=interpolateScene(b,a,sceneTravelProgress(SCENE_MS-ms,true));
    assert.ok(Math.abs(next.leaf.x-reverse.leaf.x)<1e-8);
    assert.equal(next.leafOpacity,1);
  }
  assert.equal(workspaceExitKind(reader,{...reader,id:'another'}),'');
});
