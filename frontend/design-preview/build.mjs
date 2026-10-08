import {build} from 'esbuild';
import {mkdir,copyFile,readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import http from 'node:http';
const here=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(here,'../..');
const out=path.join(root,'data/design-previews/2026-10-07-quiet-observatory');
await mkdir(out,{recursive:true});
await build({entryPoints:[path.join(here,'main.jsx')],outfile:path.join(out,'app.js'),bundle:true,format:'iife',jsx:'automatic',minify:true,external:['*.jpg'],define:{'process.env.NODE_ENV':'"production"'},legalComments:'eof',
  // Reuse the real scene and motion code, but keep preview tokens isolated from production CSS.
  plugins:[{name:'isolated-scene-style',setup(b){b.onLoad({filter:/workspace\/neural\.css$/},()=>({contents:'',loader:'css'}))}}]});
await copyFile(path.join(here,'index.html'),path.join(out,'index.html'));
for(const f of ['2k_stars.jpg','2k_stars_milky_way.jpg','ATTRIBUTION.md'])await copyFile(path.join(root,'data/design-research/2026-10-07-space-quality',f),path.join(out,f));
console.log(`Preview built: ${out}`);
if(process.argv.includes('--serve')){
 const types={'/':'text/html','/index.html':'text/html','/app.js':'text/javascript','/app.css':'text/css','/2k_stars.jpg':'image/jpeg','/2k_stars_milky_way.jpg':'image/jpeg','/2k_moon.jpg':'image/jpeg','/ATTRIBUTION.md':'text/plain'};
 http.createServer(async(req,res)=>{const p=new URL(req.url,'http://localhost').pathname;if(!types[p]){res.writeHead(404);res.end('Not found');return}try{const b=await readFile(path.join(out,p==='/'?'index.html':p.slice(1)));res.writeHead(200,{'Content-Type':types[p]+'; charset=utf-8','Cache-Control':'no-store'});res.end(b)}catch{res.writeHead(500);res.end('Preview resource unavailable')}}).listen(8818,'127.0.0.1',()=>console.log('Read-only style preview: http://127.0.0.1:8818'));
}
