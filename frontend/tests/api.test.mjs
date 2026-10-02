import assert from "node:assert/strict";
import test from "node:test";
import { createApi } from "../src/api.js";

function installFetch(t, implementation) {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  t.after(() => { globalThis.fetch = original; });
}
function response(status, value) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

test("GET requests are same-origin and unwrap the backend data envelope", async (t) => {
  const calls = [];
  installFetch(t, async (path, options) => {
    calls.push({ path, options });
    return response(200, { ok: true, data: { items: ["owned-fixture"] } });
  });
  const api = createApi();
  assert.deepEqual(await api.request("/v1/session"), { items: ["owned-fixture"] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/v1/session");
  assert.equal(calls[0].options.method ?? "GET", "GET");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.body, undefined);
});

test("POST uses an in-memory session CSRF token and JSON body", async (t) => {
  let captured;
  installFetch(t, async (path, options) => {
    captured = { path, options };
    return response(200, { ok: true, data: { accepted: true } });
  });
  const api = createApi();
  api.setSession({ csrf_token: "synthetic-csrf-only" });
  assert.deepEqual(await api.request("/v1/management/link-submit", { source_confirmed: true }), { accepted: true });
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.credentials, "same-origin");
  const headers = new Headers(captured.options.headers);
  assert.equal(headers.get("X-CSRF-Token"), "synthetic-csrf-only");
  assert.match(headers.get("Content-Type"), /application\/json/);
  assert.deepEqual(JSON.parse(captured.options.body), { source_confirmed: true });
});

test("clearing a session removes its CSRF token from later requests", async (t) => {
  let captured;
  installFetch(t, async (_, options) => {
    captured = options;
    return response(200, { ok: true, data: {} });
  });
  const api = createApi();
  api.setSession({ csrf_token: "synthetic-csrf-only" });
  api.setSession(null);
  await api.request("/v1/session", {});
  assert.equal(new Headers(captured.headers).has("X-CSRF-Token"), false);
});

test("offsite, protocol-relative and traversal routes never reach fetch", async (t) => {
  let requests = 0;
  installFetch(t, async () => { requests += 1; return response(200, { ok: true, data: {} }); });
  const api = createApi();
  for (const path of ["https://supplier.invalid/v1/chat/completions", "//supplier.invalid/v1/models",
    "/api/v1/models", "/v1/../private", "/v1/session?key=not-allowed"]) {
    await assert.rejects(async () => api.request(path), `Unexpected permitted API path: ${path}`);
  }
  assert.equal(requests, 0);
});

test("backend authorization expiry is reported and not retried", async (t) => {
  let requests = 0;
  let expirations = 0;
  installFetch(t, async () => {
    requests += 1;
    return response(401, { ok: false, error: { code: "unauthorized", message: "session expired" } });
  });
  const api = createApi(() => { expirations += 1; });
  await assert.rejects(api.request("/v1/session"));
  assert.equal(expirations, 1);
  assert.equal(requests, 1);
});

test("legacy status permits only the encoded m1 reference and no general escapes", async(t)=>{
  const paths=[];
  installFetch(t,async(path)=>{paths.push(path);return response(200,{ok:true,data:{}})});
  const api=createApi();
  await api.request('/v1/collections/m1%3AQUJD_123/status');
  for(const path of ['/v1/collections/m1%3AQUJD%2F123/status','/v1/collections/m1%3AQUJD%252F/status',
    '/v1/%2e%2e/private','/v1/collections/m1%3AQUJD/frames']) {
    await assert.rejects(()=>api.request(path));
  }
  assert.deepEqual(paths,['/v1/collections/m1%3AQUJD_123/status']);
});

test("backend envelope failures do not masquerade as successful data", async (t) => {
  installFetch(t, async () => response(200, { ok: false, error: { code: "forbidden", message: "not permitted" } }));
  await assert.rejects(createApi().request("/v1/management/overview", {}));
});

test("transport passes AbortSignal to fetch without initiating other calls", async (t) => {
  const controller = new AbortController();
  let captured;
  installFetch(t, async (_, options) => {
    captured = options;
    return response(200, { ok: true, data: {} });
  });
  await createApi().request("/v1/session", undefined, { signal: controller.signal });
  assert.equal(captured.signal, controller.signal);
});

test("a non-JSON error page is a failure, not document content", async (t) => {
  installFetch(t, async () => new Response("<html>proxy error</html>", { status: 502 }));
  await assert.rejects(createApi().request("/v1/session"));
});

test("an unrelated JSON document is rejected instead of accepted as an API envelope", async (t) => {
  installFetch(t, async () => response(200, { items: ["unexpected"] }));
  await assert.rejects(createApi().request("/v1/session"));
});

test("a response from a replaced session is rejected", async (t) => {
  let finish;
  installFetch(t, () => new Promise((resolve) => { finish = resolve; }));
  const api = createApi();
  api.setSession({ csrf_token: "old-synthetic-session" });
  const pending = api.request("/v1/session");
  api.setSession(null);
  finish(response(200, { ok: true, data: { old_private_data: "must-not-render" } }));
  await assert.rejects(pending);
});

test("401 removes CSRF before any subsequent request", async (t) => {
  const sent = [];
  installFetch(t, async (_, options) => {
    sent.push(new Headers(options.headers));
    return sent.length === 1
      ? response(401, { ok: false, error: { message: "expired" } })
      : response(200, { ok: true, data: {} });
  });
  const api = createApi();
  api.setSession({ csrf_token: "old-synthetic-session" });
  await assert.rejects(api.request("/v1/management/overview", {}));
  await api.request("/v1/session", {});
  assert.equal(sent[0].get("X-CSRF-Token"), "old-synthetic-session");
  assert.equal(sent[1].has("X-CSRF-Token"), false);
});

test("export download is authenticated same-origin POST with an exact allowlist", async(t)=>{
  let captured;
  installFetch(t,async(path,options)=>{captured={path,options};return new Response('PK-fixture',{headers:{'Content-Type':'application/zip'}})});
  const api=createApi();api.setSession({csrf_token:'fixture-csrf'});
  const blob=await api.download('/v1/management/library/export',{confirmed:true});
  assert.equal(blob.size,10);
  assert.equal(captured.options.method,'POST');
  assert.equal(captured.options.credentials,'same-origin');
  assert.equal(new Headers(captured.options.headers).get('X-CSRF-Token'),'fixture-csrf');
  assert.throws(()=>api.download('/v1/session',{}));
});

test("download error envelopes are not saved as ZIP archives",async(t)=>{
  installFetch(t,async()=>response(403,{ok:false,error:{message:'not allowed'}}));
  await assert.rejects(createApi().download('/v1/management/library/export',{}));
});

test("an untrusted MIME response and an oversized ZIP are rejected",async(t)=>{
  installFetch(t,async()=>new Response('not a download',{headers:{'Content-Type':'text/html'}}));
  await assert.rejects(createApi().download('/v1/management/library/export',{}));
  globalThis.fetch=async()=>new Response('PK',{headers:{'Content-Type':'application/zip','Content-Length':'66000000'}});
  await assert.rejects(createApi().download('/v1/management/library/export',{}));
});

test("download cannot return data for a replaced session",async(t)=>{
  let finish;
  installFetch(t,()=>new Promise(resolve=>{finish=resolve}));
  const api=createApi();api.setSession({csrf_token:'fixture'});
  const pending=api.download('/v1/management/library/export',{});
  api.setSession(null);finish(new Response('PK',{headers:{'Content-Type':'application/zip'}}));
  await assert.rejects(pending);
});

test("rapid page requests keep at most two in flight without retrying",async(t)=>{
  const finish=[];let calls=0;
  installFetch(t,()=>{calls++;return new Promise(resolve=>finish.push(resolve))});
  const api=createApi();
  const first=api.request('/v1/session');const second=api.request('/v1/session');const third=api.request('/v1/session');
  assert.equal(calls,2);
  finish[0](response(200,{ok:true,data:{}}));await first;
  await Promise.resolve();assert.equal(calls,3);
  finish[1](response(200,{ok:true,data:{}}));finish[2](response(200,{ok:true,data:{}}));
  await Promise.all([second,third]);assert.equal(calls,3);
});

test("queued requests never send after the session changes",async(t)=>{
  const finish=[];let calls=0;
  installFetch(t,()=>{calls++;return new Promise(resolve=>finish.push(resolve))});
  const api=createApi();api.setSession({csrf_token:'old-fixture'});
  const first=api.request('/v1/session');const second=api.request('/v1/session');const third=api.request('/v1/management/library/overview',{});
  const rejected=Promise.all([first,second,third].map(value=>assert.rejects(value)));
  api.setSession(null);
  finish[0](response(200,{ok:true,data:{}}));finish[1](response(200,{ok:true,data:{}}));
  await rejected;assert.equal(calls,2);
});
