import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {nebulaPixels,skyStars,starPosition,drawSky} from './space-art.mjs';

test('星云纹理确定且不规则，黑色占主体而非整片高亮渐变',()=>{
  const pixels=nebulaPixels(180,120);
  assert.deepEqual(pixels,nebulaPixels(180,120));
  let dim=0,lit=0;for(let i=0;i<pixels.length;i+=4){assert.equal(pixels[i+3],255);if(pixels[i+2]<15)dim++;if(pixels[i+2]>20)lit++;}
  assert.ok(dim>180*120*.55);assert.ok(lit>100);
  assert.notEqual(pixels[(60*180+70)*4+2],pixels[(60*180+80)*4+2]);
});
test('星点分三个深度，慢速连续漂移，数量有上限',()=>{
  const stars=skyStars(1280,900);assert.deepEqual(stars,skyStars(1280,900));assert.deepEqual([...new Set(stars.map(s=>s.depth))].sort(),[0,1,2]);
  assert.ok(skyStars(10000,10000).length<=1800);
  for(const star of stars){const a=starPosition(star,0,{x:0,y:0}),b=starPosition(star,1/30,{x:0,y:0});assert.ok(Math.hypot(a.x-b.x,a.y-b.y)<.003);assert.ok(Math.abs(a.alpha-b.alpha)<.002)}
  const far=stars.find(s=>s.depth===0),near=stars.find(s=>s.depth===2);
  assert.ok(starPosition(near,0,{x:1,y:0}).x-starPosition(near,0,{x:0,y:0}).x>starPosition(far,0,{x:1,y:0}).x-starPosition(far,0,{x:0,y:0}).x);
});
test('星场绘图调用有界，无网络素材和每帧随机闪烁',async()=>{
  let fills=0;const numeric=(...n)=>n.forEach(x=>assert.ok(Number.isFinite(x)));
  const ctx={clearRect:numeric,drawImage(){},beginPath(){},moveTo:numeric,arc:numeric,fill(){fills++},fillRect:numeric,createRadialGradient(...n){numeric(...n);return {addColorStop(){}}}};
  drawSky(ctx,{},skyStars(1280,900),1280,900,12,{x:.5,y:-.3});assert.equal(fills,15);assert.equal(ctx.globalAlpha,1);
  const source=await readFile(new URL('./space-art.mjs',import.meta.url),'utf8');assert.doesNotMatch(source,/Math\.random|fetch\(|https?:\/\//);
});
test('星空暂停、隐藏窗口、减少动态效果都受控，事件和观察器清理',async()=>{
  const source=await readFile(new URL('./CosmicBackdrop.jsx',import.meta.url),'utf8');
  assert.match(source,/!live.current.paused&&!reduced.matches&&live.current.tone==='workspace'/);
  assert.match(source,/if\(document.hidden\).*cancelAnimationFrame/);
  assert.match(source,/observer.disconnect\(\)/);assert.match(source,/removeEventListener\('pointermove'/);
  const app=await readFile(new URL('./Workbench.jsx',import.meta.url),'utf8');assert.match(app,/<CosmicBackdrop/);assert.match(app,/paused=\{cosmosPaused\} onPauseChange=\{setCosmosPaused\}/);
});
