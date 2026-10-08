import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {categories,sceneLayout,moduleCount} from './scene.mjs';
import {parseRoute,expandedReadingHash,topNavigation,activeNavigation} from './structure.mjs';
const initialState=()=>({items:[{id:'test1'},{id:'test2'},{id:'test3'}]});
test('参考的五个创作模块加收藏库，不把来源升格成主模块',()=>{assert.deepEqual(categories.map(x=>x.name),['概念网络','方法库','作品库','选题池','创作台','收藏库']);const {items}=initialState();assert.equal(moduleCount(items,'library'),3);for(const id of ['works','concepts','methods','ideas','drafts'])assert.equal(moduleCount(items,id),null)});
test('展开后核心缩小并左移，分类和叶子分居不同位置',()=>{const before=sceneLayout(1300,720,false),after=sceneLayout(1300,720,true);assert.ok(after.core.r<before.core.r);assert.ok(after.core.x<before.core.x);assert.ok(after.nodes.every(x=>x.x<after.leaf.x));assert.ok(after.nodes.every(x=>x.y>120&&x.y<720-70))});
test('小屏坐标与桌面共用几何，叶子不超画布',()=>{for(const width of [350,570,820,1320]){const scene=sceneLayout(width,720,true);assert.ok(scene.leaf.x+scene.leaf.width<=width);assert.ok(scene.nodes.every(x=>x.x>50&&x.x+51<scene.leaf.x))}});
test('模块切换保持稳定次序与色彩',()=>assert.deepEqual(sceneLayout(1320,720,true).nodes.map(x=>x.id),categories.map(x=>x.id)));
test('列表紧接筛选区 18px，不随窗口高度增加空白',()=>{
  for(const height of [640,800,1100,1400])for(const controlsHeight of [92,116]){
    const s=sceneLayout(1600,height,true,false,controlsHeight);
    assert.equal(s.leaf.top-30-(s.controlsTop+controlsHeight),18);
  }
});
test('两栏共用模块位置和内容起点，三栏只增加阅读列且不重叠',()=>{
  for(const width of [920,1100,1300,1634,2200]){
    const two=sceneLayout(width,900,true),three=sceneLayout(width,900,true,true);
    assert.equal(two.panel.x,two.leaf.x);assert.equal(two.panel.width,two.leaf.width);
    assert.ok(three.core.x<two.core.x);assert.ok(three.nodes[0].x<two.nodes[0].x);
    assert.ok(three.nodes[0].x+73<three.leaf.x);
    assert.equal(three.leaf.x+three.leaf.width+24,three.panel.x);
    assert.equal(three.panel.x+three.panel.width,width);
    assert.equal(three.leaf.top,two.leaf.top);
  }
});
test('工作台是默认入口，五个顶级模块与同步设置的层级独立',()=>{assert.equal(parseRoute('').page,'workspace');assert.deepEqual(topNavigation.map(x=>x[0]),['workspace','works','drafts','ideas','library']);assert.equal(activeNavigation('sync'),'library');assert.equal(activeNavigation('ai'),'ai');assert.equal(activeNavigation('concepts'),'workspace')});
test('收藏可从画布和独立模块阅读，深链接保留当前工作上下文',()=>{assert.deepEqual(parseRoute('#workspace/library/depth'),{page:'workspace',module:'library',id:'depth'});assert.deepEqual(parseRoute('#library/depth'),{page:'library',module:'',id:'depth'});assert.equal(parseRoute('#workspace/works/fake').id,'');assert.equal(parseRoute('#workspace/liked').module,'');assert.equal(parseRoute('#bad').page,'workspace')});
test('展开阅读定位到同一资料，独立链接和后退均可还原模式',()=>{const hash=expandedReadingHash('depth');assert.equal(hash,'#library/depth/read');assert.deepEqual(parseRoute(hash),{page:'library',module:'',id:'depth',expanded:true});assert.equal(parseRoute('#library/depth').expanded,undefined);assert.equal(parseRoute('#workspace/library/depth').expanded,undefined)});
test('预览和完整阅读共用正文及材质，放大有明确文字且可返回原预览',async()=>{const app=(await readFile(new URL('./Workbench.jsx',import.meta.url),'utf8'))+(await readFile(new URL('./Reader.jsx',import.meta.url),'utf8'));assert.equal((app.match(/aria-label="资料阅读"/g)||[]).length,1);assert.match(app,/展开阅读/);assert.match(app,/返回阅读位置/);assert.match(app,/returns\.current\.reader=\{hash:location.hash\}/);assert.match(app,/location\.hash=expandedReadingHash\(selected\.id\)/);assert.doesNotMatch(app,/setFocus/);const css=await readFile(new URL('./workspace-library.css',import.meta.url),'utf8');assert.match(css,/\.reader-pane\.reading-surface\{/);assert.match(css,/background:var\(--reader-surface\)/);assert.match(css,/reader-preview/);assert.match(css,/reader-expanded/)});
test('返回首页恢复画布收藏滚动，不因组件重新挂载重置筛选',async()=>{const scene=await readFile(new URL('./NeuralWorkspace.jsx',import.meta.url),'utf8');assert.match(scene,/leaves\.scrollTop=initialScroll/);assert.match(scene,/if\(lastFilter\.current===next\)return/);assert.match(scene,/onScrollPosition\?\.\(e\.currentTarget\.scrollTop\)/)});
test('首次加载原位淡入；模块返回不重播入场，沿原路径反向移动',async()=>{const scene=await readFile(new URL('./NeuralWorkspace.jsx',import.meta.url),'utf8'),css=await readFile(new URL('./neural.css',import.meta.url),'utf8');assert.match(scene,/\[size,setSize\]=useState\(null\)/);assert.match(scene,/size&&<div className="scene-categories"/);assert.doesNotMatch(scene,/paused\|\|reduced\.matches\|\|!opened\?1/);assert.match(scene,/sceneTravelProgress\(stamp-started,reverse\)/);assert.match(css,/\.scene-enter\.overview \.scene-category\{transition:background-color \.16s,border-color \.16s,box-shadow \.16s,color \.16s!important;animation:scene-card-reveal \.42s/);assert.match(css,/\.scene-enter\.overview \.neural-art\{animation:scene-art-reveal \.42s/);assert.match(css,/\.overview:not\(\.scene-enter\) \.scene-category\{transition:transform \.42s cubic-bezier\(\.35,0,\.65,1\)/);assert.match(css,/@media\(prefers-reduced-motion:reduce\)[^\n]*\.overview \.neural-art\{animation:none!important\}/);assert.doesNotMatch(scene,/stamp-previous<40/)});
test('未实现模块只有规划预览，不创建假内容或触发请求',async()=>{const text=await readFile(new URL('./ModulePreview.jsx',import.meta.url),'utf8');assert.match(text,/尚未接入真实数据和编辑能力/);assert.doesNotMatch(text,/fetch\(|dispatch\(|新增作品|保存草稿/)});
test('绘图不引入请求或外部代码，不替代正文组件',async()=>{const scene=await readFile(new URL('./NeuralWorkspace.jsx',import.meta.url),'utf8');assert.doesNotMatch(scene,/fetch\(|XMLHttpRequest|https:\/\//);assert.match(scene,/cancelAnimationFrame/);assert.match(scene,/prefers-reduced-motion/);const app=await readFile(new URL('./Workbench.jsx',import.meta.url),'utf8');assert.match(await readFile(new URL('./Reader.jsx',import.meta.url),'utf8'),/import \{ReadingContent\}/);assert.match(app,/view==='list'/)});
