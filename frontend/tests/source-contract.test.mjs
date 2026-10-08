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

test("source data and credentials are not persisted or interpolated as HTML", () => {
  for (const path of sourceFiles(resolve(root, "src"))) {
    const source = readFileSync(path, "utf8");
    assert.doesNotMatch(source, /\blocalStorage\b/,path);
    assert.doesNotMatch(source, /\b(?:sessionStorage|dangerouslySetInnerHTML)\b/, path);
    assert.doesNotMatch(source, /\.innerHTML\s*=|insertAdjacentHTML\s*\(/, path);
    assert.doesNotMatch(source, /\b(?:eval|new Function)\s*\(/, path);
  }
});

test("account controls include explicit logout without a forced expiry timer", () => {
  const source = read("src/management/Sources.jsx");
  assert.match(source, /onClick=\{logout\}>退出登录/);
  assert.match(source, /managementApi\(api,'connection-logout'\)/);
  assert.match(source, /已退出抖音，已有资料已保留/);
  assert.doesNotMatch(source, /连接需更新|重新登录|expires_at|setTimeout/);
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

test("reader exposes each real evidence artifact and safe Markdown", () => {
  const source=read("src/workspace/Reader.jsx");
  for(const label of ["总结","转写","画面","平台原文","我的备注"])assert.ok(source.includes(label));
  assert.match(source, /useArtifact/);
  assert.match(source, /read.result.next_offset/);
  assert.match(source, /<ReadingContent/);
  assert.match(source, /aria-controls="reading-panel"/);
  assert.match(read("src/workspace/presentation.mjs"), /safeSource/);
});

test("runtime API access is centralized and direct model calls cannot bypass it", () => {
  for (const path of sourceFiles(resolve(root, "src"))) {
    if (path.endsWith("/api.js")) continue;
    const source = readFileSync(path, "utf8");
    assert.doesNotMatch(source, /\bfetch\s*\(|\bXMLHttpRequest\b|\bnavigator\.sendBeacon\s*\(/, path);
  }
});

test("library search and list retain version-bound continuation", () => {
 const source=read("src/workspace/useLibrary.jsx");
 assert.match(source,/offset:prior.next_offset,version:prior.version/);
 assert.match(source,/if\(token!==epoch.current\)return/);
 assert.match(read("src/workspace/Workbench.jsx"),/library.listing\?\.next_offset!=null/);
});

test("Access consumes the shared setup transport and cancels stale responses",()=>{
  const source=read('src/Access.jsx');
  assert.match(source,/getAgentSetup\(api,\{signal:abort\.signal\}\)/);
  assert.match(source,/alive=false;abort\.abort\(\)/);
  assert.match(source,/if\(alive\)setSetup/);
  assert.match(source,/setSetup\(null\);setError\(''\)/);
  assert.match(source,/if\(alive\)setError/);
  assert.match(source,/刷新配置/);
  assert.doesNotMatch(source,/createApi\(|setInterval|setTimeout/);
  assert.doesNotMatch(source,/collection-context-mcp\s+|<你的资料库路径>|<旧资料目录路径>/);
});

test("Access is CLI first with click-only copying, real paths and manual fallback",()=>{
  const source=read('src/Access.jsx');
  assert.equal((source.match(/clipboard\.writeText\(/g)||[]).length,1);
  assert.match(source,/<button[^>]+onClick=\{async\(\)=>\{[\s\S]*?clipboard\.writeText\(text\)/);
  for(const label of ['已复制','请选择上方内容手动复制','已经安装','需要安装','复制给 AI 的说明',
    '本机 CLI','能执行本机命令','当前连接旧资料库']) {
    assert.ok(source.includes(label),`Missing setup boundary: ${label}`);
  }
  assert.doesNotMatch(source,/document\.cookie|csrf_token|localStorage|sessionStorage|\.exec\(|spawn\(/);
  assert.doesNotMatch(source,/agent-mcp-configuration|agent-http-example|method==='mcp'/);
  assert.match(source,/cliPrompt\(setup,method\)/);
  assert.match(source,/whitespace-pre-wrap break-all/);
});
