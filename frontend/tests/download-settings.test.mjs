import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
test('download size is a normal setting, saved to backend and reset explicitly',async()=>{
 const source=await readFile(new URL('../src/management/Settings.jsx',import.meta.url),'utf8');
 assert.match(source,/单个文件最大下载大小（MB）/);
 assert.match(source,/max_download_mb:maximum/);
 assert.match(source,/setMaxDownload\(1024\)/);
 assert.match(source,/Number\.isInteger\(maximum\)/);
 assert.match(source,/不改变模型上传限制/);
});
