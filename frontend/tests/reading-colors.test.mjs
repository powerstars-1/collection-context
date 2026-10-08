import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const css=await readFile(new URL('../src/workspace/workspace-library.css',import.meta.url),'utf8')+await readFile(new URL('../src/workspace/cosmos.css',import.meta.url),'utf8');
const color=name=>{const match=[...css.matchAll(new RegExp(`--reader-${name}: *(#[0-9a-f]{6})`,'g'))].at(-1);assert.ok(match,`missing ${name}`);return match[1]};
function luminance(hex){const c=hex.slice(1).match(/../g).map(s=>parseInt(s,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return c[0]*.2126+c[1]*.7152+c[2]*.0722}
function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}

test('reading block text stays readable on every opaque dark surface',()=>{
  for(const fg of ['block-ink','block-muted','link'])for(const bg of ['block-bg','block-header','block-alt']){
    assert.ok(contrast(color(fg),color(bg))>=7,`${fg} on ${bg} must exceed 7:1`);
  }
});
test('legacy white tables, prompts, code and outline highlights have scoped dark overrides',()=>{
  for(const selector of ['.cc-reading-table th','.cc-reading-table td','.cc-prompt-text','.cc-reading-body pre','.cc-reading-body code','.cc-reading-body blockquote']){
    const rules=[...css.matchAll(/([^{}]+)\{([^{}]+)\}/g)].filter(m=>m[1].split(',').some(s=>s.trim()===`.reading-surface ${selector}`));
    assert.ok(rules.some(m=>/background:var\(--reader-block-/.test(m[2])&&/color:var\(--reader-block-/.test(m[2])),`${selector} needs a complete foreground/background pair`);
  }
  assert.match(css,/\.reading-surface \.cc-reading-body pre code\{background:transparent;color:inherit\}/);
  assert.match(css,/\.reading-surface \.cc-outline-mobile nav a\[aria-current=location\]\{background:var\(--reader-block-header\);color:var\(--reader-link\)\}/);
});
