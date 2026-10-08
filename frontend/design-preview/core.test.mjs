import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {filamentPoint,coreGeometry} from '../src/workspace/core-art.mjs';
import {sceneLayout} from '../src/workspace/scene.mjs';
import {sceneTarget,interpolateScene,sceneProgress,sceneTravelProgress} from '../src/workspace/motion.mjs';
test('preview imports the original scene instead of a second renderer',async()=>{
 const src=await readFile(new URL('./main.jsx',import.meta.url),'utf8');
 assert.match(src,/import \{ NeuralWorkspace \} from "\.\.\/src\/workspace\/NeuralWorkspace.jsx"/);
 assert.doesNotMatch(src,/paintCore|sphereUV|2k_moon|<canvas/);
});
test('original core remains a finite closed filament surface',()=>{
 const paths=coreGeometry(3);assert.equal(paths.length,40);
 for(const p of paths){assert.equal(p.length,145);assert.ok(p.every(v=>Object.values(v).every(Number.isFinite)));assert.ok(Math.hypot(p[0].x-p.at(-1).x,p[0].y-p.at(-1).y)<1e-9);}
 const a=filamentPoint(1,2,3),b=filamentPoint(1,2,3.016);
 assert.ok(Math.hypot(a.x-b.x,a.y-b.y)<.01);
});
test('all preview layouts retain six modules and share reversible coordinates',()=>{
 for(const w of [390,820,1280,1440,1920]){
  const a=sceneTarget(sceneLayout(w,760,false),false),b=sceneTarget(sceneLayout(w,760,true,true),true);
  assert.equal(a.nodes.length,6);assert.deepEqual(a.nodes.map(x=>x.id),b.nodes.map(x=>x.id));
  assert.deepEqual(interpolateScene(a,b,0).core,a.core);
  for(const k of ['x','y','r'])assert.ok(Math.abs(interpolateScene(a,b,1).core[k]-b.core[k])<1e-9);
 }
 for(const t of [0,80,210,350,420])assert.ok(Math.abs(sceneProgress(t)-sceneTravelProgress(t,true))<.0001);
});
test('preview has no production API or credential integration',async()=>{
 const src=await readFile(new URL('./main.jsx',import.meta.url),'utf8');
 assert.doesNotMatch(src,/\bfetch\s*\(|createApi|localStorage|sessionStorage|api_key|\/v1\//);assert.match(src,/视觉预览/);
});
test('preview server binds loopback and serves only named assets',async()=>{
 const src=await readFile(new URL('./build.mjs',import.meta.url),'utf8');
 assert.match(src,/listen\(8818,'127\.0\.0\.1'/);assert.match(src,/if\(!types\[p\]\)/);
});
