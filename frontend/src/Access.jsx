import { useEffect,useState } from 'react';
import { getAgentSetup } from './api';
import { agentSetupView } from './lib/agent-setup';
import { cliPrompt } from './lib/cli-prompt';
import { button,primary,Page,Notice } from './ui';
import { Terminal, Copy } from 'lucide-react';

function CopyBlock({text,label='复制配置',id}) {
  const [feedback,setFeedback]=useState('');
  useEffect(()=>setFeedback(''),[text]);
  return <><details className="cc-cli-full-prompt"><summary>查看完整说明</summary><pre id={id} className="whitespace-pre-wrap break-all">{text}</pre></details><div className="cc-result-actions"><button className={primary} onClick={async()=>{try{await navigator.clipboard.writeText(text);setFeedback('已复制')}catch{setFeedback('请选择上方内容手动复制')}}}><Copy size={15}/>{label}</button><span role="status" className="cc-inline-help">{feedback}</span></div></>;
}

export function Access({api,legacyReadonly=false}) {
  const [setup,setSetup]=useState(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),[method,setMethod]=useState('use');
  useEffect(()=>{let alive=true;const abort=new AbortController();setSetup(null);setError('');getAgentSetup(api,{signal:abort.signal}).then(data=>agentSetupView(data,location.origin,legacyReadonly)).then(data=>{if(alive)setSetup(data)}).catch(()=>{if(alive)setError('接入配置读取失败，请重试。')});return ()=>{alive=false;abort.abort()}},[api,legacyReadonly,revision]);
  return <Page title="让 AI 使用收藏库" action={<button className="cc-text-button" onClick={()=>setRevision(old=>old+1)}>刷新配置</button>}><p className="cc-page-description">复制一段说明，发给能操作本机的 AI，就可以让它找回你的收藏。</p><Notice error>{error}</Notice>{!setup&&!error&&<Notice>正在读取接入配置…</Notice>}{setup&&<>
    <section className="cc-cli-panel"><div className="cc-cli-heading"><span className="cc-account-icon"><Terminal size={23}/></span><div><h2>本机 CLI</h2><p>不需要另填产品密钥，也不用配置 MCP。</p></div></div><div className="cc-evidence-tabs" role="tablist" aria-label="CLI 安装状态"><button role="tab" aria-selected={method==='use'} onClick={()=>setMethod('use')}>已经安装</button><button role="tab" aria-selected={method==='install'} onClick={()=>setMethod('install')}>需要安装</button></div>
      <div className="cc-cli-preview"><h3>{method==='use'?'让 AI 搜索和阅读你已保存的内容':'让 AI 帮你完成安装'}</h3><p>{method==='use'?'说明会附上当前程序和资料库位置。AI 先搜索，再读取总结或转写，回答时保留来源。':'先检查是否已安装，再使用你提供的源码或安装包。不猜测下载地址，也不覆盖已有资料。'}</p></div>
      {method==='use'&&!setup.cli.available?<Notice>当前安装无法提供 CLI 路径，请先选择“需要安装”。</Notice>:<CopyBlock id="agent-cli-prompt" text={cliPrompt(setup,method)} label="复制给 AI 的说明"/>}
    </section>
    <section className="cc-cli-question"><h2>然后直接问它</h2><p>“找一下我收藏里关于动画分镜的教程，整理成操作步骤，附上来源。”</p></section>
    <p className="cc-inline-help">需要能执行本机命令的 AI；普通网页聊天不能直接读取这台电脑。{setup.libraryMode==='legacy_readonly'?'当前连接旧资料库，只读。':''}</p>
  </>}</Page>;
}
