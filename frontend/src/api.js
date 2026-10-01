// One same-origin transport for the original backend; no provider keys in browser storage.
export function createApi(onExpired = () => {}) {
  let csrf = null;
  let generation = 0;
  return {
    setSession(session) { csrf = session?.csrf_token ?? null; generation++; },
    async request(path, value, options = {}) {
      if (!/^\/v1\/[A-Za-z0-9_/-]+$/.test(path) || path.includes('..')) throw new Error('只允许本机业务接口。');
      const revision = generation;
      const response = await fetch(path, {
        method: value === undefined ? 'GET' : 'POST',
        credentials: 'same-origin',
        signal: options.signal,
        headers: value === undefined ? {} : { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
        body: value === undefined ? undefined : JSON.stringify(value),
      });
      let envelope;
      try { envelope = await response.json(); }
      catch { throw new Error('服务暂时无法读取，请确认后台仍在运行。'); }
      if (revision !== generation) throw new Error('会话已变化，请重新读取。');
      if (!response.ok || !envelope.ok) {
        if (response.status === 401) { csrf = null; generation++; onExpired(); }
        throw new Error(envelope.error?.message || '请求未完成。');
      }
      return envelope.data;
    },
  };
}
