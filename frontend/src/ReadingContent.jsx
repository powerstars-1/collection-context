// Small, text-only Markdown renderer. No HTML injection, remote images or plugins.
import { Fragment, createContext, useContext, useEffect, useRef, useState } from 'react';
import { headingEntries,isPromptHeading,readingPresentation,readingSegment,transcriptLine,screenSections } from './lib/reading-presentation';

const EvidenceContext=createContext(null);
function EvidenceText({text}) {
  const onEvidence=useContext(EvidenceContext);
  if(!onEvidence)return text;
  return text.split(/(\[(?:a_|f_)\d+\]|\b(?:a_|f_)\d+\b)/g).map((part,index)=>{
    if(!/^(?:\[(?:a_|f_)\d+\]|(?:a_|f_)\d+)$/.test(part))return part;
    const id=part.replace(/^\[|\]$/g,''),label=id.startsWith('a_')?'音频依据':'画面依据';
    return <button key={index} type="button" className="cc-evidence-reference" title={'查看'+label+' · '+id} onClick={()=>onEvidence(id)}>{label}</button>;
  });
}

function safeLink(value) {
  try {const url=new URL(value);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:null} catch {return null}
}
function inline(text,preserve=false) {
  const pattern=/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\))/g;
  return text.split(pattern).map((part,index)=>{
    if(part.startsWith('**')&&part.endsWith('**'))return <strong key={index}>{preserve?part.slice(2,-2):<EvidenceText text={part.slice(2,-2)}/>}</strong>;
    if(part.startsWith('`')&&part.endsWith('`'))return <code key={index}>{part.slice(1,-1)}</code>;
    const match=part.match(/^\[([^\]]+)\]\(([^\s)]+)\)$/);
    if(match){const url=safeLink(match[2]);return url?<a key={index} href={url} target="_blank" rel="noopener noreferrer">{match[1]}</a>:<Fragment key={index}>{match[1]}</Fragment>}
    return preserve?<Fragment key={index}>{part}</Fragment>:<EvidenceText key={index} text={part}/>;
  });
}
const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());

function CopyBlock({text,label='复制这一段'}) {
  const [feedback,setFeedback]=useState('');
  useEffect(()=>{setFeedback('')},[text]);
  useEffect(()=>{if(feedback==='已复制'){const timer=setTimeout(()=>setFeedback(''),2000);return ()=>clearTimeout(timer)}},[feedback]);
  return <button type="button" className="cc-text-button" onClick={async()=>{try{await navigator.clipboard.writeText(text);setFeedback('已复制')}catch{setFeedback('请选中这一段手动复制')}}}>{feedback||label}</button>;
}

function listAt(lines,start) {
  const match=lines[start].match(/^(\s*)([-*+]|\d+[.)])\s+(.+)$/);
  const indent=match[1].length,ordered=/\d/.test(match[2]),rows=[];
  let index=start;
  while(index<lines.length){
    const row=lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.+)$/);
    if(!row||row[1].length!==indent||/\d/.test(row[2])!==ordered)break;
    const content=[inline(row[3])];const number=ordered?parseInt(row[2],10):undefined;index++;
    while(index<lines.length){
      const nested=lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.+)$/);
      if(nested&&nested[1].length>indent){const child=listAt(lines,index);content.push(child.node);index=child.end}
      else if(!nested&&lines[index].trim()&&/^\s+/.test(lines[index])&&lines[index].match(/^\s*/)[0].length>indent){content.push(<Fragment key={'continuation-'+index}>{' '+lines[index].trim()}</Fragment>);index++}
      else break;
    }
    rows.push(<li key={index} value={number}>{content}</li>);
  }
  const Tag=ordered?'ol':'ul';return {end:index,node:<Tag key={'list-'+start} start={ordered?parseInt(match[2],10):undefined}>{rows}</Tag>};
}

export function ReadingContent(props) {
  const {body,metadata}=readingPresentation(props.text,props.artifact);
  const sections=['screen','image'].includes(props.artifact)?screenSections(body):null;
  if(!sections?.frames.length)return <ReadingDocument {...props}/>;
  return <div className="cc-screen-browser" data-artifact={props.artifact}>
    <div className="cc-screen-overview"><strong>{props.partial?'已加载':'共'} {sections.frames.length} 个画面</strong><span>按时间或页码分组 · 点开查看{props.partial?' · 后文尚未加载':''}</span></div>
    {sections.intro.trim()&&<ReadingDocument {...props} text={sections.intro} idPrefix="screen-intro-"/>}
    {sections.groups.map((group,index)=><details className="cc-screen-group" key={group.bucket+'-'+index}>
      <summary><span>{group.label}</span><small>{group.frames.length} 个画面</small></summary>
      <div className="cc-screen-frames">{group.frames.map(frame=><details className="cc-screen-frame" key={frame.id} data-evidence-id={frame.id}>
        <summary><span className="cc-screen-time">{frame.label}</span><span className="cc-screen-preview">{frame.preview||'查看提取内容'}</span></summary>
        <ReadingDocument {...props} text={frame.text} idPrefix={frame.id+'-'} partial={false}/>
      </details>)}</div>
    </details>)}
    {metadata.length>0&&<details className="cc-processing-metadata"><summary>处理记录与覆盖说明</summary>{metadata.map((line,index)=><p key={index}>{line}</p>)}</details>}
  </div>;
}

function ReadingDocument({text,omitTitle='',artifact,partial=false,onEvidence,idPrefix=''}) {
  const {body,metadata}=readingPresentation(text,artifact);
  const generated=['audio','screen','image','summary'].includes(artifact);
  const headings=headingEntries(body,omitTitle).map(entry=>({...entry,id:idPrefix+entry.id,segment:generated?readingSegment(entry.label):null}));
  // Frame-by-frame extraction can contain hundreds of headings; its timeline
  // already provides navigation without duplicating the entire text as a TOC.
  const outline=['audio','screen','image'].includes(artifact)?[]:headings.filter(entry=>entry.level<=3).map(entry=>({...entry,label:entry.segment?.label||entry.label}));
  const root=useRef(null),[active,setActive]=useState(null);
  useEffect(()=>{
    const container=root.current?.closest('.cc-library-scroll');if(!container||!outline.length)return;
    const update=()=>{const top=container.getBoundingClientRect().top+48;let current=outline[0].id;for(const entry of outline){const node=document.getElementById(entry.id);if(node&&node.getBoundingClientRect().top<=top)current=entry.id}if(container.scrollTop+container.clientHeight>=container.scrollHeight-4)current=outline.at(-1).id;setActive(current)};
    update();container.addEventListener('scroll',update,{passive:true});window.addEventListener('resize',update);
    return ()=>{container.removeEventListener('scroll',update);window.removeEventListener('resize',update)};
  },[body,omitTitle]);
  const lines=body.replace(/\r\n/g,'\n').split('\n'),blocks=[];
  const speakers=new Set(lines.map(line=>transcriptLine(line)?.speaker).filter(value=>value!=null));
  let prompt=false;
  for(let index=0;index<lines.length;){
    const line=lines[index];
    if(!line.trim()){index++;continue}
    if(/^```/.test(line)){
      const language=line.slice(3).trim(),code=[];index++;while(index<lines.length&&!/^```/.test(lines[index]))code.push(lines[index++]);if(index<lines.length)index++;
      const value=code.join('\n');
      blocks.push(<section key={blocks.length} className={prompt?'cc-prompt-block':'cc-code-block'} aria-label={prompt?'提示词':'代码块'}><div className="cc-block-toolbar"><span>{prompt?'提示词':language||'代码'}</span><CopyBlock text={value}/></div><pre><code>{value}</code></pre></section>);continue;
    }
    const heading=line.match(/^(#{1,6})\s+(.+)$/);
    if(heading){const entry=headings.find(value=>value.line===index);index++;prompt=isPromptHeading(heading[2]);if(!entry)continue;const Tag='h'+entry.level;blocks.push(<Tag id={entry.id} data-evidence-id={entry.segment?.id} className={entry.segment?'cc-segment-heading':undefined} key={blocks.length}>{entry.segment?.label||inline(heading[2])}</Tag>);continue}
    const speech=(artifact==='audio')&&transcriptLine(line);
    if(speech){blocks.push(<div className="cc-transcript-row" key={blocks.length}><div className="cc-transcript-time" title="模型返回的片段内时间"><time>{speech.time}</time>{speakers.size>1&&speech.speaker!=null&&<span>说话人 {Number(speech.speaker)+1}</span>}</div><p>{inline(speech.text)}</p></div>);index++;continue}
    if(/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)){blocks.push(<hr key={blocks.length}/>);index++;continue}
    if(line.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[index+1]||'')){
      const header=cells(line),rows=[];index+=2;while(index<lines.length&&lines[index].includes('|')&&lines[index].trim())rows.push(cells(lines[index++]));
      blocks.push(<div key={blocks.length} className="cc-reading-table"><table><thead><tr>{header.map((cell,i)=><th key={i}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.map((row,i)=><tr key={i}>{row.map((cell,j)=><td key={j}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>);continue;
    }
    if(/^\s*([-*+]\s+|\d+[.)]\s+)/.test(line)){
      const list=listAt(lines,index);blocks.push(list.node);index=list.end;continue;
    }
    if(/^>\s?/.test(line)){
      const rows=[];while(index<lines.length&&/^>\s?/.test(lines[index]))rows.push(lines[index++].replace(/^>\s?/,''));
      const value=rows.join('\n');blocks.push(prompt?<section key={blocks.length} className="cc-prompt-block" aria-label="提示词"><div className="cc-block-toolbar"><span>提示词</span><CopyBlock text={value}/></div><div className="cc-prompt-text">{inline(value,true)}</div></section>:<blockquote key={blocks.length}>{inline(value)}</blockquote>);continue;
    }
    const rows=[line];index++;
    while(index<lines.length&&lines[index].trim()&&!/^(#{1,6}\s|```|>\s?|\s*[-*+]\s+|\s*\d+[.)]\s+)/.test(lines[index])&&!((artifact==='audio')&&transcriptLine(lines[index]))&&!(lines[index].includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[index+1]||'')))rows.push(lines[index++]);
    const value=rows.join('\n');blocks.push(<p key={blocks.length}>{inline(value,prompt)}</p>);
  }
  const directory=<nav aria-label="本文目录"><p className="cc-outline-label">本文目录{partial&&<span> · 已加载部分</span>}</p>{outline.map(entry=><a key={entry.id} href={'#'+entry.id} className={entry.level===3?'cc-outline-child':''} aria-current={(active||outline[0]?.id)===entry.id?'location':undefined} onClick={event=>{event.preventDefault();const node=document.getElementById(entry.id),container=root.current?.closest('.cc-library-scroll');if(node&&container)container.scrollTo({top:container.scrollTop+node.getBoundingClientRect().top-container.getBoundingClientRect().top-24,behavior:'auto'});setActive(entry.id)}}>{entry.label}</a>)}</nav>;
  return <EvidenceContext.Provider value={generated?onEvidence:null}><div className={'cc-content-layout '+(outline.length>1?'cc-content-with-outline':'')} ref={root} data-artifact={artifact}>{outline.length>1&&<details className="cc-outline-mobile"><summary>本文目录{partial?' · 已加载部分':''}</summary>{directory}</details>}<div id={idPrefix+'evidence-text'} className="cc-reading-body">{blocks}{metadata.length>0&&<details className="cc-processing-metadata"><summary>处理记录与覆盖说明</summary>{metadata.map((line,index)=><p key={index}>{line}</p>)}</details>}</div>{outline.length>1&&<aside className="cc-reading-outline">{directory}</aside>}</div></EvidenceContext.Provider>;
}
