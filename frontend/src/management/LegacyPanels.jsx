import { memo } from "react";

// Static owned management subtree: React never reconciles controller-generated children.
export const LegacyPanels = memo(function LegacyPanels() {
  return (<div data-management-root className="min-w-0 space-y-5">
<section id="connect" className="space-y-5" hidden={true}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">SOURCES / 连接资料</p>
                <h1 className="text-2xl font-semibold text-zinc-900">从你选择的来源开始。</h1>
                <p className="text-sm leading-6 text-zinc-500">
                  首版聚焦抖音。每种来源保留关系，同一作品只存一份。
                </p>
              </div>
            </div>
            <div className="rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600">
              同步范围可登记、手动排队或配置定时。执行须另行启动已授权的后台，并使用本产品独立登录；页面不会请求模型。下方显示刷新时的后台快照，超过30秒需重新确认。
            </div>
            <section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="添加单条作品">
              <h2 className="text-lg font-semibold text-zinc-900">添加一条抖音作品</h2>
              <p className="text-sm leading-6 text-zinc-500">粘贴完整作品链接、短链接或含一个链接的分享文字。这里只登记一条，不扩大到博主或历史列表。</p>
              <form id="link-form" className="space-y-4"><fieldset className="space-y-4" id="link-fields" disabled={true}>
                <label className="block space-y-2" htmlFor="link-url"><span className="block text-sm font-medium text-zinc-700">作品链接或分享文字</span><textarea className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="link-url" rows="3" maxLength="8192" required></textarea></label>
                <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="link-download" type="checkbox" />下载并准备原媒体；不请求模型</span></label>
                <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="link-confirmed" type="checkbox" />确认仅访问此作品及上述下载范围</span></label>
                <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">登记单条任务</button>
              </fieldset></form>
              <p className="text-sm leading-6 text-zinc-500">后台须单独允许来源访问。此确认不启用收费提取；若已另行开启新增自动提取，仍受原有授权和调用上限约束。</p>
              <p id="link-feedback" role="status" aria-live="polite"></p>
              <div id="link-result"></div>
              <button id="link-refresh" type="button" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40" disabled={true}>刷新本条任务</button>
            </section>
            <section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="独立连接证明">
              <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold text-zinc-900">本人账号与收藏夹</h2><button id="refresh-connection" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">刷新本地连接</button></div>
              <p id="connection-state" className="rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600" role="status"></p>
              <p className="text-sm leading-6 text-zinc-500">刷新只读取本地证明。主动连接才访问平台；有桌面时登录窗口出现在后台所在电脑，无桌面只验证已有独立登录。证明15分钟后过期，保存范围不代表同步或授权模型。收藏夹兼容性仍待真实账号验证。</p>
              <fieldset className="space-y-4" id="connection-actions" disabled={true}>
                <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="connection-access-confirmed" type="checkbox" />允许本次独立浏览器访问本人账号页面；不获取作品、不调用模型</span></label>
                <div className="flex flex-wrap gap-2"><button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" id="connection-login" type="button">打开独立登录</button><button id="connection-check" type="button" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">验证已有登录</button><button id="connection-folders" type="button" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">发现收藏夹</button></div>
              </fieldset>
              <p id="connection-run-state" className="text-sm leading-6 text-zinc-500" role="status" aria-live="polite">后台连接入口尚未读取。</p>
              <button id="connection-cancel" type="button" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40" disabled={true}>取消本次验证</button>
              <p className="text-sm leading-6 text-zinc-500">验证期间可继续看资料；退出本页不会自动取消已确认的验证。取消在当前页面操作结束后生效，已确认结果可能保留。</p>
            </section>
            <div className="grid gap-5 xl:grid-cols-3">
              <article className="rounded-3xl border border-zinc-200 bg-white p-6">
                <span className="inline-flex rounded-md bg-zinc-100 px-2 py-1 text-xs text-zinc-600">本人来源</span>
                <h2 className="text-lg font-semibold text-zinc-900">喜欢与收藏</h2>
                <p>保留兴趣线索。未知的喜欢时间，不会冒充最近点赞。</p>
                <form id="self-source-form" className="space-y-4"><fieldset className="space-y-4" id="self-source-fields" disabled={true}>
                  <label className="block space-y-2" htmlFor="self-source-kind"><span className="block text-sm font-medium text-zinc-700">选择本人列表</span><select className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="self-source-kind"><option value="saved">收藏</option><option value="liked">喜欢</option></select></label>
                  <label className="block space-y-2" htmlFor="self-source-limit"><span className="block text-sm font-medium text-zinc-700">每批最多作品数</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="self-source-limit" type="number" min="1" max="20" defaultValue="5" required /></label>
                  <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="self-source-download" type="checkbox" />同步时下载并准备媒体，不调用模型</span></label>
                  <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="self-source-confirmed" type="checkbox" required />确认本人列表；本次仅登记范围</span></label>
                  <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">保存本人范围</button>
                </fieldset></form>
              </article>
              <article className="rounded-3xl border border-zinc-200 bg-white p-6">
                <span className="inline-flex rounded-md bg-zinc-100 px-2 py-1 text-xs text-zinc-600">按范围选择</span>
                <h2 className="text-lg font-semibold text-zinc-900">指定收藏夹</h2>
                <p>只同步你选择的收藏夹。同一作品的其他来源仍会保留。</p>
                <form id="folder-source-form" className="space-y-4"><fieldset className="space-y-4" id="folder-source-fields" disabled={true}>
                  <label className="block space-y-2" htmlFor="folder-source-id"><span className="block text-sm font-medium text-zinc-700">已观察收藏夹</span><select className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="folder-source-id" required></select></label>
                  <p id="folder-coverage" className="text-sm leading-6 text-zinc-500"></p>
                  <label className="block space-y-2" htmlFor="folder-source-limit"><span className="block text-sm font-medium text-zinc-700">每批最多作品数</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="folder-source-limit" type="number" min="1" max="20" defaultValue="5" required /></label>
                  <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="folder-source-download" type="checkbox" />同步时下载并准备媒体，不调用模型</span></label>
                  <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="folder-source-confirmed" type="checkbox" required />确认此收藏夹；本次仅登记范围</span></label>
                  <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">保存收藏夹范围</button>
                </fieldset></form>
              </article>
              <article className="rounded-3xl border border-zinc-200 bg-white p-6">
                <span className="inline-flex rounded-md bg-zinc-100 px-2 py-1 text-xs text-zinc-600">资料来源</span>
                <h2 className="text-lg font-semibold text-zinc-900">博主已发布作品</h2>
                <p>登记完整博主主页，只收集已发布作品。首次建议5条；保存范围不会立即访问抖音。</p>
                <form id="creator-form" className="space-y-4">
                  <fieldset className="space-y-4" id="creator-fields" disabled={true}>
                    <label className="block space-y-2" htmlFor="creator-url"><span className="block text-sm font-medium text-zinc-700">完整抖音博主主页</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="creator-url" type="url" required maxLength="8192" placeholder="https://www.douyin.com/user/…" /></label>
                    <label className="block space-y-2" htmlFor="creator-limit"><span className="block text-sm font-medium text-zinc-700">每批最多作品数</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="creator-limit" type="number" min="1" max="20" defaultValue="5" required /></label>
                    <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="creator-download" type="checkbox" />同步时下载附件并准备音轨与画面（不调用模型）</span></label>
                    <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">保存博主范围</button>
                  </fieldset>
                </form>
              </article>
            </div>
            <section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="同步范围">
              <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold text-zinc-900">已登记同步范围</h2><button id="refresh-sources" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">刷新范围</button></div>
              <p id="source-state" className="text-sm leading-6 text-zinc-500" role="status"></p>
              <p className="text-sm leading-6 text-zinc-500">每轮观察最新指定数量，不代表全历史同步。关闭定时不会取消手动批次；已经开始的访问可能继续，暂停在检查点生效。同步与收费提取开关独立。</p>
              <p id="source-feedback" className="text-sm text-rose-600" role="status" aria-live="polite"></p>
              <div id="source-list"></div>
              <button id="sources-more" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40" hidden={true}>下一页同步范围</button>
            </section>
          </section>
          <section id="activity" className="space-y-5" hidden={true}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">PROCESSING / 处理与设置</p>
                <h1 className="text-2xl font-semibold text-zinc-900">知道资料走到了哪一步。</h1>
                <p className="text-sm leading-6 text-zinc-500">查状态不会重试提取，也不会产生模型费用。</p>
              </div>
            </div>
            <div id="overview"></div>
            <p id="management-state" className="rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600" role="status"></p>
            <p id="management-feedback" role="status" aria-live="polite"></p>
            <div>
              <article className="rounded-3xl border border-zinc-200 bg-white p-6">
                <h2 className="text-lg font-semibold text-zinc-900">自动运行</h2>
                <p id="auto-state">加载状态…</p>
                <form id="auto-form" className="space-y-4">
                  <fieldset className="space-y-4" id="auto-fields" disabled={true}>
                    <label className="block space-y-2" htmlFor="auto-enabled"><span className="block text-sm font-medium text-zinc-700">新增资料自动提取</span><select className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="auto-enabled"><option value="no">关闭</option><option value="yes">开启</option></select></label>
                    <label className="block space-y-2" htmlFor="auto-calls"><span className="block text-sm font-medium text-zinc-700">每条最多云请求次数</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="auto-calls" type="number" min="0" max="1000" defaultValue="2" required /></label>
                    <label className="block space-y-2" htmlFor="auto-tasks"><span className="block text-sm font-medium text-zinc-700">本次最多新增任务</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="auto-tasks" type="number" min="1" max="1000" defaultValue="5" required /></label>
                    <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="auto-fee" type="checkbox" />我确认上传媒体及此调用上限；金额未知</span></label>
                    <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">保存自动规则</button>
                  </fieldset>
                </form>
              </article>

            </div>

            <section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="历史补处理">
              <h2 className="text-lg font-semibold text-zinc-900">选择历史资料</h2>
              <p className="text-sm leading-6 text-zinc-500">仅列出已准备媒体的资料，每批最多20条。提交只登记任务，须另行授权后台执行器；可能复用成功阶段，未知费用不会自动重试。</p>
              <form id="history-form" className="space-y-4">
                <fieldset className="space-y-4" id="history-fields" disabled={true}>
                  <div id="prepared-items"></div>
                  <label className="block space-y-2" htmlFor="history-calls"><span className="block text-sm font-medium text-zinc-700">每条最多云请求次数</span><input className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400" id="history-calls" type="number" min="0" max="1000" defaultValue="2" required /></label>
                  <p id="history-budget" className="text-sm leading-6 text-zinc-500"></p>
                  <label className="block space-y-2 text-sm text-zinc-600"><span className="flex items-start gap-2"><input id="history-fee" type="checkbox" required />确认所选媒体上传及调用上限；金额未知</span></label>
                  <button className="rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40" type="submit">提交所选历史批次</button>
                </fieldset>
              </form>
              <button id="prepared-more" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40" hidden={true}>下一页历史资料</button>
            </section>
            <section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="任务状态">
              <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold text-zinc-900">已登记任务</h2><button id="refresh-tasks" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">刷新状态</button></div>
              <p id="worker-state" className="rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600" role="status"></p>
              <p className="text-sm leading-6 text-zinc-500">在线状态是刷新时的内核所有权快照，不能证明处理进度或识别成功；离开页面或长时间未刷新后需重新确认。取消只停止后续阶段，已发出请求可能继续计费。</p>
              <div id="task-list"></div>
              <button id="tasks-more" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40" hidden={true}>下一页任务</button>
            </section>
          </section>
<section id="settings" className="space-y-5" hidden={true}><section className="rounded-3xl border border-zinc-200 bg-white p-6 space-y-4" aria-label="模型配置">
              <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold text-zinc-900">模型与接口</h2><button id="refresh-models" className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40">刷新配置</button></div>
              <p id="model-state" className="rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600" role="status"></p>
              <p className="text-sm leading-6 text-zinc-500">接口与识别模型由你提供。保存只登记配置，不代表模型能力或密钥已验证；执行须另行授权，可能上传媒体并计费。修改默认值不改变已有任务的固定配置。刷新、退出或提交后会清空密钥输入，不回显已保存密钥。</p>
              <p id="model-feedback" className="text-sm text-rose-600" role="status" aria-live="polite"></p>
              <div id="model-forms" className="grid gap-5 xl:grid-cols-3"></div>
            </section></section>
  </div>);
});
export default LegacyPanels;
