import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {createRequire} from 'node:module';
import {collectionFolders,folderLabel} from '../src/workspace/collectionFolders.mjs';

const compiled=await build({stdin:{contents:"export {CollectionFolderPicker} from './src/workspace/CollectionFolderPicker.jsx';export {renderToStaticMarkup} from 'react-dom/server';export {createElement} from 'react';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
const module={exports:{}};
new Function('require','module','exports',compiled.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
const {CollectionFolderPicker,renderToStaticMarkup,createElement}=module.exports;
const show=props=>renderToStaticMarkup(createElement(CollectionFolderPicker,{onChange(){},...props}));

test('folder choices merge saved membership and configured names by scope, not title',()=>{
  const folders=collectionFolders([
    {kind:'collection',scope_id:'a',title:'AI'},
    {kind:'collection',scope_id:'b',title:'AI',account_matches:false},
    {kind:'saved',scope_id:'c',title:'我的收藏'},
  ],[{kind:'collection',scope_id:'a'},{kind:'collection',scope_id:'d'},{kind:'liked',scope_id:'e'}]);
  assert.deepEqual(folders.map(f=>f.id),['a','d','b']);
  assert.equal(folders[0].name,'AI');
  assert.equal(folders[1].historical,true);
  assert.equal(folderLabel(folders[2]),'AI · 之前的账号');
});

test('no saved or configured folders means no fabricated categories',()=>{
  assert.deepEqual(collectionFolders(),[]);
  assert.match(show({}),/尚未添加指定收藏夹/);
});

test('picker selects exact scope and safely renders folder titles',()=>{
  const html=show({folders:[{id:'a',name:'AI <教程>'},{id:'b',name:'AI <教程>'}],value:'b'});
  assert.match(html,/value="b" selected=""/);
  assert.match(html,/AI &lt;教程&gt;/);
  assert.match(html,/全部收藏夹/);
});

test('unknown selected scope is preserved while names load or fail',()=>{
  const html=show({value:'removed-scope',error:'offline'});
  assert.match(html,/value="removed-scope" selected=""/);
  assert.match(html,/当前收藏夹/);
  assert.match(html,/收藏夹名称未能读取/);
  assert.match(html,/重试/);
});
