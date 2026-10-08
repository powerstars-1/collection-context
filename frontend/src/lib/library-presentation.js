// Presentation only: original titles and evidence remain untouched in the library.
export function displayTitle(value) {
  const original=(value||'无标题内容').trim();
  const firstLine=original.split(/\r?\n/).find(line=>line.trim())||original;
  const withoutTags=firstLine.split(/\s+#/)[0].trim()||firstLine;
  const heading=withoutTags.split(/\s+(?=本期|这是|文档已经|感谢|附上)/)[0].split(/(?<=[。？！!?])\s/)[0];
  return heading.length>72?heading.slice(0,72)+'…':heading;
}

export function itemStates(item) {
  return item.artifact_states||Object.fromEntries(Object.entries(item.artifacts||{}).map(([key,value])=>[key,value.state]));
}

export function noteState(item) {
  const states=itemStates(item);
  if(!Object.keys(states).length&&item.matched_artifacts?.includes('summary'))return {label:'命中总结',tone:'ready'};
  if(!Object.keys(states).length&&item.matched_artifacts?.some(key=>['audio','screen','image'].includes(key)))return {label:'命中正文',tone:'partial'};
  if(states.summary==='ready')return {label:'总结可读',tone:'ready'};
  if(states.summary==='stale')return {label:'总结待更新',tone:'partial'};
  if(states.audio==='ready'||states.screen==='ready'||states.image==='ready')return {label:'正文可读 · 待总结',tone:'partial'};
  if(['audio','screen','image'].some(key=>states[key]==='stale'))return {label:'正文待更新',tone:'partial'};
  if(Object.values(states).includes('unavailable'))return {label:'文件待检查',tone:'failed'};
  return {label:'待生成笔记',tone:'pending'};
}

export function firstReadable(artifacts={}) {
  const notes=['summary','audio','screen','image'];
  return notes.find(key=>artifacts[key]?.state==='ready')||notes.find(key=>artifacts[key]?.state==='stale')||(artifacts.original?.state==='ready'?'original':'summary');
}

export function savedDate(value) {
  if(!value)return '';
  const date=new Date(value);
  return Number.isNaN(date.valueOf())?'':new Intl.DateTimeFormat('zh-CN',{month:'numeric',day:'numeric'}).format(date)+'入库';
}

export function sourceResult(scope) {
  const state=scope.latest_job?.state;
  const report=scope.download_report;
  if(scope.pending_count>0||state==='queued'||state==='running')return {label:state==='running'?'同步中':'等待同步',tone:'pending'};
  if(state==='failed'||state==='blocked')return {label:state==='failed'?'同步失败':'需要处理',tone:'failed'};
  if(report?.failed_count>0)return {label:'已入库 · 原媒体待补',tone:'partial'};
  if(state==='partial')return {label:'已保存部分内容',tone:'partial'};
  if(state==='succeeded')return {label:'本轮同步完成',tone:'ready'};
  if(state==='cancelled')return {label:'已取消',tone:'pending'};
  return {label:'尚未同步',tone:'pending'};
}

export function libraryUrl(kind='',scope='') {
  const params=new URLSearchParams();if(kind)params.set('kind',kind);if(scope)params.set('scope',scope);
  return '/'+(params.size?'?'+params:'');
}
