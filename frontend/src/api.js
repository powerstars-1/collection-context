// One same-origin transport for the original backend; no provider keys in browser storage.
export async function getAgentSetup(request,options={}) {
  if(typeof request!=='function')throw new Error('接入配置需要当前页面会话。');
  return request('/v1/agent-setup',undefined,{signal:options.signal});
}

export function createApi(onExpired = () => {}) {
  let csrf = null;
  let generation = 0;
  let active = 0;
  const pending = [];
  async function transport(path, value, options = {}, binary = false) {
    const ordinary = typeof path==='string' && /^\/v1\/[A-Za-z0-9_/-]+$/.test(path);
    const legacyStatus = typeof path==='string' && /^\/v1\/collections\/m1%3A[A-Za-z0-9_-]{1,1397}\/status$/.test(path);
    if ((!ordinary&&!legacyStatus) || path.includes('..')) throw new Error('只允许本机业务接口。');
    const revision = generation;
    if(active>=2&&pending.length>=16) throw new Error('页面待处理请求过多，请等待当前操作完成。');
    if(active<2) active++;
    else await new Promise(resolve=>pending.push(resolve));
    try {
    if (revision !== generation) throw new Error('会话已变化，请重新读取。');
    if(options.signal?.aborted) throw new DOMException('Request cancelled','AbortError');
    function current() {if (revision !== generation) throw new Error('会话已变化，请重新读取。');}
    const readOnly = value === undefined
      ? path === '/v1/session' || path === '/v1/agent-setup' || path === '/v1/collections/overview' || /^\/v1\/collections\/.+\/(status|frames)(\/[^/]+\/f_[^/]+)?$/.test(path)
      : ['/v1/collections/list','/v1/collections/search','/v1/collections/read','/v1/management/overview','/v1/management/activity','/v1/management/preferences'].includes(path);
    let response;
    for(let attempt=0;attempt<3;attempt++){
      current();
      if(options.signal?.aborted)throw new DOMException('Request cancelled','AbortError');
      response = await fetch(path, {
        method: value === undefined ? 'GET' : 'POST', credentials: 'same-origin', signal: options.signal,
        headers: value === undefined ? {} : { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
        body: value === undefined ? undefined : JSON.stringify(value),
      });
      current();
      if(!readOnly||attempt===2||response.status!==429)break;
      const denied=await response.clone().json().catch(()=>null);
      if(denied?.error?.code!=='concurrency_limited')break;
      // Another open tab can briefly occupy the local reader slots. Retry only
      // confirmed read routes; submissions, model calls and fees never retry.
      await new Promise((resolve,reject)=>{
        const signal=options.signal;
        const cancel=()=>{clearTimeout(timer);reject(new DOMException('Request cancelled','AbortError'))};
        const timer=setTimeout(()=>{signal?.removeEventListener('abort',cancel);resolve()},attempt?450:150);
        if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
      });
    }
    current();
    if (binary === 'image' && response.ok) {
      const mime=response.headers.get('content-type')?.split(';')[0];
      if(!['image/png','image/jpeg','image/webp'].includes(mime))throw new Error('图片格式无法读取。');
      const cap=10_000_000;
      if(Number(response.headers.get('content-length'))>cap)throw new Error('图片超过读取上限。');
      const blob=await response.blob();current();
      if(blob.size>cap)throw new Error('图片超过读取上限。');
      return blob;
    }
    if (binary && response.ok && response.headers.get('content-type')?.split(';')[0] === 'application/zip') {
      const cap = 65_000_000;
      if (Number(response.headers.get('content-length')) > cap) throw new Error('导出包超过下载上限。');
      const blob = await response.blob();current();
      if (blob.size > cap) throw new Error('导出包超过下载上限。');
      return blob;
    }
    let envelope;
    try { envelope = await response.json(); }
    catch { throw new Error('服务暂时无法读取，请确认后台仍在运行。'); }
    current();
    if (!response.ok || !envelope.ok || binary) {
      if (response.status === 401) { csrf = null; generation++; onExpired(); }
      const error=new Error(envelope.error?.message || '请求未完成。');
      error.code=envelope.error?.code;error.status=response.status;
      throw error;
    }
    if(/^\/v1\/management\/(source-submit|source-edit|source-remove|source-timer|self-source-create|source-create|link-submit|media-prepare|history|retry|cancel|connection-start|connection-logout|connection-cancel|creator-resolve|model-check)$/.test(path))globalThis.window?.dispatchEvent(new Event('collection-context-changed'));
    return envelope.data;
    } finally {
      active--;
      const next=pending.shift();if(next){active++;next()}
    }
  }
  return {
    setSession(session) { csrf = session?.csrf_token ?? null; generation++; },
    request: transport,
    image(path,options={}) {
      if(typeof path!=='string'||!/^\/v1\/collections\/i_[A-Za-z0-9_-]+\/frames\/[A-Za-z0-9_-]{1,160}\/f_[A-Za-z0-9_-]+$/.test(path))throw new Error('只允许读取这条资料已保存的图片。');
      return transport(path,undefined,options,'image');
    },
    download(path, value, options = {}) {
      if (path !== '/v1/management/library/export' || value === undefined) throw new Error('只允许确认后的资料导出。');
      return transport(path, value, options, true);
    },
  };
}
