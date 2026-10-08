import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {createRequire} from 'node:module';

const compiled=await build({stdin:{contents:"export {button,primary,input,Card,Page,Dialog} from './src/ui.jsx';export {renderToStaticMarkup} from 'react-dom/server';export {createElement} from 'react';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
const module={exports:{}};new Function('require','module','exports',compiled.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
const ui=module.exports;
test('core UI exposes semantic variants instead of relying on vendor colour utility names',()=>{
  assert.ok(ui.primary.includes('cc-button--primary'));assert.ok(ui.button.includes('cc-button--secondary'));assert.ok(ui.input.includes('cc-input'));
  for(const [component,props,semantic] of [[ui.Card,{title:'测试'},'cc-ui-card'],[ui.Page,{title:'测试'},'cc-page-heading'],[ui.Dialog,{title:'测试',onClose(){}},'cc-ui-dialog']]){
    assert.ok(ui.renderToStaticMarkup(ui.createElement(component,props)).includes(semantic));
  }
});
test('component skin loads after the environment and does not move approved scene geometry',async()=>{
  const app=await readFile(new URL('../src/workspace/Workbench.jsx',import.meta.url),'utf8'),css=await readFile(new URL('../src/workspace/space-ui.css',import.meta.url),'utf8');
  assert.ok(app.indexOf("import './space-ui.css'")>app.indexOf("import './atmosphere.css'"));
  const rule=css.match(/\.scene-category\{([^}]+)\}/)[1];assert.doesNotMatch(rule,/\b(?:width|height|transform|translate|left|top|transition)\s*:/);
  assert.match(css,/\.live-controls \.cc-sources-page button\.cc-button--primary\{/);
  assert.match(css,/\.live-controls \.cc-sources-page button\.cc-button--primary:disabled\{/);
});
test('silver primary and glass secondary text and control edges retain contrast',()=>{
  const luminance=hex=>{const c=hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return c[0]*.2126+c[1]*.7152+c[2]*.0722};
  const contrast=(a,b)=>(Math.max(luminance(a),luminance(b))+.05)/(Math.min(luminance(a),luminance(b))+.05);
  for(const [fg,bg] of [['#101d30','#e4edf9'],['#101d30','#b5c7e0'],['#e3ecfa','#253246'],['#a8b6c9','#192230']])assert.ok(contrast(fg,bg)>=4.5);
  assert.ok(contrast('#70829c','#192230')>=3);
});
