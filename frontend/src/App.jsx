import { useEffect, useLayoutEffect, useState } from 'react';
import { TopNav } from './components/TopNav';
import { SubNav } from './components/SubNav';
import { Library } from './Library';
import { Access } from './Access';
import { LegacyPanels } from './management/LegacyPanels';
import { mountManagement } from './management/management';
import { createApi } from './api';

const routes = {home:'/',workbench:'/connect',settings:'/settings',profile:'/access'};
function navigate(key) {location.assign(routes[key] || '/');}
const page = location.pathname === '/connect' ? 'connect' : location.pathname === '/activity' ? 'activity' : location.pathname === '/settings' ? 'settings' : location.pathname === '/access' ? 'access' : 'materials';
const navPage = page === 'materials' ? 'home' : page === 'access' ? 'profile' : page === 'settings' ? 'settings' : 'workbench';

function Login({api,onSession}) {
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  return <div className="flex min-h-screen items-center justify-center bg-[#F7F8FC] p-6">
    <div className="w-full max-w-md rounded-[32px] border border-white/80 bg-white p-8 shadow-[0_24px_80px_-32px_rgba(15,23,42,0.18)]">
      <div className="inline-flex rounded-full border border-rose-100 bg-rose-50 px-3 py-1 text-xs font-medium text-rose-500">本地优先 · 自配模型</div>
      <h1 className="mt-4 text-2xl font-semibold text-zinc-900">连接你的收藏库</h1>
      <p className="mt-2 text-sm leading-6 text-zinc-500">使用首次启动生成的产品访问口令。这里不接收抖音密码或模型 API Key。</p>
      <form className="mt-6 space-y-4" onSubmit={async event=>{
        event.preventDefault();setBusy(true);setError('');
        const input=event.currentTarget.elements.namedItem('token');
        const token=input.value;input.value='';
        try {const session=await api.request('/v1/session',{token});api.setSession(session);onSession(session)}
        catch(err){setError(err.message)} finally {setBusy(false)}
      }}>
        <label className="block text-sm text-zinc-600" htmlFor="access-token">产品访问口令</label>
        <input id="access-token" name="token" aria-label="产品访问口令" required type="password" autoComplete="off" className="w-full rounded-2xl border border-zinc-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-rose-300"/>
        {error && <p role="alert" className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">{error}</p>}
        <button disabled={busy} type="submit" className="w-full rounded-2xl bg-rose-500 px-4 py-3 text-sm font-medium text-white transition hover:bg-rose-400 disabled:cursor-not-allowed disabled:bg-rose-300">{busy?'连接中…':'进入收藏库'}</button>
      </form>
    </div>
  </div>;
}

export default function App() {
  const [session,setSession]=useState(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [overview,setOverview]=useState(null);
  const [kind,setKind]=useState('');
  const [density,setDensity]=useState('standard');
  const [client]=useState(()=>createApi(()=>{setSession(null);setLoading(false);setOverview(null)}));
  useEffect(()=>{
    let alive=true;
    client.request('/v1/session').then(value=>{if(alive){client.setSession(value);setSession(value)}})
      .catch(()=>{if(alive)setSession(null)}).finally(()=>{if(alive)setLoading(false)});
    return ()=>{alive=false};
  },[client]);
  useEffect(()=>{
    if(!session)return;
    let alive=true;
    client.request('/v1/collections/overview').then(value=>{if(alive)setOverview(value)}).catch(()=>{});
    return ()=>{alive=false};
  },[session,client]);
  useLayoutEffect(()=>{
    if(!session)return;
    const manager=mountManagement({api:client.request,canManage:session.permissions.includes('ui:manage'),root:document.querySelector('[data-management-root]'),onOverview:setOverview});
    manager.show(page).catch(err=>setError(err.message));
    return ()=>manager.destroy();
  },[session,client]);
  if(loading)return <div className="flex min-h-screen items-center justify-center bg-[#F7F8FC] text-sm text-zinc-500">正在连接本地资料库…</div>;
  if(!session)return <Login api={client} onSession={setSession}/>;
  function onSub(key) {
    if(navPage==='home'){setKind(key==='all'?'':key);return}
    if(navPage==='workbench')location.assign(key==='tasks'?'/activity':'/connect');
  }
  const sub=navPage==='home'?kind||'all':page==='connect'?'sources':page==='activity'?'tasks':page==='settings'?'models':'access';
  const shellPaddingClass=density==='compact'?'p-3':density==='relaxed'?'p-4':'p-3.5';
  const shellRadiusClass=density==='compact'?'rounded-[28px]':density==='relaxed'?'rounded-[36px]':'rounded-[32px]';
  return <div className={`notranslate size-full bg-[#F7F8FC] text-zinc-900 ${shellPaddingClass}`} data-ui-density={density} translate="no">
    <div className={`flex h-full flex-col overflow-hidden border border-white/80 bg-white/80 shadow-[0_24px_80px_-32px_rgba(15,23,42,0.18)] backdrop-blur-xl ${shellRadiusClass}`}>
      <TopNav page={navPage} onChange={navigate} density={density} onSearch={query=>location.assign('/?q='+encodeURIComponent(query))}/>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-zinc-100 bg-white/60 px-4 py-2 text-xs text-zinc-500">
        <span>{session.permissions.includes('ui:manage')?'主人管理会话':'只读会话'}</span>
        <span>已保存 {overview?.total_items??'—'} 条</span><span>尚无转写 {overview?.audio_missing??'—'} 条</span>
        <label className="ml-auto flex items-center gap-2">界面密度<select aria-label="界面密度" value={density} onChange={event=>setDensity(event.target.value)} className="rounded-lg border border-zinc-200 bg-white px-2 py-1"><option value="compact">紧凑</option><option value="standard">标准</option><option value="relaxed">宽松</option></select></label>
        <button type="button" className="rounded-lg border border-zinc-200 px-2 py-1 hover:bg-zinc-50" onClick={async()=>{
          try {await client.request('/v1/session/logout',{});client.setSession(null);setSession(null);setOverview(null)} catch(err){setError(err.message)}
        }}>退出</button>
      </div>
      {error && <p role="alert" className="m-3 rounded-xl bg-rose-50 px-4 py-2 text-sm text-rose-700">{error}</p>}
      <div className="relative flex min-h-0 flex-1 overflow-hidden bg-white/55">
        <SubNav page={navPage} active={sub} onChange={onSub} density={density}/>
        <main id="main-content" className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-[linear-gradient(180deg,rgba(255,255,255,0.40),rgba(248,250,252,0.76))]">
          {page==='materials'&&<Library api={client.request} download={client.download} canManage={session.permissions.includes('ui:manage')} sourceKind={kind}/>}
          {page==='access'&&<div className="min-h-0 flex-1 overflow-y-auto"><Access/></div>}
          <div hidden={page==='materials'||page==='access'} className="min-h-0 flex-1 overflow-y-auto"><LegacyPanels/></div>
        </main>
      </div>
    </div>
  </div>;
}
