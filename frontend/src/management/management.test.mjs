import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setMaxListeners } from "node:events";
import test from "node:test";
import { mountManagement } from "./management.js";

// A narrow DOM harness: no browser, network, user vault, or model requests.
setMaxListeners(0);
const template = readFileSync(new URL("./LegacyPanels.jsx", import.meta.url), "utf8");
class Node extends EventTarget {
  constructor(tag = "div") {
    super();
    Object.assign(this, { tag, children: [], dataset: {}, textContent: "", value: "", checked: false, disabled: false });
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parentElement) node.parentElement.children = node.parentElement.children.filter((child) => child !== node);
      node.parentElement = this;
      this.children.push(node);
    }
  }
  insertBefore(node, previous) {
    this.append(node);
    this.children.splice(this.children.indexOf(node), 1);
    this.children.splice(this.children.indexOf(previous), 0, node);
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  all() { return this.children.flatMap((child) => [child, ...child.all()]); }
  querySelector(selector) {
    const id = selector.match(/^\[id="([^"]+)"\]$/)?.[1];
    const label = selector.match(/^label\[for='([^']+)'\]$/)?.[1];
    return this.all().find((node) => id ? node.id === id : label ? node.tag === "label" && node.htmlFor === label : false) || null;
  }
  querySelectorAll(selector) {
    return this.all().filter((node) => selector === "input:checked" ? node.tag === "input" && node.checked
      : selector === "input[type=password]" ? node.tag === "input" && node.type === "password"
      : selector === "input[type=password], textarea" ? (node.tag === "input" && node.type === "password") || node.tag === "textarea"
      : selector === "fieldset, button" ? ["fieldset", "button"].includes(node.tag)
      : selector === "button" ? node.tag === "button" : false);
  }
  checkValidity() { return true; }
  reportValidity() { return true; }
}
function fixture(api, canManage = true) {
  const root = new Node();
  const nodes = {};
  for (const match of template.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Node(match[1]);
    node.id = match[2];
    node.disabled = /disabled=/.test(match[0]);
    node.type = match[0].match(/type="([^"]+)"/)?.[1];
    node.value = match[0].match(/defaultValue="([^"]+)"/)?.[1] || "";
    root.append(node); nodes[node.id] = node;
  }
  globalThis.document = { createElement: (tag) => new Node(tag) };
  globalThis.confirm = () => false;
  return { root, nodes, controller: mountManagement({ root, api, canManage }) };
}
const overview = { total_items: 0, audio_missing: 0, job_counts: {}, auto_sync: false, auto_process: false };
const management = { jobs: [], prepared: [], next_offset: null, next_prepared_offset: null, automatic: { enabled: false, remaining_new_tasks: 0, paused_count: 0, preparation_blocked_count: 0 } };
const connection = { state: "unverified", folders: [] };
const source = { scopes: [], next_offset: null };
const connectionRun = { state: "disabled", active: false, enabled: false };
const modelData = { configuration_enabled: false, roles: { audio: { configured: false }, vision: { configured: false }, summary: { configured: false } } };
const routes = { "/v1/collections/overview": overview, "/v1/management/overview": management, "/v1/management/sources": source, "/v1/management/connection": connection, "/v1/management/connection-run": connectionRun, "/v1/management/models": modelData };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const defer = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

test("read-only session never queries owner management or writes through disabled controls", async () => {
  const calls = [];
  const { controller, nodes } = fixture(async (path) => { calls.push(path); return routes[path]; }, false);
  await controller.show("connect");
  await controller.show("settings");
  await controller.show("activity");
  assert.deepEqual(calls, ["/v1/collections/overview"]);
  assert.equal(nodes["creator-fields"].disabled, true);
  assert.equal(nodes["auto-fields"].disabled, true);
  const event = new Event("submit", { cancelable: true });
  nodes["creator-form"].dispatchEvent(event);
  await tick();
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(calls, ["/v1/collections/overview"]);
  controller.destroy();
});

test("routes load only their controls; models are never requested as a side effect of tasks", async () => {
  const calls = [];
  const { controller, nodes, root } = fixture(async (path) => { calls.push(path); return routes[path]; });
  await controller.show("connect");
  assert.deepEqual(calls, ["/v1/management/sources", "/v1/management/connection", "/v1/management/connection-run"]);
  assert.equal(nodes.connect.hidden, false);
  assert.equal(nodes["self-source-fields"].disabled, true);
  assert.equal(nodes["folder-source-fields"].disabled, true);
  calls.length = 0;
  await controller.show("activity");
  assert.deepEqual(calls, ["/v1/collections/overview", "/v1/management/overview"]);
  calls.length = 0;
  await controller.show("settings");
  assert.deepEqual(calls, ["/v1/management/models"]);
  assert.equal(nodes["model-forms"].children.length, 3);
  assert.equal(root.querySelectorAll("input[type=password]").length, 3);
  const key = root.querySelector('[id="model-audio-key"]');
  key.value = "synthetic-input-not-a-key";
  await controller.show("activity");
  assert.equal(key.value, "");
  controller.destroy();
  assert.equal(nodes["model-forms"].children.length, 0);
});

test("route epoch discards pending source response before further queries or DOM mutation", async () => {
  const pending = defer(), calls = [];
  const { controller, nodes } = fixture(async (path) => { calls.push(path); return path === "/v1/management/sources" ? pending.promise : routes[path]; });
  const opening = controller.show("connect");
  await controller.show("settings");
  pending.resolve(source);
  await opening;
  assert.deepEqual(calls, ["/v1/management/sources", "/v1/management/models"]);
  assert.equal(nodes["source-list"].children.length, 0);
  assert.equal(nodes.settings.hidden, false);
  controller.destroy();
});

test("destroy invalidates in-flight responses, removes listeners, and is safe to replay", async () => {
  const pending = defer(), calls = [];
  const { controller, nodes, root } = fixture(async (path) => { calls.push(path); return pending.promise; });
  const opening = controller.show("connect");
  controller.destroy();
  pending.resolve(source);
  await opening;
  nodes["refresh-sources"].dispatchEvent(new Event("click"));
  await tick();
  assert.deepEqual(calls, ["/v1/management/sources"]);
  const replay = mountManagement({ root, api: async (path) => routes[path], canManage: true });
  await replay.show("connect");
  assert.equal(nodes["refresh-sources"].disabled, false);
  replay.destroy();
});

test("single-link requires explicit confirmation before even queueing a request", async () => {
  const calls = [];
  const { controller, nodes } = fixture(async (path) => { calls.push(path); return routes[path]; });
  await controller.show("connect");
  calls.length = 0;
  const event = new Event("submit", { cancelable: true });
  event.submitter = new Node("button");
  nodes["link-form"].dispatchEvent(event);
  await tick();
  assert.deepEqual(calls, []);
  assert.match(nodes["link-feedback"].textContent, /须确认/);
  controller.destroy();
});

test("JSX uses true option values and controller has no HTML injection or browser credential storage", () => {
  assert.equal(template.includes("<option defaultValue"), false);
  const source = readFileSync(new URL("./management.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /innerHTML|localStorage|sessionStorage|fetch\(/);
  const ids = [...template.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const match of source.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(match[1]), "missing static control: " + match[1]);
});
