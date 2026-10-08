import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
test('the library renders its single-line link form with an explicit submit',async()=>{
 const source=await readFile('src/workspace/Workbench.jsx','utf8');
 assert.match(source, /<form onSubmit=\{save\}/);
 assert.match(source, /aria-label="添加抖音链接"/);
 assert.match(source, /event.nativeEvent.isComposing/);
 assert.match(source, /source_confirmed:true/);
 assert.match(source, /idempotency_key:key.current/);
 assert.match(source, /submittingRef.current/);
 assert.match(source, /link-submit/);
 assert.match(source, /link-status/);
 assert.match(source, /activeJob\(linkJob\)/);
 assert.doesNotMatch(source, /FlowPanels|model.mjs|initialItems/);
});
test('source management preserves its scoped list navigation',async()=>{
 const source=await readFile('src/management/Sources.jsx','utf8');
 assert.doesNotMatch(source, /<AddLink|title="添加抖音链接"/);
 assert.match(source,/查看内容/);
});
