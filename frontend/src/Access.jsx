export function Access({legacyReadonly=false}) {
  const origin = location.origin;
  const call = `${origin}/v1/collections/search`;
  const libraryArgs = legacyReadonly ? '--workspace "<旧资料目录路径>" --legacy-vault' : '--workspace "<你的资料库路径>"';
  return <div className="mx-auto max-w-3xl space-y-5 px-6 py-8">
    <div className="rounded-3xl border border-zinc-200 bg-white p-6"><div className="text-xs text-zinc-400">CLI / MCP / HTTP</div><h1 className="mt-2 text-2xl font-semibold text-zinc-900">把收藏交给你的 AI。</h1><p className="mt-3 text-sm leading-7 text-zinc-500">三个入口共用一个后端和同一份资料库。默认搜索、受控读取和处理状态；添加、批量提取与计费需单独授权。</p></div>
    <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">认证 HTTP</h2><p className="mt-2 text-sm leading-7 text-zinc-500">给 AI 使用专用只读产品口令，通过 Authorization: Bearer 请求。不要使用主人管理口令，也不要把模型 API Key 当成产品口令。</p><pre className="mt-4 overflow-auto rounded-2xl bg-zinc-50 p-4 text-xs leading-6 text-zinc-600">{`POST ${call}\nAuthorization: Bearer <专用只读口令>\nContent-Type: application/json\n\n{"query":"UI 提示词","limit":3}`}</pre><p className="mt-3 text-xs text-zinc-500">口令须由部署主人创建。页面不自动创建、展示或复制凭据。</p></section>
    <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">本机 CLI</h2><pre className="mt-4 overflow-auto rounded-2xl bg-zinc-50 p-4 text-xs leading-6 text-zinc-600">{`collection-context ${libraryArgs} search --query "UI 提示词" --limit 3`}</pre><p className="mt-3 text-sm leading-7 text-zinc-500">{legacyReadonly?'旧库只读模式保留原目录。这里填写旧资料目录，服务的独立访问配置目录不含这些资料。':'返回稳定资料引用，再按需读取原文、转写或画面。已有非空库不要重新初始化。'}</p></section>
    <pre className="overflow-auto rounded-2xl bg-zinc-50 p-4 text-xs leading-6 text-zinc-600">{`collection-context-mcp ${libraryArgs}`}</pre>
    <section className="rounded-3xl border border-zinc-200 bg-white p-6"><h2 className="font-semibold text-zinc-900">可选 MCP</h2><p className="mt-3 text-sm leading-7 text-zinc-500">安装 MCP 可选依赖，用 collection-context-mcp 指向同一资料库。搜索、读取、状态默认只读；远程 AI 可使用上面的认证 HTTP，不必额外部署一个后端。</p><p className="mt-3 text-xs leading-6 text-zinc-400">实际参数和宿主配置以运行说明为准。接入测试不等同于识别质量验收。</p></section>
  </div>;
}
