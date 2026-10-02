import { useEffect, useState } from 'react';
import { getAgentSetup } from './api';
import { agentSetupView } from './lib/agent-setup';

const button = 'rounded-xl border border-zinc-200 px-3 py-2 text-sm text-zinc-700 transition hover:bg-zinc-50 disabled:opacity-45';
const code = 'mt-4 max-w-full overflow-auto whitespace-pre-wrap break-all rounded-2xl bg-zinc-50 p-4 text-xs leading-6 text-zinc-600';

function CopyBlock({text,label,id}) {
  const [feedback,setFeedback] = useState('');
  const [busy,setBusy] = useState(false);
  useEffect(()=>{setFeedback('')},[text]);
  return <>
    <pre id={id} tabIndex={0} className={code}>{text}</pre>
    <div className="mt-3 flex flex-wrap items-center gap-3">
      <button type="button" className={button} disabled={busy} onClick={async()=>{
        setBusy(true);setFeedback('');
        try {
          if(!navigator.clipboard?.writeText)throw new Error('clipboard_unavailable');
          await navigator.clipboard.writeText(text);setFeedback('已复制。请到你的 AI 工具中手动配置；尚未验证宿主调用。');
        } catch {setFeedback('复制未完成，请手动选择上方内容复制。')}
        finally {setBusy(false)}
      }}>{busy?'复制中…':label}</button>
      <span role="status" aria-live="polite" className="text-xs leading-6 text-zinc-500">{feedback}</span>
    </div>
  </>;
}

export function Access({api,legacyReadonly=false}) {
  const [state,setState] = useState({status:'loading',data:null});
  const [attempt,setAttempt] = useState(0);
  useEffect(()=>{
    let alive = true;
    const controller = new AbortController();
    setState({status:'loading',data:null});
    getAgentSetup(api,{signal:controller.signal})
      .then(data=>agentSetupView(data,location.origin,legacyReadonly))
      .then(data=>{if(alive)setState({status:'ready',data})})
      .catch(()=>{if(alive)setState({status:'failed',data:null})});
    return ()=>{alive=false;controller.abort()};
  },[api,legacyReadonly,attempt]);
  const setup = state.data;
  return <div className="mx-auto min-w-0 max-w-3xl space-y-5 px-6 py-8">
    <div className="rounded-3xl border border-zinc-200 bg-white p-6"><div className="text-xs text-zinc-400">CLI / MCP / HTTP</div><h1 className="mt-2 text-2xl font-semibold text-zinc-900">把收藏交给你的 AI。</h1><p className="mt-3 text-sm leading-7 text-zinc-500">三个入口共用一个后端和同一份资料库。默认搜索、受控读取和处理状态；添加、批量提取与计费需单独授权。</p><p className="mt-3 text-xs leading-6 text-zinc-500">这里读取当前资料库与当前安装位置生成配置，不自动修改你的 AI 工具。页面可见或复制完成，都不代表宿主已成功调用。</p></div>
    {state.status==='loading'&&<p role="status" aria-live="polite" className="rounded-3xl border border-zinc-200 bg-white p-6 text-sm text-zinc-500">正在读取当前安装版与资料库的接入配置…</p>}
    {state.status==='failed'&&<section className="rounded-3xl border border-rose-200 bg-white p-6"><p role="alert" className="text-sm leading-7 text-rose-700">接入配置读取失败。请确认后台仍在运行、页面会话有效，且前后端版本一致；不会使用猜测的命令或旧配置代替。</p><button type="button" className={button+' mt-3'} onClick={()=>setAttempt(value=>value+1)}>重新读取接入配置</button></section>}
    {setup&&<>
      <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">当前连接 · {setup.libraryMode==='legacy_readonly'?'旧资料库只读':'产品资料库'}</h2><p className="mt-3 text-sm leading-7 text-zinc-500">{setup.libraryMode==='legacy_readonly'?'旧库只读模式保留原目录。配置直接指向旧资料目录，服务的独立访问配置目录不含这些资料；不初始化、不同步、不改写旧库。':'配置指向当前资料库。返回稳定资料引用，再按需读取原文、转写或画面；已有非空库不要重新初始化。'}</p><ul className="mt-3 space-y-2 text-sm text-zinc-600">{setup.tools.map(tool=><li key={tool.name}>{tool.label} · <code className="break-all text-xs">{tool.name}</code></li>)}</ul></section>
      <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">可选 MCP · 本机 stdio</h2><p className="mt-3 text-xs leading-6 text-zinc-500">运行方式：{setup.runtimeLabel}{setup.mcp.available&&setup.mcp.reason?` · ${setup.mcp.reason}`:''}</p><p className="mt-3 text-sm leading-7 text-zinc-500">将下面的 JSON 合并到同一台电脑上 AI 工具的 MCP 配置中，再由你启动或启用。这个 stdio 接入不需要另一个 HTTP 口令，也不需要模型 API Key；配置包含本机程序和资料库路径，请勿公开分享。</p>
        {setup.mcp.available?<CopyBlock id="agent-mcp-configuration" text={setup.mcp.text} label="复制 MCP 配置"/>:<div role="status" className="mt-4 rounded-2xl bg-amber-50 p-4 text-sm leading-7 text-amber-900"><p>MCP 暂不可用：{setup.mcp.reason}</p><p>若安装包缺少配套控制台程序（console 伴侣），请使用完整安装包后重新读取。这里不会猜测裸 CLI 名称，也不会自动安装依赖。</p></div>}
        <p className="mt-3 text-xs leading-6 text-zinc-500">此配置只提供上面的只读工具。宿主仍需自行授权本机文件访问；添加链接和批量处理不在这份配置中。</p></section>
      <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">本机 CLI</h2><p className="mt-3 text-sm leading-7 text-zinc-500">下面是当前安装程序的精确参数清单（JSON），保留路径中的空格和中文。它不是可直接粘贴到终端的 shell 命令；供支持 command / args 的工具逐项配置，不会在这里执行。</p>
        {setup.cli.available?<CopyBlock id="agent-cli-examples" text={setup.cli.text} label="复制 CLI 参数清单"/>:<p role="status" className="mt-3 text-sm leading-7 text-amber-900">当前运行环境无法提供经过确认的 CLI 路径。请检查完整安装包中的控制台程序，再重新读取配置。</p>}</section>
      <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">认证 HTTP</h2><p className="mt-2 text-sm leading-7 text-zinc-500">给 AI 使用专用只读产品口令，通过 Authorization: Bearer 请求。不要使用主人管理口令，也不要把模型 API Key 当成产品口令。</p><CopyBlock id="agent-http-example" text={setup.http.example} label="复制 HTTP 示例（仅占位口令）"/><p className="mt-3 break-all text-xs leading-6 text-zinc-500">当前页面服务地址：{setup.http.baseUrl}</p><pre className={code}>{setup.http.pathsText}</pre><p className="mt-3 text-xs leading-6 text-zinc-500">将 {'{material_ref}'} 替换为搜索返回的资料引用，并进行 URL 编码。只读口令须由部署主人单独创建；页面不读取、展示或复制当前会话凭据，也不自动创建口令。</p><p className="mt-3 text-xs leading-6 text-zinc-500">{setup.http.sameComputerOnly?'当前地址仅供本机使用。':''}{setup.http.remoteNote}</p></section>
      <p className="text-xs leading-6 text-zinc-400">本页未调用模型或平台。配置是否被宿主接受、首次搜索能否返回资料，仍需你在宿主侧验证；接入测试不等同于识别质量验收。</p>
    </>}
  </div>;
}
