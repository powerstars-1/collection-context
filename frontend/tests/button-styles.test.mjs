import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {createRequire} from 'node:module';

async function buttonStyles(){
  const output=await build({stdin:{contents:"export {button,primary} from './src/ui.jsx';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
  const compiled={exports:{}};
  new Function('require','module','exports',output.outputFiles[0].text)(createRequire(import.meta.url),compiled,compiled.exports);
  return compiled.exports;
}

test('primary buttons have exactly one base foreground, background and border color',async()=>{
  const {primary,button}=await buttonStyles();
  const tokens=primary.split(' ');
  assert.deepEqual(tokens.filter(value=>/^text-(?:white|black|zinc-\d+)$/.test(value)),['text-white']);
  assert.deepEqual(tokens.filter(value=>/^bg-(?:white|black|(?:zinc|blue)-\d+)$/.test(value)),['bg-blue-700']);
  assert.deepEqual(tokens.filter(value=>/^border-(?:zinc|blue)-\d+$/.test(value)),['border-blue-700']);
  assert.ok(button.split(' ').includes('text-zinc-700'));
  assert.ok(!tokens.includes('text-zinc-700'));
});

test('disabled primary buttons use a distinct readable light surface, not faded dark text',async()=>{
  const {primary}=await buttonStyles();
  for(const value of ['disabled:bg-zinc-200','disabled:text-zinc-600','disabled:border-zinc-200','disabled:cursor-not-allowed','enabled:hover:bg-blue-800'])assert.ok(primary.split(' ').includes(value));
  assert.doesNotMatch(primary,/disabled:opacity-/);
});
