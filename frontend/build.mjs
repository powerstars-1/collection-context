// Deterministic static build. Avoid a second runtime and native Vite CSS plugins.
import { build } from 'esbuild';
import { compile } from 'tailwindcss';
import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require=createRequire(import.meta.url);
const root=path.dirname(fileURLToPath(import.meta.url));
const out=path.resolve(root,'../backend/src/collection_context/interfaces/assets');
await mkdir(out,{recursive:true});
await build({entryPoints:[path.join(root,'src/main.jsx')],outfile:path.join(out,'app.js'),bundle:true,minify:true,format:'esm',target:['es2020'],jsx:'automatic',legalComments:'eof',define:{'process.env.NODE_ENV':'"production"'}});
async function sourceFiles(dir){const entries=await readdir(dir,{withFileTypes:true});const nested=await Promise.all(entries.map(entry=>entry.isDirectory()?sourceFiles(path.join(dir,entry.name)):/\.(jsx|tsx|js)$/.test(entry.name)?[path.join(dir,entry.name)]:[]));return nested.flat();}
const sources=await Promise.all((await sourceFiles(path.join(root,'src'))).map(file=>readFile(file,'utf8')));
const candidates=[...new Set(sources.flatMap(source=>source.match(/[^\s<>"'`{}]+/g)||[]))];
const tailwind=path.dirname(require.resolve('tailwindcss/package.json'));
let css=await readFile(path.join(root,'src/upstream.css'),'utf8');
// No animation extension is used by the retained components; retain upstream file verbatim.
css=css.replace('@import "tw-animate-css";','');
css+='\n'+await readFile(path.join(root,'src/adapters.css'),'utf8');
const compiler=await compile(css,{loadStylesheet:async (id)=>({content:await readFile(path.join(tailwind,id==='tailwindcss'?'index.css':id),'utf8'),base:tailwind})});
await writeFile(path.join(out,'app.css'),compiler.build(candidates));
await writeFile(path.join(out,'index.html'),await readFile(path.join(root,'index.html')));
await writeFile(path.join(out,'THIRD_PARTY_NOTICES.txt'),await readFile(path.join(root,'THIRD_PARTY_NOTICES.md')));
console.log('Static React frontend built into backend assets. Node is not needed at runtime.');
