import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const css=await readFile(new URL('../src/workspace/cosmos.css',import.meta.url),'utf8');
const color=name=>{const match=css.match(new RegExp(`--${name}: *(#[0-9a-f]{6})`));assert.ok(match,`missing ${name}`);return match[1]};
function luminance(hex){const rgb=hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722}
function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}

test('space palette stays black-led while body, muted and accent text remain readable',()=>{
  assert.ok(luminance(color('bg'))<.002);
  for(const background of ['bg','surface','paper','space-panel','space-raised','space-hover']){
    for(const foreground of ['text','muted','accent'])assert.ok(contrast(color(foreground),color(background))>=4.5,`${foreground} on ${background}`);
  }
  for(const background of ['space-primary','space-primary-hover'])assert.ok(contrast(color('ink'),color(background))>=7);
  for(const [fg,bg] of [['space-success','space-success-bg'],['amber','space-warning-bg'],['red','space-error-bg']])assert.ok(contrast(color(fg),color(bg))>=4.5);
});

test('theme loads last and preserves approved geometry and motion',async()=>{
  const source=await readFile(new URL('../src/workspace/Workbench.jsx',import.meta.url),'utf8');
  assert.ok(source.indexOf("import './cosmos.css'")>source.indexOf("import './live.css'"));
  assert.doesNotMatch(css,/(?:^|[;{])\s*(?:width|height|margin|padding|transform|transition|animation|display|position)\s*:/m);
});
