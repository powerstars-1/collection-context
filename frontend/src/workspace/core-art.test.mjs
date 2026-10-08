import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {coreGeometry,drawSilkCore} from './core-art.mjs';

test('核心细曲线可重复、闭合且坐标有界，不在每帧随机跳动',()=>{
  const geometry=coreGeometry(24);
  assert.deepEqual(geometry,coreGeometry(24));
  assert.equal(geometry.length,40);
  for(const strand of geometry){
    assert.equal(strand.length,145);
    for(const axis of ['x','y','z'])assert.ok(Math.abs(strand[0][axis]-strand.at(-1)[axis])<1e-10);
    for(const point of strand)for(const value of Object.values(point))assert.ok(Number.isFinite(value)&&Math.abs(value)<1.1);
  }
});

test('相邻帧变化小，持续观察时几何确实缓慢流动',()=>{
  const start=coreGeometry(0).flat(),next=coreGeometry(1/60).flat(),later=coreGeometry(5).flat();
  const distance=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
  assert.ok(Math.max(...start.map((p,i)=>distance(p,next[i])))<.002);
  assert.ok(Math.max(...start.map((p,i)=>distance(p,later[i])))>.01);
});

test('渲染限定画笔调用、坐标有效，并恢复 Canvas 状态',()=>{
  let balance=0,strokes=0,segments=0;
  const numbers=(...args)=>args.forEach(n=>assert.ok(Number.isFinite(n)));
  const ctx={save(){balance++},restore(){balance--},createRadialGradient(...args){numbers(...args);return {addColorStop(){}}},fillRect:numbers,beginPath(){},moveTo:numbers,lineTo(...args){numbers(...args);segments++},arc:numbers,stroke(){strokes++},fill(){}};
  for(const radius of [45,280])drawSilkCore(ctx,360,360,radius,15);
  assert.equal(balance,0);
  assert.equal(strokes,2*(12+3*18));
  assert.ok(segments>11000);
});

test('核心无需外部资源；暂停环境动效仍保留导航过渡',async()=>{
  const core=await readFile(new URL('./core-art.mjs',import.meta.url),'utf8');
  assert.doesNotMatch(core,/fetch\(|XMLHttpRequest|Math\.random|https:\/\//);
  const scene=await readFile(new URL('./NeuralWorkspace.jsx',import.meta.url),'utf8');
  assert.match(scene,/\(paused\|\|reduced\.matches\)&&lastDraw===live\.current&&current===target/);
  assert.match(scene,/const progress=reduced\.matches\?1:sceneTravelProgress/);
});
