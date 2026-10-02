import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = fileURLToPath(new URL("../", import.meta.url));
const read = (path) => readFileSync(resolve(root, path), "utf8");
const withoutAttribution = (value) => value.replace(/^\/\/[^\n]+\n/, "");
function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(dir, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : /\.(?:jsx?|tsx?)$/.test(path) ? [path] : [];
  });
}

test("original owner WorkbenchLayout is retained byte-for-byte below attribution", () => {
  const source = withoutAttribution(read("src/components/workbench/WorkbenchLayout.tsx"));
  assert.equal(createHash("sha256").update(source).digest("hex"),
    "595b6fccaa924bac875d6833d27ec990b57f6a4d2ba8578edd562421a39f9cc3");
});

test("original material row component keeps selection and layout classes", () => {
  const source = read("src/components/workbench/MaterialList.tsx");
  for (const fragment of ["export function MaterialList", "selectedIds.includes(material.id)",
    "flex w-full items-center gap-3 rounded-2xl border p-3 text-left transition",
    "border-zinc-900 bg-zinc-50", "border-zinc-200 bg-white hover:border-zinc-300",
    "min-w-0 flex-1", "block truncate text-sm text-zinc-900", "onToggle(material.id)"]) {
    assert.ok(source.includes(fragment), `Original MaterialList contract missing: ${fragment}`);
  }
});

test("original top navigation preserves all three responsive density variants", () => {
  const source = read("src/components/TopNav.tsx");
  for (const fragment of ["export function TopNav", 'density === "compact"', 'density === "relaxed"',
    "border-zinc-100/90 bg-white/85", "backdrop-blur-xl", "flex min-w-0 flex-1 items-center justify-center",
    "h-9 w-9 rounded-lg px-0 text-sm md:h-auto md:w-auto md:px-3 md:py-1.5",
    "h-10 w-10 rounded-xl px-0 text-[15px]", "h-10 w-10 rounded-xl px-0 text-[14px]"]) {
    assert.ok(source.includes(fragment), `Original TopNav contract missing: ${fragment}`);
  }
});

test("original sidebar preserves responsive widths and component mapping", () => {
  const source = read("src/components/SubNav.tsx");
  for (const fragment of ["export function SubNav", "cfg.groups.map", "g.items.map",
    "w-[68px] md:w-[220px]", "w-[82px] md:w-[260px]", "w-[74px] md:w-[240px]",
    "border-zinc-100/80 bg-white/60 backdrop-blur-xl", "hidden md:inline", "aria-label={label}"]) {
    assert.ok(source.includes(fragment), `Original SubNav contract missing: ${fragment}`);
  }
});

test("React application actually consumes copied components rather than just their CSS", () => {
  const source = read("src/App.jsx");
  for (const name of ["TopNav", "SubNav", "Library"]) {
    assert.match(source, new RegExp(`<${name}\\b`), `${name} must be rendered`);
  }
  const library = read("src/Library.jsx");
  for (const name of ["WorkbenchLayout", "MaterialList"]) {
    assert.match(library, new RegExp(`<${name}\\b`), `${name} must be rendered in the library`);
  }
  for (const route of ["/connect", "/activity", "/settings", "/access"]) {
    assert.ok(source.includes(route), `Missing management route ${route}`);
  }
});

test("source data and credentials are not persisted or interpolated as HTML", () => {
  for (const path of sourceFiles(resolve(root, "src"))) {
    const source = readFileSync(path, "utf8");
    assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage|dangerouslySetInnerHTML)\b/, path);
    assert.doesNotMatch(source, /\.innerHTML\s*=|insertAdjacentHTML\s*\(/, path);
    assert.doesNotMatch(source, /\b(?:eval|new Function)\s*\(/, path);
  }
});

test("management controller is permission gated and uses the shared request transport", () => {
  const source = read("src/management/management.js");
  assert.match(source, /api:\s*requestApi/);
  assert.match(source, /!canManage/);
  assert.match(source, /AbortController/);
  assert.match(source, /node\.textContent\s*=/);
  assert.doesNotMatch(source, /\bfetch\s*\(/);
  assert.match(source, /key\.value\s*=\s*["']["']/);
});

test("frontend dependencies do not import upstream operational backend", () => {
  const pkg = JSON.parse(read("package.json"));
  assert.ok(pkg.dependencies.react);
  assert.ok(pkg.dependencies["react-dom"]);
  assert.ok(pkg.dependencies["lucide-react"]);
  for (const name of Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })) {
    assert.doesNotMatch(name, /redis|celery|feishu|creator-sync|openapi-client/);
  }
  assert.match(pkg.scripts.test, /node --test/);
  const lock = JSON.parse(read("package-lock.json"));
  for (const [name, dependency] of Object.entries(lock.packages)) {
    assert.ok(name === "" || name.startsWith("node_modules/"));
    assert.ok(!dependency.link);
    if (dependency.resolved) assert.match(dependency.resolved, /^https:\/\//);
  }
});

test("library exposes separate evidence artifacts with text binding and provenance", () => {
  const source = read("src/Library.jsx");
  for (const label of ["原文", "转写", "画面文字", "总结", "可读全文", "图片理解", "备注"]) {
    assert.ok(source.includes(label), `Missing evidence artifact ${label}`);
  }
  assert.match(source, /source_url/);
  assert.match(source, /material_ref/);
  assert.match(source, /read\.next_offset/);
  assert.match(source, /version:read\.version/);
  assert.match(source, /<pre\b[^>]*id="evidence-text"[\s\S]*?\{text\s*\|\|/);
  assert.match(source, /rel="noopener noreferrer"/);
});

test("runtime API access is centralized and direct model calls cannot bypass it", () => {
  for (const path of sourceFiles(resolve(root, "src"))) {
    if (path.endsWith("/api.js")) continue;
    const source = readFileSync(path, "utf8");
    assert.doesNotMatch(source, /\bfetch\s*\(|\bXMLHttpRequest\b|\bnavigator\.sendBeacon\s*\(/, path);
  }
});

test("library search and list share version-bound continuation without hiding search pages", () => {
  const source = read("src/Library.jsx");
  assert.match(source, /append&&listing\?\{offset:listing\.next_offset,version:listing\.version\}/);
  assert.match(source, /\{listing\?\.next_offset!=null && <button/);
  assert.doesNotMatch(source, /!committedQuery&&listing\?\.next_offset/);
  assert.match(source, /if\(revision!==epoch\.current\)return/);
});

test("Access consumes the shared setup transport and cancels stale responses",()=>{
  const source=read('src/Access.jsx');
  assert.match(source,/getAgentSetup\(api,\{signal:controller\.signal\}\)/);
  assert.match(source,/alive=false;controller\.abort\(\)/);
  assert.match(source,/if\(alive\)setState/);
  assert.match(source,/status:'loading',data:null/);
  assert.match(source,/status:'failed',data:null/);
  assert.match(source,/重新读取接入配置/);
  assert.doesNotMatch(source,/createApi\(|setInterval|setTimeout/);
  assert.doesNotMatch(source,/collection-context-mcp\s+|<你的资料库路径>|<旧资料目录路径>/);
});

test("Access clipboard is click-only and contains explicit manual setup and secret boundaries",()=>{
  const source=read('src/Access.jsx');
  assert.equal((source.match(/clipboard\.writeText\(/g)||[]).length,1);
  assert.match(source,/<button[^>]+onClick=\{async\(\)=>\{[\s\S]*?clipboard\.writeText\(text\)/);
  for(const label of ['本机 stdio','不需要另一个 HTTP 口令','不会自动安装依赖','尚未验证宿主调用',
    '复制未完成，请手动选择','不要使用主人管理口令','模型 API Key','旧库只读模式保留原目录']) {
    assert.ok(source.includes(label),`Missing setup boundary: ${label}`);
  }
  assert.doesNotMatch(source,/document\.cookie|csrf_token|localStorage|sessionStorage|\.exec\(|spawn\(/);
  assert.match(source,/配置是否被宿主接受/);
  assert.match(source,/whitespace-pre-wrap break-all/);
});
