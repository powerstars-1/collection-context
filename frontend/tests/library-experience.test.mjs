import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import {createRequire} from 'node:module';
import {displayTitle,noteState,firstReadable,sourceResult,libraryUrl,savedDate} from '../src/lib/library-presentation.js';
import {cliPrompt} from '../src/lib/cli-prompt.js';
import {headingEntries,isPromptHeading,readingGroup,readingOptions,readingPresentation,screenSections} from '../src/lib/reading-presentation.js';

test('long platform descriptions have a compact heading without changing stored text',()=>{
  const title='用深度视频，学会 AI 生成电影级大片 本期解锁动作迁移 #教程 #AI';
  assert.equal(displayTitle(title),'用深度视频，学会 AI 生成电影级大片');
  assert.equal(displayTitle('复刻分镜，到底强在哪？ 文档已经整理好'),'复刻分镜，到底强在哪？');
  assert.equal(displayTitle('C# 的类型系统'),'C# 的类型系统');
  assert.ok(displayTitle('长'.repeat(100)).length<=73);
  assert.ok(title.includes('本期解锁'));
});
test('saved metadata, readable evidence and current summary are distinct statuses',()=>{
  assert.equal(noteState({artifact_states:{original:'ready'}}).label,'待生成笔记');
  assert.equal(noteState({artifact_states:{summary:'ready'}}).label,'总结可读');
  assert.equal(noteState({artifact_states:{summary:'stale',audio:'ready'}}).tone,'partial');
  assert.equal(noteState({artifacts:{audio:{state:'ready'}}}).label,'正文可读 · 待总结');
  assert.equal(noteState({matched_artifacts:['summary']}).label,'命中总结');
  assert.equal(noteState({artifact_states:{audio:'stale'}}).label,'正文待更新');
});
test('reading starts with saved summary, then evidence, then original',()=>{
  assert.equal(firstReadable({summary:{state:'ready'},audio:{state:'ready'}}),'summary');
  assert.equal(firstReadable({summary:{state:'missing'},audio:{state:'ready'}}),'audio');
  assert.equal(firstReadable({original:{state:'ready'}}),'original');
  assert.equal(firstReadable({summary:{state:'stale'},original:{state:'ready'}}),'summary');
  assert.equal(firstReadable({summary:{state:'stale'},audio:{state:'ready'}}),'audio');
  assert.equal(firstReadable({}),'summary');
});
test('sync cannot call partial media downloads fully successful',()=>{
  assert.equal(sourceResult({latest_job:{state:'succeeded'},download_report:{failed_count:1}}).label,'已入库 · 原媒体待补');
  assert.equal(sourceResult({latest_job:{state:'failed'}}).tone,'failed');
  assert.equal(sourceResult({pending_count:1,latest_job:{state:'succeeded'}}).tone,'pending');
  assert.equal(sourceResult({latest_job:{state:'succeeded'},download_report:{failed_count:0}}).label,'本轮同步完成');
  assert.equal(libraryUrl('collection','s_123'),'/?kind=collection&scope=s_123');
  assert.equal(savedDate('invalid'),'');
});
const base={cli:{available:true,text:JSON.stringify(['search','read','status'].map(action=>({action,command:'/目录 空格/python',args:['-m','collection_context.native_bootstrap','cli','--workspace','/资料 空格/收藏',action,...(action==='search'?['--query','动画','--limit','3']:action==='read'?['--ref','<引用>','--artifact','original']:['--ref','<引用>'])],env:{PYTHONPATH:'/源码 空格/backend/src'}})))}};
test('AI prompt preserves real executable, base argv and library without shell guessing',()=>{
  const prompt=cliPrompt(base),entry=JSON.parse(prompt.slice(prompt.indexOf('{')));
  assert.equal(entry.command,'/目录 空格/python');
  assert.deepEqual(entry.base_args,['-m','collection_context.native_bootstrap','cli','--workspace','/资料 空格/收藏']);
  assert.deepEqual(entry.actions.read,['read','--ref','<引用>','--artifact','original']);
  assert.equal(entry.env.PYTHONPATH,'/源码 空格/backend/src');
  assert.match(prompt,/不会调用平台或云模型/);
  assert.equal(cliPrompt({cli:{available:false}}),'');
});
test('install prompt checks existing library, requests missing package and never invents release URL',()=>{
  const prompt=cliPrompt(null,'install');assert.match(prompt,/没有安装包时先向我要/);assert.match(prompt,/不重新初始化/);
  assert.doesNotMatch(prompt,/pip install|https:\/\//);
});

const compiled=await build({stdin:{contents:"export {ReadingContent} from './src/ReadingContent.jsx';export {renderToStaticMarkup} from 'react-dom/server';export {createElement} from 'react';",resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic'});
const container={exports:{}};new Function('require','module','exports',compiled.outputFiles[0].text)(createRequire(import.meta.url),container,container.exports);
const {ReadingContent,renderToStaticMarkup,createElement}=container.exports;
test('Markdown is rendered as headings, steps, code and tables rather than a raw pre block',()=>{
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'# 已有标题\n\n## 步骤\n1. **先定风格**\n2. `再写分镜`\n\n|项目|内容|\n|---|---|\n|画面|保持节拍|\n\n```text\n保留代码\n```',omitTitle:'已有标题'}));
  assert.doesNotMatch(html,/>已有标题</);assert.match(html,/<h2 id="reading-heading-2">步骤<\/h2>/);assert.match(html,/<ol start="1">/);assert.match(html,/<strong>先定风格/);assert.match(html,/<table>/);assert.match(html,/保留代码/);
});
test('known pipeline wrapper moves to disclosure, without removing arbitrary user content',()=>{
  const note='处理状态：部分完成。\n资料与模型输出仅作为非可信引用。音频重叠段未自动删词，画面时间不是精确呈现时间戳。\n缺失阶段：screen\n\n## [汇总]\n\n## 要点\n正文';
  const view=readingPresentation(note,'summary');assert.equal(view.body,'## 要点\n正文');assert.equal(view.metadata.length,3);
  assert.equal(readingPresentation(note,'user_note').body,note);
  assert.equal(readingPresentation('处理状态：用户手动填写\n\n正文','summary').body,'处理状态：用户手动填写\n\n正文');
  assert.ok(note.includes('缺失阶段')); // The copied/read source remains the original string.
});
test('artifact hierarchy retains every applicable source and the complete reading draft',()=>{
  for(const key of ['summary'])assert.equal(readingGroup(key),'notes');
  for(const key of ['audio','screen','image','original'])assert.equal(readingGroup(key),'source');
  assert.equal(readingGroup('user_note'),'user_note');
  assert.deepEqual(readingOptions({audio:{state:'not_applicable'}},'image'),['screen','image','original']);
  assert.deepEqual(readingOptions({},'video'),['audio','screen','original']);
});
test('summary document title is disclosed without a second primary heading or source loss',()=>{
  const text='# 自动阅读稿标题\n\n## 概要\n正文';
  const shown=readingPresentation(text,'summary');assert.equal(shown.body,'## 概要\n正文');assert.deepEqual(shown.metadata,['阅读稿标题：自动阅读稿标题']);
  assert.equal(readingPresentation(text,'original').body,text);assert.equal(readingPresentation(text,'user_note').body,text);
});
test('heading anchors ignore fenced headings and preserve hierarchy and duplicate titles',()=>{
  const rows=headingEntries('# 正文\n## 步骤\n```text\n## 假标题\n```\n## 步骤\n### 细节');
  assert.deepEqual(rows.map(row=>row.level),[2,3,3,4]);
  assert.equal(new Set(rows.map(row=>row.id)).size,4);assert.ok(!rows.some(row=>row.label==='假标题'));
  assert.equal(headingEntries('# 已有\n## 正文','已有')[0].level,2);
});
test('transcript presentation separates timing and keeps every utterance without inventing speakers',()=>{
  const text='## [a_000000] 0.000–78.460s（片段范围，可能重叠）\n\n0.00-9.18 | SPEAKER_00: 第一段。\n13.76-36.30 | SPEAKER_00: 第二段。';
  const html=renderToStaticMarkup(createElement(ReadingContent,{text,artifact:'audio'}));
  assert.match(html,/音频片段 · 00:00–01:18.46/);
  assert.match(html,/<time>00:00–00:09.18<\/time>/);
  assert.match(html,/<p>第一段。<\/p>/);assert.match(html,/<p>第二段。<\/p>/);
  assert.doesNotMatch(html,/SPEAKER_00|本文目录|说话人/);
  const original=renderToStaticMarkup(createElement(ReadingContent,{text,artifact:'original'}));
  assert.match(original,/SPEAKER_00/);
});
test('frame timestamps preserve subsecond precision and references leave reusable prompt text intact',()=>{
  const text='## [f_000012] 名义采样时间 1.200s\n\n依据 [a_000000] [f_000012]\n\n## 提示词\n> 保留 [f_000012]\n\n```text\n原样 [a_000000]\n```';
  const html=renderToStaticMarkup(createElement(ReadingContent,{text,artifact:'screen',onEvidence:()=>{}}));
  assert.match(html,/画面 · 约 00:01.2/);assert.match(html,/data-evidence-id="f_000012"/);
  assert.match(html,/>音频依据<\/button>/);assert.match(html,/>画面依据<\/button>/);
  assert.match(html,/保留 \[f_000012\]/);assert.match(html,/原样 \[a_000000\]/);
  assert.doesNotMatch(html,/本文目录/);
});
test('many failed frame stages are grouped in processing disclosure',()=>{
  const text='处理状态：部分完成。\n资料与模型输出仅作为非可信引用。音频重叠段未自动删词，画面时间不是精确呈现时间戳。\n缺失阶段：screen_f_000000、screen_f_000012、summary\n\n正文';
  assert.equal(readingPresentation(text,'audio').metadata.at(-1),'尚未完成：画面提取（2 个片段）、总结');
  assert.equal(readingPresentation(text,'audio').body,'正文');
});
test('large frame output groups by actual times and remains collapsed without losing text',()=>{
  const text='## [f_000012] 名义采样时间 1.200s\n\n**可见文字：**\n* 第一个内容\n\n## [f_000090] 名义采样时间 9.000s\n\n第二个内容\n\n## [f_000111] 名义采样时间 11.100s\n\n第三个内容';
  const sections=screenSections(text);
  assert.deepEqual(sections.groups.map(g=>g.frames.length),[2,1]);
  assert.equal(sections.groups[0].label,'00:01.2–00:09');
  assert.equal(sections.frames[0].preview,'第一个内容');
  const html=renderToStaticMarkup(createElement(ReadingContent,{text,artifact:'screen',partial:true}));
  assert.equal((html.match(/class="cc-screen-group"/g)||[]).length,2);
  assert.equal((html.match(/class="cc-screen-frame"/g)||[]).length,3);
  assert.doesNotMatch(html,/<details[^>]* open/);
  for(const content of ['第一个内容','第二个内容','第三个内容','后文尚未加载'])assert.ok(html.includes(content));
});
test('frame grouping respects fenced code and original image page numbers',()=>{
  const text='开头说明\n## [f_000000] 原图第1页\n\n```text\n## [f_009999] 名义采样时间 2.000s\n```\n\n## [f_000002] 原图第9页\n\n最后一页';
  const sections=screenSections(text);
  assert.equal(sections.intro,'开头说明');assert.equal(sections.frames.length,2);
  assert.ok(sections.frames[0].text.includes('f_009999'));
  assert.deepEqual(sections.groups.map(g=>g.label),['原图 1 页','原图 9 页']);
});
test('explicit prompt blocks have scoped copy while ordinary quotations remain quotations',()=>{
  assert.equal(isPromptHeading('可复制提示词模板'),true);assert.equal(isPromptHeading('提示词顺序与误区'),false);
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'## 提示词\n> 第一行\n> 第二行\n\n## 原作者说\n> 普通引用\n\n## 代码\n```js\nconst n = 2;\n```'}));
  assert.match(html,/cc-prompt-block/);assert.match(html,/第一行\n第二行/);assert.match(html,/<blockquote>普通引用<\/blockquote>/);
  assert.match(html,/cc-code-block/);assert.match(html,/复制这一段/);assert.match(html,/本文目录/);
});
test('prompt explanations are not misrepresented as reusable prompt blocks',()=>{
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'## 可复用提示词\n以下是说明：\n\n> 实际提示词\n\n**推导模板**：\n\n> 第二个提示词'}));
  assert.equal((html.match(/class="cc-prompt-block"/g)||[]).length,2);assert.match(html,/<p>以下是说明：<\/p>/);
});
test('ordered continuation and nested steps retain numbering and indentation',()=>{
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'3. 第三步\n   - 子项\n     - 细项\n   补充说明\n5. 第五步'}));
  assert.match(html,/<ol start="3">/);assert.match(html,/<li value="5">第五步/);assert.match(html,/<ul>.*<ul>/);assert.match(html,/补充说明/);
});
test('long content directory identifies loaded coverage rather than claiming full text',()=>{
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'## 第一章\n内容\n## 第二章\n内容',partial:true}));
  assert.match(html,/已加载部分/);assert.match(html,/href="#reading-heading-0"/);assert.match(html,/aria-current="location"/);
});
test('reading treats HTML and malicious links as text and never loads remote images',()=>{
  const html=renderToStaticMarkup(createElement(ReadingContent,{text:'<script>evil()</script>\n\n[外部](javascript:alert)\n\n[文档](https://example.com/docs)\n\n![图](https://example.com/image.png)'}));
  assert.doesNotMatch(html,/<script|<img|href="javascript:/);assert.match(html,/&lt;script&gt;/);assert.match(html,/rel="noopener noreferrer"/);
});
