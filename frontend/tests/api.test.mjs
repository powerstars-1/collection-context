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
