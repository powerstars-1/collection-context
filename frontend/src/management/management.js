// Existing /v1 owner-management controls, scoped to a static React island.
const classNames = {
  "page": "space-y-5",
  "card": "rounded-3xl border border-zinc-200 bg-white p-6",
  "management-section": "space-y-4",
  "section-head": "flex flex-wrap items-center justify-between gap-3",
  "eyebrow": "text-xs font-medium uppercase tracking-wide text-zinc-500",
  "muted": "text-sm leading-6 text-zinc-500",
  "notice": "rounded-xl bg-zinc-50 p-3 text-sm leading-6 text-zinc-600",
  "stack-form": "space-y-4",
  "field": "block space-y-2",
  "field-label": "block text-sm font-medium text-zinc-700",
  "field-toggle": "text-sm text-zinc-600",
  "checkbox-row": "flex items-start gap-2",
  "actions": "flex flex-wrap gap-2",
  "source-grid": "grid gap-5 xl:grid-cols-3",
  "tag-chip": "inline-flex rounded-md bg-zinc-100 px-2 py-1 text-xs text-zinc-600",
  "secondary": "rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-40",
  "error": "text-sm text-rose-600",
  "source-row": "space-y-3 rounded-xl border border-zinc-200 p-4",
  "source-controls": "flex flex-wrap items-center gap-3",
  "model-card": "space-y-4 rounded-xl border border-zinc-200 bg-white p-4",
  "task-row": "min-w-0 space-y-3 rounded-xl border border-zinc-200 p-4 [overflow-wrap:anywhere]",
  "tag": "inline-flex rounded-md bg-zinc-100 px-2 py-1 text-xs text-zinc-600",
  "check": "flex min-w-0 items-start gap-2 text-sm text-zinc-600 [overflow-wrap:anywhere]",
  "field-grid": "grid gap-4 sm:grid-cols-2",
  "hero-metrics": "grid gap-4 sm:grid-cols-3",
  "metric-tile": "rounded-xl border border-zinc-200 bg-white p-4",
  "metric-label": "block text-xs text-zinc-500"
};
const inputClass = "w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 outline-none focus:border-zinc-400 focus:ring-2 focus:ring-zinc-100 disabled:bg-zinc-50 disabled:text-zinc-400";
const buttonClass = "rounded-xl bg-zinc-900 px-4 py-2 text-sm text-white hover:bg-zinc-800 disabled:opacity-40";

export function mountManagement({ root = document.querySelector("[data-management-root]"), api: requestApi, canManage: permission = false, onOverview } = {}) {
  if (!root || typeof requestApi !== "function") throw new Error("管理页面未就绪。");
  const canManage = typeof permission === "function" ? Boolean(permission()) : Boolean(permission);
  const $ = (id) => root.querySelector(`[id="${id}"]`);
  const events = new AbortController();
  const timers = new Set();
  let destroyed = false, requestEpoch = 0, currentPage = null;
  // React StrictMode can replay this effect against the same static DOM island.
  root.querySelectorAll("button").forEach((node) => { node.disabled = !canManage; });
  const labels = { liked: "喜欢", saved: "收藏", collection: "收藏夹", creator: "博主", link: "链接" };
  const sourceKeys = new Map(), linkKeys = new Map(), workerSnapshots = new Map();
  let linkJobId = null, linkEpoch = 0, connectionVersion = null, connectionExpiry = null;
  let connectionPoll = null, connectionRunId = null, connectionRunEpoch = 0;
  let sourceEpoch = 0, sourceOffset = 0, modelEpoch = 0, managementEpoch = 0;
  let taskOffset = 0, preparedOffset = 0, historyKey = null, historyFingerprint = null;
  const preparedCounts = new Map();
  function schedule(fn, delay) {
    const timer = setTimeout(() => { timers.delete(timer); if (!destroyed) fn(); }, delay);
    timers.add(timer); return timer;
  }
  function listen(node, event, handler) {
    node.addEventListener(event, async (value) => {
      if (destroyed) return;
      if (value.type === "submit") value.preventDefault();
      if (!canManage) return;
      try { await handler(value); }
      catch (error) { if (!destroyed && error.name !== "AbortError") { const feedback = $(currentPage === "connect" ? "source-feedback" : currentPage === "settings" ? "model-feedback" : "management-feedback"); if (feedback) feedback.textContent = error.message; } }
    }, { signal: events.signal });
  }
  async function api(path, value) {
    if (destroyed || (path.startsWith("/v1/management/") && !canManage)) throw new DOMException("管理请求已取消。", "AbortError");
    const epoch = requestEpoch;
    const result = await requestApi(path, value);
    if (destroyed || epoch !== requestEpoch) throw new DOMException("页面已切换。", "AbortError");
    return result;
  }
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className.split(/\s+/).map((name) => classNames[name] || "").join(" ");
    if (tag === "button" && !className) node.className = buttonClass;
    if (["input", "select", "textarea"].includes(tag)) node.className = inputClass;
    if (tag === "fieldset") node.className = "space-y-4";
    return node;
  }
  function fieldComponent(label, input) {
    label.className = classNames.field;
    const text = label.textContent;
    label.replaceChildren(el("span", text, "field-label"), input);
    return label;
  }
  async function loadOverview() {
    const epoch = requestEpoch;
    try {
      const data = await api("/v1/collections/overview");
      if (epoch !== requestEpoch || destroyed) return;
      const metrics = el("div", undefined, "hero-metrics");
      for (const [value, label] of [[data.total_items, "资料条目"], [data.audio_missing, "视频尚无转写"], [Object.values(data.job_counts).reduce((sum, n) => sum + n, 0), "已登记任务"]]) {
        const card = el("article", undefined, "metric-tile");
        card.append(el("span", label, "metric-label"), el("strong", String(value))); metrics.append(card);
      }
      $("overview").replaceChildren(metrics);
      $("auto-state").textContent = `当前开关：同步${data.auto_sync ? "开启" : "关闭"}，处理${data.auto_process ? "开启" : "关闭"}。后台执行须另行授权；开启不会补跑全部历史。`;
      onOverview?.(data);
    } catch (error) { if (!destroyed && epoch === requestEpoch) $("overview").replaceChildren(el("p", error.message, "error")); }
  }
const taskLabels = { queued: "已排队", running: "运行中", succeeded: "完成", ready: "已完成", not_applicable: "不适用", not_started: "尚未开始", interrupted: "中断待恢复", partial: "部分完成", failed: "失败", cancelled: "已取消", blocked: "需人工处理" };
const sourceStates = { not_started: "尚未执行", running: "正在同步", ready: "本批已保存", partial: "部分保存", cancelled: "已取消", paused: "检查点已暂停", blocked: "需人工处理" };
async function loadModels() {
  const epoch = ++modelEpoch;
  root.querySelectorAll("input[type=password]").forEach((node) => { node.value = ""; });
  $("model-forms").replaceChildren();
  $("refresh-models").disabled = !canManage;
  if (!canManage) {
    $("model-state").textContent = "当前为只读访问，模型配置只向主人页面会话开放。";
    return;
  }
  try {
    const data = await api("/v1/management/models", {});
    if (epoch !== modelEpoch || !canManage) return;
    renderModels(data);
  } catch (error) {
    if (epoch === modelEpoch) $("model-feedback").textContent = error.message;
  }
}
function renderModels(data) {
  modelEpoch++;
  const storage = {
    macos_keychain: "密钥保存在 macOS 系统钥匙串，库外只存受控引用，不导出密钥。",
    private_service_files_not_encrypted: "密钥写入固定的库外私有文件，尚未加密。",
  }[data.credential_storage] || "凭据存储方式尚未验证；请查看后台配置。";
  $("model-state").textContent = data.configuration_enabled
    ? `模型配置已单独授权。${storage} 不进入资料、导出或AI读接口。旧任务所需凭据保留，轮换默认值不会撤销旧凭据。`
    : "本次后台未开启模型配置。需在启动时明确授权并指定库外私有凭据目录；页面不能选择文件路径或自动读取其他项目密钥。";
  $("model-forms").replaceChildren();
  const names = { audio: "音频转写", vision: "画面与图片", summary: "内容总结" };
  for (const [role, value] of Object.entries(data.roles)) {
    const section = el("article", undefined, "model-card");
    section.dataset.role = role;
    section.append(el("h3", names[role]), el("p", value.configured ? "已登记 · 尚未验证模型能力" : "尚未配置", "muted"));
    const form = el("form", undefined, "stack-form");
    const fields = el("fieldset");
    fields.disabled = !data.configuration_enabled;
    function field(name, label, type, content) {
      const id = `model-${role}-${name}`;
      const labelNode = el("label", label);
      labelNode.htmlFor = id;
      const input = el("input");
      input.id = id;
      input.type = type;
      input.value = content ?? "";
      fields.append(fieldComponent(labelNode, input));
      return input;
    }
    const config = value.profile || {};
    const url = field("url", "兼容接口基础地址（通常含 /v1）", "url", config.base_url);
    url.required = true;
    url.maxLength = 2048;
    url.placeholder = "https://你的接口地址/v1";
    const model = field("name", "模型名", "text", config.model);
    model.required = true;
    model.maxLength = 200;
    const protocol = el("select");
    protocol.id = `model-${role}-protocol`;
    const protocolLabel = el("label", "请求协议");
    protocolLabel.htmlFor = protocol.id;
    const protocols = role === "audio" ? { chat_audio: "聊天接口接收音频", transcription: "音频转写接口" } : { chat: "兼容聊天接口" };
    for (const [name, label] of Object.entries(protocols)) {
      const option = el("option", label); option.value = name; protocol.append(option);
    }
    protocol.value = config.protocol || (role === "audio" ? "chat_audio" : "chat");
    fields.append(fieldComponent(protocolLabel, protocol));
    const key = field("key", value.configured ? "API Key（留空保留此角色原密钥）" : "API Key", "password", "");
    key.autocomplete = "off";
    key.maxLength = 4096;
    key.required = !value.configured;
    for (const pair of [[url, model], [protocol, key]]) {
      const grid = el("div", undefined, "field-grid");
      const first = pair[0].parentElement;
      fields.insertBefore(grid, first);
      pair.forEach((input) => grid.append(input.parentElement));
    }
    const advanced = el("details");
    advanced.append(el("summary", "超时与可选参数"));
    const timeout = field("timeout", "超时（秒，1～600）", "number", config.timeout ?? 120);
    timeout.min = "1"; timeout.max = "600"; timeout.required = true;
    const timeoutLabel = fields.querySelector(`label[for='${timeout.id}']`);
    advanced.append(timeoutLabel);
    const parameters = el("textarea");
    parameters.id = `model-${role}-parameters`;
    parameters.maxLength = 8192;
    parameters.value = JSON.stringify(config.parameters || {}, null, 2);
    const parametersLabel = el("label", "参数 JSON（仅温度、输出上限、思考参数）");
    parametersLabel.htmlFor = parameters.id;
    advanced.append(fieldComponent(parametersLabel, parameters));
    fields.append(advanced);
    const confirmation = el("input");
    confirmation.type = "checkbox";
    confirmation.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
    confirmation.id = `model-${role}-confirmed`;
    confirmation.required = true;
    const check = el("label", undefined, "check");
    check.append(confirmation, el("span", "确认该接口可信，允许后台将凭据用于此接口；保存不发请求"));
    const button = el("button", "保存" + names[role]);
    button.type = "submit";
    fields.append(check, button);
    form.append(fields);
    listen(form, "submit", async (event) => {
      event.preventDefault();
      const epoch = modelEpoch;
      const api_key = key.value;
      key.value = "";
      button.disabled = true;
      try {
        let params;
        try { params = JSON.parse(parameters.value); }
        catch { throw new Error("可选参数须为有效JSON；密钥输入已清空，请核对后重新提交。"); }
        const updated = await api("/v1/management/model-save", {
          role, base_url: url.value, model: model.value, protocol: protocol.value,
          parameters: params, timeout: Number(timeout.value), api_key,
          expected_profile_id: value.profile_id, credential_confirmed: confirmation.checked,
        });
        if (epoch !== modelEpoch || !canManage) return;
        $("model-feedback").textContent = "配置已保存。未测试接口或模型能力，未发模型请求；已有任务仍使用原固定配置。";
        renderModels(updated);
      } catch (error) {
        if (epoch === modelEpoch) $("model-feedback").textContent = error.message;
      } finally { key.value = ""; confirmation.checked = false; button.disabled = destroyed || !canManage; }
    });
    section.append(form);
    $("model-forms").append(section);
  }
}
listen($("refresh-models"), "click", loadModels);
async function loadSources() {
  const epoch = ++sourceEpoch;
  $("creator-fields").disabled = !canManage;
  $("link-fields").disabled = !canManage;
  $("link-refresh").disabled = !canManage || !linkJobId;
  $("refresh-sources").disabled = !canManage;
  $("refresh-connection").disabled = !canManage;
  $("self-source-fields").disabled = $("folder-source-fields").disabled = true;
  connectionVersion = null;
  clearTimeout(connectionExpiry);
  clearTimeout(connectionPoll);
  const runEpoch = ++connectionRunEpoch;
  $("connection-actions").disabled = $("connection-cancel").disabled = true;
  connectionRunId = null;
  $("source-state").textContent = canManage
    ? "已授权主人管理。独立登录和后台执行须另行完成；列表仅显示已登记的固定范围。"
    : "当前为只读访问；管理来源需要主人口令。";
  if (!canManage) {
    $("source-list").replaceChildren();
    $("sources-more").hidden = true;
    $("connection-state").textContent = "连接证明仅向主人管理会话提供。";
    $("folder-source-id").replaceChildren();
    $("connection-run-state").textContent = "连接操作仅向主人管理会话提供。";
    return;
  }
  try {
    const data = await api("/v1/management/sources", { offset: sourceOffset });
    const connection = await api("/v1/management/connection", {});
    const run = await api("/v1/management/connection-run", {});
    if (epoch !== sourceEpoch || !canManage) return;
    renderConnection(connection);
    renderConnectionRun(run, runEpoch);
    showWorkerSnapshot("source-state", data.worker, "已授权主人管理。", " 独立登录仍须另行完成，范围登记不代表同步成功。");
    $("source-list").replaceChildren();
    data.scopes.forEach(renderSource);
    if (!data.scopes.length) $("source-list").append(el("p", "尚未登记范围。可保存博主主页；本人列表需先取得独立账号证明，收藏夹仅从实际观察列表选择。", "muted"));
    $("sources-more").hidden = data.next_offset === null;
    $("sources-more").dataset.offset = data.next_offset;
  } catch (error) {
    if (epoch === sourceEpoch) {
      $("connection-state").textContent = "连接证明无法重新确认，未沿用旧选择权限。";
      $("source-state").textContent = "无法重新确认来源与后台状态；请刷新，旧快照不代表当前在线。";
      $("source-feedback").textContent = error.message;
      $("connection-run-state").textContent = "无法确认连接任务；未自动重开浏览器，请刷新。";
    }
  }
}
function showLinkResult(task) {
  const messages = {queued: "已登记，等待已授权的来源后台；此任务不调用模型。", running: "来源任务正在执行；刷新只读取本地状态。", succeeded: "单条任务已完成，可查看保存的原文；转写仍单独处理。", partial: "原文已保留，媒体尚未完整准备；不会自动重试。", blocked: "任务受阻，请先处理所示原因；不会自动重试。", cancelled: "任务已取消，已保存内容可能保留。"};
  $("link-feedback").textContent = messages[task.state] || "请查看任务状态。";
  const result = task.link_result;
  const states = {not_started: "尚未开始", not_requested: "未请求", ready: "已保存", blocked: "受阻"};
  $("link-result").replaceChildren(el("p", `单条任务 ${task.job_id} · ${taskLabels[task.state] || task.state}`));
  if (result) {
    $("link-result").append(el("p", `原文：${states[result.metadata] || result.metadata}；媒体：${states[result.download] || result.download}；本任务模型请求：0。`, "muted"));
    if (result.download_error_code) $("link-result").append(el("p", `下载需处理：${result.download_error_code}`, "error"));
    if (result.material_ref) {
      const open = el("a", "查看已保存资料");
      open.href = "/?ref=" + encodeURIComponent(result.material_ref);
      $("link-result").append(open);
    }
  }
  if (task.error_code) $("link-result").append(el("p", `需处理：${task.error_code}。不会自动重试。`, "error"));
}
listen($("link-form"), "submit", async (event) => {
  event.preventDefault();
  if (!$("link-confirmed").checked) { $("link-feedback").textContent = "须确认仅访问这条作品。"; return; }
  const epoch = ++linkEpoch;
  const button = event.submitter;
  button.disabled = true;
  const args = {url: $("link-url").value.trim(), download: $("link-download").checked, source_confirmed: true};
  const fingerprint = JSON.stringify(args);
  if (!linkKeys.has(fingerprint)) linkKeys.set(fingerprint, crypto.randomUUID());
  try {
    const task = await api("/v1/management/link-submit", {...args, idempotency_key: linkKeys.get(fingerprint)});
    if (epoch !== linkEpoch || !canManage) return;
    linkJobId = task.job_id;
    $("link-refresh").disabled = false;
    showLinkResult(task);
  } catch (error) {
    if (epoch === linkEpoch) $("link-feedback").textContent = error.message;
  } finally {
    button.disabled = destroyed || !canManage;
    if (epoch === linkEpoch) $("link-confirmed").checked = false;
  }
});
listen($("link-refresh"), "click", async () => {
  const epoch = ++linkEpoch, ref = linkJobId;
  if (!ref || !canManage) return;
  try {
    const task = await api("/v1/management/link-status", {job_id: ref});
    if (epoch === linkEpoch && canManage) showLinkResult(task);
  } catch (error) { if (epoch === linkEpoch) $("link-feedback").textContent = error.message; }
});
function renderConnection(data) {
  connectionVersion = data.state === "verified" ? data.version : null;
  const states = {not_connected: "尚无独立账号证明", unverified: "未验证账号，不能选择本人来源", stale: "账号证明已过期，请重新验证", verified: "已观察本人账号"};
  $("connection-state").textContent = `${states[data.state] || "连接状态未知"}${data.display_name ? "：" + data.display_name : ""}。${data.observed_at ? "观察于 " + data.observed_at + "；不代表此刻仍登录。" : "不借用其他项目的登录目录。"}${data.error_code ? " " + data.error_code : ""}`;
  $("self-source-fields").disabled = !connectionVersion;
  $("folder-source-fields").disabled = !connectionVersion || !data.folders_observed || !data.folders.length;
  $("self-source-confirmed").checked = $("folder-source-confirmed").checked = false;
  $("folder-source-id").replaceChildren();
  for (const folder of data.folders) {
    const option = el("option", folder.name + " · " + folder.collection_id);
    option.value = folder.collection_id;
    $("folder-source-id").append(option);
  }
  $("folder-coverage").textContent = !data.folders_observed ? "尚未发现收藏夹，不能手填身份替代。" : `已观察 ${data.folders.length} 个；${data.complete ? "本次列表已到末页，不能保证期间无变动" : "覆盖不完整，未列出不代表不存在"}。同名收藏夹按稳定身份区分。`;
  if (connectionVersion) connectionExpiry = schedule(() => {
    connectionVersion = null;
    $("self-source-fields").disabled = $("folder-source-fields").disabled = true;
    $("connection-state").textContent = "账号证明已过期，请重新验证独立登录；页面不会自行访问平台。";
  }, Math.max(0, Math.min(900000, new Date(data.expires_at).getTime() - Date.now())));
}
listen($("refresh-connection"), "click", loadSources);
function renderConnectionRun(run, epoch) {
  if (epoch !== connectionRunEpoch || !canManage) return;
  connectionRunId = run.run_id;
  const labels = {idle: "尚未开始连接观察", disabled: "后台未启用页面连接，请在启动配置中明确授权独立浏览器", starting: "正在启动独立浏览器，窗口尚未确认就绪；请勿重复点击", running: "独立浏览器已就绪，正在验证；请在后台所在电脑完成平台要求", cancelling: "已请求取消，等待当前页面操作结束", completed: "本次观察结束，请核对下方账号及收藏夹证明", cancelled: "本次观察已取消", failed: "本次观察失败，未自动重试"};
  $("connection-run-state").textContent = `${labels[run.state] || "连接任务状态未知"}${run.headless ? "；无可见桌面，仅验证已有独立登录" : ""}${run.error_code ? " · " + run.error_code : ""}。未同步作品或调用模型。`;
  $("connection-actions").disabled = !run.enabled || run.active;
  if (run.active) $("self-source-fields").disabled = $("folder-source-fields").disabled = true;
  $("connection-login").disabled = run.headless === true;
  $("connection-cancel").disabled = !run.active || run.state === "cancelling";
  clearTimeout(connectionPoll);
  if (run.active) connectionPoll = schedule(async () => {
    try {
      const current = await api("/v1/management/connection-run", {});
      if (epoch !== connectionRunEpoch || !canManage) return;
      renderConnectionRun(current, epoch);
      if (!current.active) await loadSources();
    } catch (error) {
      if (epoch === connectionRunEpoch && canManage) {
        $("connection-run-state").textContent = "无法确认连接状态：" + error.message + "；未自动重开，请刷新。";
        $("connection-actions").disabled = $("connection-cancel").disabled = true;
      }
    }
  }, 2500);
}
for (const [id, mode] of [["connection-login", "login"], ["connection-check", "check"], ["connection-folders", "folders"]]) {
  listen($(id), "click", async () => {
    if (!$("connection-access-confirmed").checked) { $("connection-run-state").textContent = "请先确认本次平台访问。"; return; }
    clearTimeout(connectionPoll);
    const epoch = ++connectionRunEpoch;
    $("connection-actions").disabled = true;
    try {
      const run = await api("/v1/management/connection-start", {mode, source_confirmed: true});
      if (epoch === connectionRunEpoch && canManage) renderConnectionRun(run, epoch);
    } catch (error) {
      if (epoch === connectionRunEpoch && canManage) $("connection-run-state").textContent = error.message + "；未自动重试，请刷新。";
    } finally { $("connection-access-confirmed").checked = false; }
  });
}
listen($("connection-cancel"), "click", async () => {
  if (!connectionRunId) return;
  const epoch = connectionRunEpoch;
  $("connection-cancel").disabled = true;
  try {
    const run = await api("/v1/management/connection-cancel", {run_id: connectionRunId});
    renderConnectionRun(run, epoch);
  } catch (error) {
    if (epoch === connectionRunEpoch && canManage) $("connection-run-state").textContent = error.message + "；未自动重试，请刷新。";
  }
});
for (const [form, prefix, kind] of [["self-source-form", "self-source", null], ["folder-source-form", "folder-source", "collection"]]) {
  listen($(form), "submit", async (event) => {
    event.preventDefault();
    if (!connectionVersion) { $("source-feedback").textContent = "请先重新验证本人账号。"; return; }
    await sourceAction(event.submitter, "self-source-create", {
      kind: kind || $("self-source-kind").value,
      collection_id: kind ? $("folder-source-id").value : null,
      connection_version: connectionVersion, limit: Number($(prefix + "-limit").value),
      download: $(prefix + "-download").checked, source_confirmed: $(prefix + "-confirmed").checked,
    }, "本人范围已登记；未同步作品或调用模型，后续批次须单独确认。");
  });
}
function renderSource(scope) {
  const row = el("article", undefined, "source-row");
  row.dataset.scope = scope.scope_id;
  row.append(el("span", labels[scope.kind] || scope.kind, "tag"), el("h3", scope.kind === "creator" ? scope.creator_url : scope.kind === "collection" ? `收藏夹 ${scope.collection_id}` : `本人${labels[scope.kind]}`));
  row.append(el("p", `每批最多 ${scope.limit} 条 · ${scope.download ? "下载并准备媒体" : "仅保存文字与来源"} · 模型请求 0`, "muted"));
  row.append(el("p", `${sourceStates[scope.status] || scope.status}；已确认保存 ${scope.committed_count ?? "未知"} 条。覆盖${scope.complete ? "已由本批响应证明" : "未证明完整"}，操作时间不从同步时间推断。`, "muted"));
  if (scope.latest_job) {
    row.append(el("p", `最近批次：${taskLabels[scope.latest_job.state] || scope.latest_job.state}${scope.latest_job.error_code ? " · " + scope.latest_job.error_code : ""}`));
    if (!["queued", "running"].includes(scope.latest_job.state)) sourceKeys.delete(scope.config_id);
  }
  const timer = scope.timer;
  row.append(el("p", timer
    ? `定时${timer.enabled ? "开启" : "关闭"} · 每 ${timer.interval_minutes} 分钟 · 下次检查 ${timer.next_due_at}${timer.blocked_job_id ? " · 受阻，不自动重试" : ""}`
    : "定时关闭；尚未授权此范围的定时访问。", "muted"));
  if (!scope.timer_matches_current) row.append(el("p", "定时仍绑定旧固定配置，暂停不会替换它；重新启用前请核对新配置。", "error"));
  const controls = el("div", undefined, "source-controls");
  const manual = el("button", "同步一次", "secondary");
  manual.type = "button";
  manual.disabled = scope.pending_count > 0;
  listen(manual, "click", async () => {
    if (!confirm(`访问此来源，最多${scope.limit}条，${scope.download ? "下载并准备媒体" : "仅保存文字与来源"}？本操作只排队，不调用云模型。`)) return;
    if (!sourceKeys.has(scope.config_id)) sourceKeys.set(scope.config_id, crypto.randomUUID());
    await sourceAction(manual, "source-submit", { config_id: scope.config_id, idempotency_key: sourceKeys.get(scope.config_id), source_confirmed: true }, "批次已登记；等待独立登录与已授权后台执行，不代表同步完成。");
  });
  const intervalId = "interval-" + scope.scope_id;
  const intervalLabel = el("label", "间隔（分钟）");
  intervalLabel.htmlFor = intervalId;
  const interval = el("input");
  interval.id = intervalId;
  interval.type = "number";
  interval.min = "60";
  interval.max = "1440";
  interval.value = timer?.interval_minutes || 60;
  const toggle = el("button", timer?.enabled ? "暂停定时" : "启用定时", "secondary");
  toggle.type = "button";
  listen(toggle, "click", async () => {
    const enabled = !timer?.enabled;
    if (enabled && !interval.checkValidity()) { interval.reportValidity(); return; }
    if (enabled && !confirm(`允许后台按每${interval.value}分钟访问此固定来源，每批最多${scope.limit}条？休眠后不补跑全部错过批次；不会自动授权模型。`)) return;
    await sourceAction(toggle, "source-timer", { config_id: !enabled && timer ? timer.config_id : scope.config_id, enabled, interval_minutes: !enabled && timer ? timer.interval_minutes : Number(interval.value), reset_blocked: false, source_confirmed: enabled }, enabled ? "定时规则已保存；等待已授权后台，不代表后台在线。" : "定时已暂停，手动任务不受影响，已开始访问在检查点停止。");
  });
  controls.append(manual, intervalLabel, interval, toggle);
  if (timer?.blocked_job_id) {
    const retry = el("button", "确认重新尝试", "secondary");
    retry.type = "button";
    listen(retry, "click", async () => {
      if (!interval.checkValidity()) { interval.reportValidity(); return; }
      if (!confirm("已解决登录或来源异常？确认后开启定时并允许新批次访问，旧失败记录保留；不会请求模型。")) return;
      await sourceAction(retry, "source-timer", { config_id: scope.config_id, enabled: true, interval_minutes: Number(interval.value), reset_blocked: true, source_confirmed: true }, "已明确允许重新尝试；等待后台，不代表成功。");
    });
    controls.append(retry);
  }
  row.append(controls);
  $("source-list").append(row);
}
async function sourceAction(button, action, value, message) {
  const epoch = sourceEpoch;
  button.disabled = true;
  try {
    await api(`/v1/management/${action}`, value);
    if (epoch !== sourceEpoch || !canManage) return;
    $("source-feedback").textContent = message;
    await loadSources();
  } catch (error) {
    if (epoch === sourceEpoch) $("source-feedback").textContent = error.message;
  } finally { button.disabled = destroyed || !canManage; }
}
listen($("creator-form"), "submit", async (event) => {
  event.preventDefault();
  await sourceAction(event.submitter, "source-create", { creator_url: $("creator-url").value, limit: Number($("creator-limit").value), download: $("creator-download").checked }, "博主范围已保存；尚未排队或访问来源。已有不同配置不会被覆盖。");
});
listen($("refresh-sources"), "click", async () => { sourceOffset = 0; await loadSources(); });
listen($("sources-more"), "click", async () => { sourceOffset = Number($("sources-more").dataset.offset); await loadSources(); });
function workerText(worker) {
  if (!worker) return "后台状态未知，请刷新确认。";
  const state = worker.online === true ? "后台持有运行锁" : worker.online === false ? "未检测到运行中的后台" : "后台状态未知，未猜测接管";
  const capability = (value) => value === true ? "允许" : value === false ? "未允许" : "权限未知";
  return `${state}。同步${capability(worker.capabilities.source_sync)}，模型调用${capability(worker.capabilities.model_calls)}。截至 ${worker.observed_at}，这不代表任务有进度或已成功。`;
}
function showWorkerSnapshot(id, worker, prefix = "", suffix = "") {
  clearTimeout(workerSnapshots.get(id));
  $(id).textContent = prefix + workerText(worker) + suffix;
  workerSnapshots.set(id, schedule(() => {
    workerSnapshots.delete(id);
    if (canManage) $(id).textContent = prefix + "后台快照已过期，请点击刷新重新确认；页面不会自行执行或重启任务。" + suffix;
  }, 30000));
}
async function loadManagement() {
  const epoch = ++managementEpoch;
  $("auto-fields").disabled = !canManage;
  $("history-fields").disabled = !canManage;
  $("management-state").textContent = canManage
    ? "已授权主人管理。页面不回显已保存密钥，不直接请求模型；修改规则或提交任务可能授权后续费用。"
    : "当前为只读访问。管理功能需要单独的主人口令，AI读口令不会获得计费权限。";
  if (!canManage) {
    $("worker-state").textContent = "后台状态仅向主人管理会话提供。";
    $("task-list").replaceChildren();
    $("prepared-items").replaceChildren();
    $("tasks-more").hidden = $("prepared-more").hidden = true;
    return;
  }
  try {
    $("worker-state").textContent = "正在重新确认后台状态…";
    const tasks = await api("/v1/management/overview", { offset: taskOffset });
    const data = preparedOffset === taskOffset ? tasks : await api("/v1/management/overview", { offset: preparedOffset });
    if (epoch !== managementEpoch || !canManage) return;
    showWorkerSnapshot("worker-state", tasks.worker);
    $("task-list").replaceChildren();
    tasks.jobs.forEach(renderTask);
    if (!tasks.jobs.length) $("task-list").append(el("p", "当前页没有任务。", "muted"));
    $("tasks-more").hidden = tasks.next_offset === null;
    $("tasks-more").dataset.offset = tasks.next_offset;
    $("prepared-more").hidden = data.next_prepared_offset === null;
    $("prepared-more").dataset.offset = data.next_prepared_offset;
    $("prepared-items").replaceChildren();
    preparedCounts.clear();
    data.prepared.forEach((item) => {
      preparedCounts.set(item.input_id, item.planned_calls_before_reuse ?? null);
      const label = el("label", undefined, "check");
      const input = el("input");
      input.type = "checkbox";
      input.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
      input.value = item.input_id;
      listen(input, "change", updateHistoryBudget);
      label.append(input, el("span", item.title || "无标题资料"));
      label.append(el("span", item.planned_calls_before_reuse == null
        ? "计划次数未确认，请先检查媒体准备状态。"
        : `音频 ${item.audio_segments} 段 + 画面 ${item.visual_frames} 页 + 总结 1 次；无复用时共 ${item.planned_calls_before_reuse} 次。`, "muted"));
      $("prepared-items").append(label);
    });
    if (!data.prepared.length) $("prepared-items").append(el("p", "当前页没有已准备媒体的资料。", "muted"));
    const auto = tasks.automatic;
    $("auto-enabled").value = auto.enabled ? "yes" : "no";
    $("auto-calls").value = auto.max_calls_per_task ?? "";
    $("auto-tasks").value = auto.max_new_tasks || 5;
    $("auto-fee").checked = false;
    $("history-fee").checked = false;
    $("auto-state").textContent = `处理${auto.enabled ? "开启" : "关闭"}；本次剩余新任务 ${auto.remaining_new_tasks}，暂停积压 ${auto.paused_count}，准备异常 ${auto.preparation_blocked_count}。开启记录新边界，积压需逐项确认恢复。`;
    updateHistoryBudget();
  } catch (error) {
    if (epoch === managementEpoch) {
      $("worker-state").textContent = "后台状态无法重新确认；旧快照不作为当前在线证明。";
      $("management-feedback").textContent = error.message;
    }
  }
}
function renderTask(task) {
  const row = el("article", undefined, "task-row");
  row.append(el("span", taskLabels[task.state] || task.state, "tag"), el("p", `${task.kind === "sync" ? "来源同步" : task.kind === "process" ? "媒体提取" : "链接入库"} · ${task.job_id}`));
  row.append(el("p", `已登记请求 ${task.recorded_calls} / 上限 ${task.max_calls}；结果不明 ${task.unknown_calls}，已完成但无用量 ${task.usage_missing_calls}。金额未提供。`, "muted"));
  if (task.parent_job_id) row.append(el("p", `第 ${task.attempt} 次尝试 · 原任务 ${task.parent_job_id}；旧请求及费用记录保留。`, "muted"));
  if (Object.keys(task.stages).length) row.append(el("p", Object.entries(task.stages).map(([k, v]) => `${k}：${taskLabels[v] || v}`).join(" / ")));
  if (task.error_code) row.append(el("p", `需处理：${task.error_code}`, "error"));
  if (task.link_result) {
    const result = task.link_result;
    row.append(el("p", `原文：${result.metadata}；媒体：${result.download}；此任务未请求提取。`, "muted"));
    if (result.download_error_code) row.append(el("p", `下载需处理：${result.download_error_code}`, "error"));
    if (result.material_ref) { const open = el("a", "查看已保存资料"); open.href = "/?ref=" + encodeURIComponent(result.material_ref); row.append(open); }
  }
  if (["queued", "running", "blocked"].includes(task.state)) {
    const button = el("button", "取消后续处理", "secondary");
    button.type = "button";
    listen(button, "click", async () => {
      if (!confirm("取消此任务的后续处理？已经发出的请求可能继续计费。")) return;
      await managementAction(button, "cancel", { job_id: task.job_id }, "已取消后续处理。");
    });
    row.append(button);
  }
  if (task.mode === "automatic" && task.state === "queued") {
    const button = el("button", "预览恢复", "secondary");
    button.type = "button";
    listen(button, "click", async () => {
      const epoch = requestEpoch;
      button.disabled = true;
      try {
        const preview = await api("/v1/management/resume-preview", { job_ids: [task.job_id] });
        if (confirm(`恢复这1条旧自动任务？原固定模型、最多${preview.max_calls}次请求，金额未知。自动开关需开启。`)) {
          await api("/v1/management/resume", { job_ids: preview.job_ids, preview_token: preview.preview_token, fee_confirmed: true });
          if (destroyed || epoch !== requestEpoch) return;
          $("management-feedback").textContent = "已授权选定积压，等待后台执行器。";
          await loadManagement();
        }
      } catch (error) { if (!destroyed && epoch === requestEpoch) $("management-feedback").textContent = error.message; }
      finally { button.disabled = destroyed || !canManage; }
    });
    row.append(button);
  }
  if (task.can_retry) {
    const button = el("button", "核对并选择重试阶段", "secondary");
    button.type = "button";
    const panel = el("div", undefined, "stack-form");
    listen(button, "click", async () => {
      button.disabled = true;
      panel.replaceChildren(el("p", "正在读取固定计划；不会请求模型…", "muted"));
      try {
        const initial = await api("/v1/management/retry-preview", { job_id: task.job_id });
        panel.replaceChildren(el("p", "选择要补做的阶段。成功阶段会复用；所选阶段影响的总结和保存步骤也会重新检查。未选缺口保持原状。", "notice"));
        panel.append(el("p", `沿用原任务模型：${Object.entries(initial.fixed_models).map(([role, name]) => `${role}: ${name}`).join(" / ")}`, "muted"));
        const selected = [];
        const details = el("div", undefined, "stack-form");
        for (const stage of initial.stages) {
          const label = el("label", undefined, "check");
          const input = el("input"); input.type = "checkbox";
          input.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
          input.disabled = !stage.selectable;
          selected.push({ input, name: stage.name });
          listen(input, "change", () => details.replaceChildren());
          label.append(input, el("span", `${stage.name} · ${taskLabels[stage.state] || stage.state}${stage.selectable ? "" : "（已完成，将复用）"}`));
          panel.append(label);
        }
        const previewButton = el("button", "预览所选阶段", "secondary"); previewButton.type = "button";
        listen(previewButton, "click", async () => {
          const stages = selected.filter(value => value.input.checked && !value.input.disabled).map(value => value.name);
          if (!stages.length) { details.replaceChildren(el("p", "请先选择至少一个未完成阶段。", "error")); return; }
          previewButton.disabled = true;
          details.replaceChildren(el("p", "正在核对阶段与未知请求…", "muted"));
          try {
            const preview = await api("/v1/management/retry-preview", { job_id: task.job_id, stages });
            if (JSON.stringify(stages) !== JSON.stringify(selected.filter(value => value.input.checked && !value.input.disabled).map(value => value.name))) {
              details.replaceChildren(el("p", "选择已变化，请重新预览。", "muted")); return;
            }
            details.replaceChildren(el("p", `本次范围：${preview.affected_stages.join("、")}。无复用时最多 ${preview.max_calls} 次云请求；金额未知。`, "notice"));
            const callChecks = [];
            if (preview.unknown_calls.length) details.append(el("p", "以下请求结果未明。请到上游控制台核对：未查到记录、超时或等待很久都不代表未收费。此操作保留原来的未知账本，仅允许这一次新尝试重试，可能重复计费。", "error"));
            for (const call of preview.unknown_calls) {
              const check = el("input"); check.type = "checkbox";
              check.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
              const label = el("label", undefined, "check");
              label.append(check, el("span", `我已核对 ${call.stage} · ${call.call_id}；登记时间 ${call.created_at || "未提供"}；上游请求号：${call.upstream_request_id || "未提供，请按时间和固定模型核对"}。`));
              callChecks.push({ input: check, id: call.call_id }); details.append(label);
            }
            const duplicate = el("input"); duplicate.type = "checkbox";
            duplicate.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
            if (preview.unknown_calls.length) {
              const label = el("label", undefined, "check");
              label.append(duplicate, el("span", "已核对仍需重试，我接受本次可能重复计费。")); details.append(label);
            }
            const budget = el("input"); budget.type = "number"; budget.min = String(preview.max_calls); budget.max = "1000"; budget.required = true;
            budget.value = ""; budget.placeholder = `请明确填写上限（至少 ${preview.max_calls}）`;
            const budgetLabel = el("label", "本次最多云请求次数", "field"); fieldComponent(budgetLabel, budget); details.append(budgetLabel);
            const fee = el("input"); fee.type = "checkbox"; fee.className = "mt-1 h-4 w-4 shrink-0 accent-zinc-900";
            const feeLabel = el("label", undefined, "check"); feeLabel.append(fee, el("span", "确认本次范围、媒体上传和请求上限；不会自动补额或无限重试。")); details.append(feeLabel);
            const submit = el("button", "创建新尝试"); submit.type = "button";
            const feedback = el("p", "", "error");
            const idempotencyKey = crypto.randomUUID();
            listen(submit, "click", async () => {
              const maxCalls = Number(budget.value);
              if (!budget.value.trim() || !Number.isInteger(maxCalls) || maxCalls < preview.max_calls || maxCalls > 1000 || !fee.checked || callChecks.some(value => !value.input.checked) || (callChecks.length && !duplicate.checked)) {
                feedback.textContent = "请填写足够的明确上限，并完成范围、费用及所有未知请求的核对确认。"; return;
              }
              submit.disabled = true;
              try {
                const next = await api("/v1/management/retry", { job_id: task.job_id, stages, preview_token: preview.preview_token, idempotency_key: idempotencyKey, max_calls: maxCalls, fee_confirmed: true, reviewed_call_ids: callChecks.map(value => value.id), duplicate_charge_confirmed: duplicate.checked });
                $("management-feedback").textContent = `新尝试 ${next.job_id} 已排队；原任务与费用保留，等待已授权后台处理。`;
                await loadManagement();
              } catch (error) { feedback.textContent = error.message; }
              finally { submit.disabled = destroyed || !canManage; }
            });
            details.append(submit, feedback);
          } catch (error) { details.replaceChildren(el("p", error.message, "error")); }
          finally { previewButton.disabled = destroyed || !canManage; }
        });
        panel.append(previewButton, details);
      } catch (error) { panel.replaceChildren(el("p", error.message, "error")); }
      finally { button.disabled = destroyed || !canManage; }
    });
    row.append(button, panel);
  }
  $("task-list").append(row);
}
function historySelection() {
  return [...$("prepared-items").querySelectorAll("input:checked")].map((input) => input.value);
}
function updateHistoryBudget() {
  const selected = historySelection(), counts = selected.map(id => preparedCounts.get(id));
  const planned = counts.every(value => Number.isInteger(value)) ? counts.reduce((sum, value) => sum + value, 0) : null;
  $("history-budget").textContent = `选中 ${selected.length} 条；无复用时计划 ${planned ?? "未知"} 次请求；本批授权上限 ${$("history-calls").value.trim() ? selected.length * Number($("history-calls").value) : "尚未填写"}。成功阶段可能复用；不足时停在缺口，不自动补额，金额未知。`;
}
async function managementAction(button, action, value, message) {
  const epoch = requestEpoch;
  button.disabled = true;
  try {
    await api(`/v1/management/${action}`, value);
    if (destroyed || epoch !== requestEpoch) return;
    $("management-feedback").textContent = message;
    await loadOverview();
    if (destroyed || epoch !== requestEpoch) return;
    await loadManagement();
  } catch (error) { if (!destroyed && epoch === requestEpoch) $("management-feedback").textContent = error.message; }
  finally { button.disabled = destroyed || !canManage; }
}
listen($("auto-form"), "submit", async (event) => {
  event.preventDefault();
  if ($("auto-enabled").value === "yes" && !$("auto-calls").value.trim()) throw new Error("请明确填写每条请求上限：有音轨的视频至少需要音频段数 + 画面数 + 总结 1 次；后续新资料长度不同，额度不足会停止，不会自动补额。");
  await managementAction(event.submitter, "automatic", {
    enabled: $("auto-enabled").value === "yes", max_calls: Number($("auto-calls").value),
    max_new_tasks: Number($("auto-tasks").value), fee_confirmed: $("auto-fee").checked,
  }, "自动规则已保存。同步计划和已发出的请求不受此开关撤回。");
});
listen($("history-calls"), "input", updateHistoryBudget);
listen($("history-form"), "submit", async (event) => {
  event.preventDefault();
  const input_ids = historySelection();
  if (!input_ids.length) { $("management-feedback").textContent = "请先选择资料。"; return; }
  if (!$("history-calls").value.trim()) throw new Error("请根据所选资料的阶段数量，明确填写每条请求上限。");
  const max_calls = Number($("history-calls").value);
  if (!Number.isInteger(max_calls) || max_calls < 0 || max_calls > 1000) throw new Error("请求上限须为 0 至 1000 的整数。");
  if (input_ids.some(id => !Number.isInteger(preparedCounts.get(id)))) throw new Error("所选资料尚无法确认计划次数，请先检查媒体准备状态。");
  if (input_ids.some(id => preparedCounts.get(id) > max_calls) && !confirm("此上限低于所选资料无复用时需要的阶段数。若不能复用，任务会中途停止；不会自动追加费用。仍以此较低上限提交吗？")) return;
  const fingerprint = JSON.stringify({ input_ids, max_calls });
  if (fingerprint !== historyFingerprint) { historyKey = crypto.randomUUID(); historyFingerprint = fingerprint; }
  await managementAction(event.submitter, "history", {
    input_ids, max_calls, idempotency_key: historyKey, fee_confirmed: $("history-fee").checked,
  }, "历史批次已登记，等待单独授权的后台执行器；重复提交同一批次不会重复建任务。");
});
listen($("refresh-tasks"), "click", async () => {
  const epoch = requestEpoch;
  taskOffset = preparedOffset = 0;
  await loadOverview();
  if (!destroyed && epoch === requestEpoch) await loadManagement();
});
listen($("tasks-more"), "click", async () => { taskOffset = Number($("tasks-more").dataset.offset); await loadManagement(); });
listen($("prepared-more"), "click", async () => { preparedOffset = Number($("prepared-more").dataset.offset); await loadManagement(); });

  async function show(page) {
    if (destroyed) return;
    currentPage = page; requestEpoch++;
    sourceEpoch++; modelEpoch++; managementEpoch++; linkEpoch++; connectionRunEpoch++;
    clearTimeout(connectionPoll); connectionPoll = null;
    root.querySelectorAll("input[type=password]").forEach((node) => { node.value = ""; });
    for (const name of ["connect", "activity", "settings"]) $(name).hidden = page !== name;
    if (page === "connect") await loadSources();
    else if (page === "activity") { await loadOverview(); if (!destroyed && currentPage === page) await loadManagement(); }
    else if (page === "settings") await loadModels();
  }
  function destroy() {
    destroyed = true; requestEpoch++; sourceEpoch++; modelEpoch++; managementEpoch++; linkEpoch++; connectionRunEpoch++;
    events.abort();
    timers.forEach(clearTimeout); timers.clear(); workerSnapshots.clear();
    sourceKeys.clear(); linkKeys.clear(); historyKey = historyFingerprint = connectionVersion = connectionRunId = linkJobId = null;
    root.querySelectorAll("input[type=password], textarea").forEach((node) => { node.value = ""; });
    root.querySelectorAll("fieldset, button").forEach((node) => { node.disabled = true; });
    for (const id of ["model-forms", "source-list", "task-list", "prepared-items", "link-result", "overview", "folder-source-id"]) $(id).replaceChildren();
  }
  return { show, destroy };
}
