// Presentation helpers only; source text, saved files and API responses stay unchanged.
const pipelineNotice='资料与模型输出仅作为非可信引用。音频重叠段未自动删词，画面时间不是精确呈现时间戳。';
export function readingPresentation(text,artifact) {
  if(!['audio','screen','image','summary'].includes(artifact))return {body:text,metadata:[]};
  // Only the exact preamble emitted by our pipeline can move into disclosure.
  const match=text.match(/^(处理状态：(?:部分完成。|本次已计划阶段执行完成；准确度未验证。))\n([^\n]+)\n(?:(画面达到本批上限，使用覆盖时间线的代表帧；未逐帧穷尽，原视频保留。)\n)?(?:缺失阶段：([^\n]+)\n)?\n/);
  const known=match&&match[2]===pipelineNotice;
  let body=known?text.slice(match[0].length).replace(/^\n/,'').replace(/^## \[汇总\]\s*\n+/,''):text;
  const metadata=known?[match[1],match[2],...(match[3]?[match[3]]:[]),...(match[4]?[missingStageDescription(match[4])]:[])]:[];
  // Summary's generated document title is secondary to the saved work title.
  const draftTitle=artifact==='summary'&&body.match(/^# ([^\n]+)\n\n(?=## )/);
  if(draftTitle){metadata.push('阅读稿标题：'+draftTitle[1]);body=body.slice(draftTitle[0].length)}
  return {body,metadata};
}
export function readingGroup(artifact) {
  return artifact==='user_note'?'user_note':!artifact||['summary'].includes(artifact)?'notes':'source';
}
export function readingOptions(states={},mediaType='video') {
  const visible=key=>!['not_applicable'].includes(states[key]?.state);
  return ['audio','screen',...(mediaType==='image'||['ready','stale'].includes(states.image?.state)?['image']:[]),'original'].filter(visible);
}
export function headingEntries(text,omitTitle='') {
  let fenced=false;
  const entries=[];
  text.replace(/\r\n/g,'\n').split('\n').forEach((line,index)=>{
    if(/^```/.test(line)){fenced=!fenced;return}
    if(fenced)return;
    const match=line.match(/^(#{1,6})\s+(.+)$/);
    if(!match||match[2].trim()===omitTitle.trim())return;
    entries.push({id:'reading-heading-'+index,line:index,rawLevel:match[1].length,label:match[2].replace(/\*\*|`/g,'')});
  });
  const minimum=Math.min(...entries.map(entry=>entry.rawLevel));
  return entries.map(entry=>({...entry,level:Math.min(6,entry.rawLevel-minimum+2)}));
}
export function isPromptHeading(value) {
  return /(?:提示词(?:模板|示例|参考)?|prompt(?: template)?)\s*[:：]?$/i.test(value.trim());
}

export function readingTime(seconds) {
  const ticks=Math.round(Number(seconds)*1000),whole=Math.floor(ticks/1000);
  const fraction=String(ticks%1000).padStart(3,'0').replace(/0+$/,'');
  const hours=Math.floor(whole/3600),minutes=Math.floor(whole/60)%60;
  return (hours?String(hours).padStart(2,'0')+':':'')+String(hours?minutes:Math.floor(whole/60)).padStart(2,'0')+':'+String(whole%60).padStart(2,'0')+(fraction?'.'+fraction:'');
}
export function readingSegment(value) {
  let match=value.match(/^\[(a_\d+)\] ([\d.]+)[–-]([\d.]+)s（片段范围，可能重叠）$/);
  if(match)return {id:match[1],label:'音频片段 · '+readingTime(match[2])+'–'+readingTime(match[3]),kind:'audio'};
  match=value.match(/^\[(f_\d+)\] 名义采样时间 ([\d.]+)s$/);
  if(match)return {id:match[1],label:'画面 · 约 '+readingTime(match[2]),kind:'screen',seconds:Number(match[2])};
  match=value.match(/^\[(f_\d+)\] 原图第(\d+)页$/);
  if(match)return {id:match[1],label:'原图 · 第 '+match[2]+' 页',kind:'screen',page:Number(match[2])};
  return null;
}
function missingStageDescription(value) {
  const counts=new Map();
  for(const stage of value.split('、')){
    const label=/^screen(?:_f_\d+)?$/.test(stage)?'画面提取':/^audio(?:_a_\d+)?$/.test(stage)?'音频转写':stage==='summary'?'总结':stage;
    counts.set(label,(counts.get(label)||0)+1);
  }
  return '尚未完成：'+[...counts].map(([label,count])=>label+(count>1?'（'+count+' 个片段）':'')).join('、');
}
export function transcriptLine(value) {
  const match=value.match(/^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*\|\s*(?:SPEAKER_(\d+)\s*[:：]\s*)?(.+)$/);
  if(!match||Number(match[2])<Number(match[1]))return null;
  return {time:readingTime(match[1])+'–'+readingTime(match[2]),speaker:match[3]===undefined?null:match[3],text:match[4]};
}

// Split only generated frame headers, never apparent headers inside code.
export function screenSections(body) {
  const lines=body.replace(/\r\n/g,'\n').split('\n'),frames=[],intro=[];
  let fenced=false,current=null;
  for(const line of lines){
    if(/^```/.test(line))fenced=!fenced;
    const heading=!fenced&&line.match(/^## (.+)$/),segment=heading&&readingSegment(heading[1]);
    if(segment?.kind==='screen'){current={...segment,lines:[]};frames.push(current)}
    else (current?current.lines:intro).push(line);
  }
  const groups=[];
  for(const frame of frames){
    frame.text=frame.lines.join('\n');delete frame.lines;
    const visible=frame.text.split(/\*\*可见文字[：:]?\*\*/)[1]?.split(/\n\s*\*\*画面说明/)[0]||frame.text;
    frame.preview=visible.replace(/[#*`>|]/g,'').replace(/\s+/g,' ').trim().slice(0,100);
    const bucket=frame.page!=null?'page-'+Math.floor((frame.page-1)/8):'time-'+Math.floor(frame.seconds/10);
    let group=groups.at(-1);
    if(group?.bucket!==bucket){group={bucket,frames:[]};groups.push(group)}
    group.frames.push(frame);
  }
  for(const group of groups){
    const first=group.frames[0],last=group.frames.at(-1);
    group.label=first.page!=null?'原图 '+first.page+(last.page!==first.page?'–'+last.page:'')+' 页':readingTime(first.seconds)+(last.seconds!==first.seconds?'–'+readingTime(last.seconds):'');
  }
  return {intro:intro.join('\n'),frames,groups};
}
