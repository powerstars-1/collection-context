// One same-origin transport for the original backend; no provider keys in browser storage.
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
    const response = await fetch(path, {
      method: value === undefined ? 'GET' : 'POST', credentials: 'same-origin', signal: options.signal,
      headers: value === undefined ? {} : { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
      body: value === undefined ? undefined : JSON.stringify(value),
    });
    function current() {if (revision !== generation) throw new Error('会话已变化，请重新读取。');}
    current();
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
      throw new Error(envelope.error?.message || '请求未完成。');
    }
    return envelope.data;
    } finally {
      active--;
      const next=pending.shift();if(next){active++;next()}
    }
  }
  return {
    setSession(session) { csrf = session?.csrf_token ?? null; generation++; },
    request: transport,
    download(path, value, options = {}) {
      if (path !== '/v1/management/library/export' || value === undefined) throw new Error('只允许确认后的资料导出。');
      return transport(path, value, options, true);
    },
  };
}
