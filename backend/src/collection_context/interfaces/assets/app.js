"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  liked: "喜欢",
  saved: "收藏",
  collection: "收藏夹",
  creator: "博主",
  link: "链接",
};
const artifactLabels = {
  original: "原文",
  audio: "转写",
  screen: "画面",
  summary: "总结",
  readable: "全文",
  image: "图片",
  user_note: "备注",
};
const stateLabels = {
  ready: "已保存 · 精度需核对",
  missing: "尚未提取",
  not_applicable: "不适用 · 没有音轨",
  stale: "输入已变化",
  unavailable: "文件需核对",
};
const sourceKeys = new Map();
const linkKeys = new Map();
let linkJobId = null, linkEpoch = 0;
const workerSnapshots = new Map();
let connectionVersion = null, connectionExpiry = null;
let connectionPoll = null, connectionRunId = null, connectionRunEpoch = 0;
let sourceEpoch = 0, sourceOffset = 0;
let modelEpoch = 0;
let csrf = null,
  canManage = false,
  managementEpoch = 0,
  taskOffset = 0,
  preparedOffset = 0,
  historyKey = null,
  historyFingerprint = null,
  listing = null,
  selected = null,
  currentArtifact = "original",
  readPage = null,
  searchEpoch = 0,
  detailEpoch = 0,
  readEpoch = 0;
const page =
  location.pathname === "/connect"
    ? "connect"
    : location.pathname === "/activity"
      ? "activity"
      : "materials";
const pageCopy = {
  materials: ["你的个人收藏资料库", "把收藏，留给下一次灵感。", "从喜欢和收藏中找到过去的教程。读原文、查转写、核对画面，让你的 AI 有据可答。"],
  connect: ["添加与连接", "从你感兴趣的地方开始。", "添加一条作品，或连接喜欢、收藏夹与指定博主。同步范围由你选择，保存资料与模型提取分开。"],
  activity: ["处理与设置", "让每一次处理，都有迹可循。", "查看任务、配置自己的模型，决定哪些内容自动处理。请求次数和缺失项如实显示，未知费用不会假装为零。"],
};
["hero-eyebrow", "hero-title", "hero-description"].forEach((id, index) => {
  $(id).textContent = pageCopy[page][index];
});
document
  .querySelectorAll("nav a")
  .forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute("aria-current", "page");
  });
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  if (tag === "button" && !node.classList.contains("tiny-button")) {
    node.classList.add("button");
    if (!["secondary", "tertiary", "ghost"].some((name) => node.classList.contains(name))) node.classList.add("primary");
  }
  return node;
}
function fieldComponent(label, input) {
  label.classList.add("field");
  const text = label.textContent;
  label.replaceChildren(el("span", text, "field-label"), input);
  return label;
}
document.querySelectorAll("button:not(.tiny-button)").forEach((button) => {
  button.classList.add("button");
  if (!["primary", "secondary", "tertiary", "ghost"].some((name) => button.classList.contains(name))) button.classList.add("primary");
});
async function api(path, value) {
  const response = await fetch(path, {
    method: value === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers:
      value === undefined
        ? {}
        : {
            "Content-Type": "application/json",
            ...(csrf ? { "X-CSRF-Token": csrf } : {}),
          },
    body: value === undefined ? undefined : JSON.stringify(value),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("服务暂时不可读取，请确认后台仍在运行。");
  }
  if (!response.ok || !result.ok) {
    if (response.status === 401) showLogin();
    throw new Error(result.error?.message || "请求未完成。");
  }
  return result.data;
}
function resetDetail() {
  const empty = el("div", undefined, "empty-state");
  empty.append(
    el("p", "还没有详情结果。"),
    el("span", "在资料卡片里点击“查看详情”，可分别阅读原文、转写和画面。"),
  );
  $("detail").classList.add("empty-shell");
  $("detail").replaceChildren(empty);
  $("detail-result-summary").textContent = "尚未选择资料";
}
function showLogin() {
  linkEpoch++;
  linkJobId = null;
  linkKeys.clear();
  $("link-fields").disabled = $("link-refresh").disabled = true;
  $("link-url").value = "";
  $("link-confirmed").checked = false;
  $("link-result").replaceChildren();
  $("link-feedback").textContent = "";
  csrf = null;
  canManage = false;
  clearTimeout(connectionExpiry);
  clearTimeout(connectionPoll);
  connectionRunEpoch++;
  connectionRunId = null;
  $("connection-actions").disabled = $("connection-cancel").disabled = true;
  $("connection-access-confirmed").checked = false;
  $("connection-run-state").textContent = "未读取本次连接观察。";
  connectionVersion = null;
  $("folder-source-id").replaceChildren();
  $("self-source-fields").disabled = $("folder-source-fields").disabled = true;
  $("connection-state").textContent = "未读取连接证明。";
  for (const timer of workerSnapshots.values()) clearTimeout(timer);
  workerSnapshots.clear();
  managementEpoch++;
  sourceEpoch++;
  modelEpoch++;
  $("model-forms").replaceChildren();
  sourceKeys.clear();
  $("source-list").replaceChildren();
  $("creator-fields").disabled = true;
  $("creator-url").value = "";
  selected = null;
  listing = null;
  detailEpoch++;
  readEpoch++;
  $("workspace").hidden = true;
  $("login-panel").hidden = false;
  $("logout").hidden = true;
  $("session-state").textContent = "未登录";
  $("session-state").className = "status-pill status-pending";
  ["hero-count", "hero-pending", "hero-jobs"].forEach((id) => { $(id).textContent = "—"; });
  $("items").replaceChildren();
  $("detail").replaceChildren();
  $("json-output").textContent = '{"message":"请登录本机资料库。"}';
}
async function enter(session) {
  csrf = session.csrf_token;
  canManage = session.permissions.includes("ui:manage");
  $("login-panel").hidden = true;
  $("workspace").hidden = false;
  $("logout").hidden = false;
  $("session-state").textContent = "已连接本地资料库";
  $("session-state").className = "status-pill status-success";
  $(page).hidden = false;
  await loadHero();
  if (csrf !== session.csrf_token) return;
  if (page === "materials") {
    resetDetail();
    await loadItems(false);
    const ref = new URLSearchParams(location.search).get("ref");
    if (ref && /^[A-Za-z0-9_-]{1,160}$/.test(ref)) {
      try {
        const item = await api(`/v1/collections/${encodeURIComponent(ref)}/status`);
        if (csrf === session.csrf_token) await showDetail(item);
      } catch (error) { $("search-message").textContent = error.message; }
    }
  } else if (page === "activity") {
    await loadOverview();
    await loadManagement();
    await loadModels();
  } else if (page === "connect") {
    await loadSources();
  }
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("login-error").textContent = "";
  const token = $("access-token").value;
  $("access-token").value = "";
  try {
    await enter(await api("/v1/session", { token }));
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  try {
    await api("/v1/session/logout", {});
  } finally {
    showLogin();
  }
});
function filters() {
  const kind = $("source-filter").value;
  return kind ? { source_kinds: [kind] } : {};
}
async function loadItems(append) {
  const epoch = ++searchEpoch;
  $("search-message").textContent = "正在读取已有资料…";
  const query = $("query").value.trim();
  const oldListing = listing;
  try {
    const data = query
      ? await api("/v1/collections/search", {
          query,
          limit: 20,
          filters: filters(),
        })
      : await api("/v1/collections/list", {
          limit: 20,
          filters: filters(),
          ...(append && oldListing
            ? { offset: oldListing.next_offset, version: oldListing.version }
            : {}),
        });
    if (epoch !== searchEpoch) return;
    listing = query ? null : data;
    if (!append) $("items").replaceChildren();
    $("material-count").textContent = String(
      data.total_items ?? data.total_matches,
    );
    data.items.forEach(renderItem);
    $("response-state").textContent = query ? "搜索结果已更新" : "资料列表已更新";
    $("response-banner").classList.remove("is-error");
    $("response-banner").classList.add("is-success");
    $("json-output").textContent = JSON.stringify(data, null, 2);
    $("more-items").hidden = !listing || listing.next_offset === null;
    $("search-message").textContent = data.items.length
      ? query
        ? "关键词命中；继续读原文或画面核对细节。"
        : "按首次发现时间排序；这不是实际点赞时间。"
      : "当前范围没有资料。尝试换个词或来源筛选。";
    if (!data.items.length) {
      const empty = el("div", undefined, "empty-state");
      empty.append(el("p", "没有匹配资料"), el("span", "更换关键词或来源，或者查看全部资料。"));
      $("items").append(empty);
    }
  } catch (error) {
    if (epoch === searchEpoch) {
      $("search-message").textContent = error.message;
      $("response-banner").classList.remove("is-success");
      $("response-banner").classList.add("is-error");
      $("response-state").textContent = "读取失败";
    }
  }
}
function renderStat(label, value) {
  const stat = el("div", undefined, "stat-item");
  stat.append(el("span", label, "stat-label"), el("span", String(value ?? "未知"), "stat-value"));
  return stat;
}
function sourceLink(item, text) {
  if (!/^https:\/\/www\.douyin\.com\/(video|note)\/\d+$/.test(item.source_url)) return null;
  const link = el("a", text, "tiny-button");
  link.href = item.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
function copyLinkButton(item) {
  const button = el("button", "复制链接", "tiny-button");
  button.type = "button";
  button.disabled = !sourceLink(item, "");
  button.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(item.source_url); button.textContent = "已复制"; }
    catch { button.textContent = "复制失败，请打开来源"; }
  });
  return button;
}
function renderItem(item) {
  const card = el("article", undefined, "note-card item");
  card.dataset.ref = item.material_ref;
  const head = el("div", undefined, "note-head");
  const title = el("div");
  const metadata = el("ul", undefined, "note-meta");
  metadata.append(el("li", "作者：" + (item.author || "未知")), el("li", "引用：" + item.material_ref));
  title.append(el("h5", item.title || "未提供标题", "note-title"), metadata);
  const mediaLabel = item.media_type === "image" ? "图文" : item.media_type === "video" ? "视频" : "资料";
  head.append(title, el("span", mediaLabel, "type-pill"));
  const ready = Object.entries(item.artifact_states || {}).filter(([kind, state]) => kind !== "original" && state === "ready").map(([kind]) => artifactLabels[kind] || kind);
  const processingLabel = item.artifact_states
    ? (ready.length ? `已保存：${ready.join(" / ")}` : "原文已保存 · 提取待处理")
    : "检索已命中 · 处理状态见详情";
  const stats = el("div", undefined, "stat-grid");
  stats.append(renderStat("来源关系", [...new Set(item.relations.map((r) => labels[r.kind] || r.kind))].join(" / ")), renderStat("资料类型", mediaLabel), renderStat("处理状态", processingLabel));
  const actions = el("div", undefined, "note-actions");
  const view = el("button", "查看详情", "tiny-button js-parse-detail");
  view.type = "button";
  view.addEventListener("click", () => showDetail(item));
  const source = sourceLink(item, "打开来源");
  if (source) actions.append(source);
  actions.append(view, copyLinkButton(item));
  card.append(head, stats, actions);
  if (item.snippet) card.append(el("p", item.snippet, "detail-desc"));
  $("items").append(card);
}
async function showDetail(item) {
  const epoch = ++detailEpoch;
  selected = item;
  currentArtifact = "original";
  readPage = null;
  document
    .querySelectorAll(".item")
    .forEach((b) =>
      b.classList.toggle("selected", b.dataset.ref === item.material_ref),
    );
  const detail = $("detail");
  detail.classList.remove("empty-shell");
  detail.replaceChildren(el("h4", item.title || "未提供标题", "detail-title"));
  const metadata = el("ul", undefined, "detail-meta");
  metadata.append(el("li", "作者：" + (item.author || "未知")), el("li", "内容不代表你的观点"));
  detail.append(metadata);
  const actions = el("div", undefined, "note-actions");
  const source = sourceLink(item, "打开来源");
  if (source) actions.append(source);
  actions.append(copyLinkButton(item));
  const relations = el("div", undefined, "chip-row");
  [...new Set(item.relations.map((r) => r.kind))].forEach((kind) => relations.append(el("span", labels[kind] || kind, "tag-chip")));
  detail.append(actions, relations);
  const tabs = el("div", undefined, "note-actions tabs");
  Object.entries(artifactLabels).forEach(([kind, label]) => {
    const button = el("button", label, "tiny-button");
    button.dataset.artifact = kind;
    button.type = "button";
    button.addEventListener("click", () => loadRead(kind, false));
    tabs.append(button);
  });
  detail.append(tabs, el("p", "正在读取证据…", "detail-desc artifact-gaps"));
  $("detail-result-summary").textContent = "原文与提取分别读取";
  const state = el("p");
  state.id = "evidence-state";
  const content = el("pre", "", "detail-desc evidence");
  content.id = "evidence-text";
  const more = el("button", "继续读取", "secondary");
  more.id = "more-read";
  more.hidden = true;
  more.addEventListener("click", () => loadRead(currentArtifact, true));
  detail.append(state, content, more);
  try {
    const status = await api(
      `/v1/collections/${encodeURIComponent(item.material_ref)}/status`,
    );
    if (selected !== item || epoch !== detailEpoch) return;
    detail.querySelector(".artifact-gaps").textContent = Object.entries(
      status.artifacts,
    )
      .filter(([, a]) => a.state !== "ready")
      .map(
        ([kind, a]) =>
          `${artifactLabels[kind]}：${stateLabels[a.state] || a.state}`,
      )
      .join(" / ");
  } catch (error) {
    if (selected === item && epoch === detailEpoch)
      $("evidence-state").textContent = error.message;
  }
  if (selected === item && epoch === detailEpoch)
    await loadRead(currentArtifact, false);
}
async function loadRead(kind, append) {
  const epoch = ++readEpoch;
  if (!selected) return;
  const item = selected;
  const request = {
    material_ref: item.material_ref,
    artifact: kind,
    ...(append && readPage
      ? { offset: readPage.next_offset, version: readPage.version }
      : {}),
  };
  currentArtifact = kind;
  if (!append) {
    readPage = null;
    $("evidence-text").textContent = "";
  }
  $("more-read").hidden = true;
  $("evidence-state").textContent = "读取中…";
  document
    .querySelectorAll(".tabs button")
    .forEach((b) =>
      b.classList.toggle("selected", b.dataset.artifact === kind),
    );
  try {
    const data = await api("/v1/collections/read", request);
    if (selected !== item || currentArtifact !== kind || epoch !== readEpoch)
      return;
    readPage = data;
    $("json-output").textContent = JSON.stringify(data, null, 2);
    $("evidence-text").textContent += data.text;
    $("evidence-state").textContent =
      `${stateLabels[data.state] || data.state} · ${data.total_chars} 字符${data.warnings.length ? " · " + data.warnings.join(" ") : ""}`;
    $("more-read").hidden = data.next_offset === null;
  } catch (error) {
    if (selected === item && currentArtifact === kind && epoch === readEpoch)
      $("evidence-state").textContent = error.message;
  }
}
async function loadHero() {
  const session = csrf;
  try {
    const data = await api("/v1/collections/overview");
    if (!session || csrf !== session) return;
    $("hero-count").textContent = String(data.total_items);
    $("hero-pending").textContent = String(data.audio_missing);
    $("hero-jobs").textContent = String(Object.values(data.job_counts).reduce((sum, n) => sum + n, 0));
  } catch (error) {
    if (session && csrf === session) ["hero-count", "hero-pending", "hero-jobs"].forEach((id) => { $(id).textContent = "未知"; });
  }
}
async function loadOverview() {
  try {
    const data = await api("/v1/collections/overview");
    const metrics = el("div", undefined, "hero-metrics");
    for (const [value, label] of [
      [data.total_items, "资料条目"],
      [data.audio_missing, "视频尚无转写"],
      [
        Object.values(data.job_counts).reduce((sum, n) => sum + n, 0),
        "已登记任务",
      ],
    ]) {
      const card = el("article", undefined, "metric-tile");
      card.append(el("span", label, "metric-label"), el("strong", String(value)));
      metrics.append(card);
    }
    $("overview").replaceChildren(metrics);
    $("auto-state").textContent =
      `当前开关：同步${data.auto_sync ? "开启" : "关闭"}，处理${data.auto_process ? "开启" : "关闭"}。后台执行须另行授权；开启不会补跑全部历史。`;
  } catch (error) {
    $("overview").replaceChildren(el("p", error.message, "error"));
  }
}
const taskLabels = { queued: "已排队", running: "运行中", succeeded: "完成", partial: "部分完成", failed: "失败", cancelled: "已取消", blocked: "需人工处理" };
const sourceStates = { not_started: "尚未执行", running: "正在同步", ready: "本批已保存", partial: "部分保存", cancelled: "已取消", paused: "检查点已暂停", blocked: "需人工处理" };
async function loadModels() {
  const epoch = ++modelEpoch;
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
  $("model-state").textContent = data.configuration_enabled
    ? "模型配置已单独授权。密钥写入固定的库外私有文件，尚不是加密钥匙串；不进入资料、导出或AI读接口。旧任务所需凭据保留，轮换默认值不会撤销旧凭据。"
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
    confirmation.id = `model-${role}-confirmed`;
    confirmation.required = true;
    const check = el("label", undefined, "check");
    check.append(confirmation, el("span", "确认该接口可信，允许后台将凭据用于此接口；保存不发请求"));
    const button = el("button", "保存" + names[role]);
    button.type = "submit";
    fields.append(check, button);
    form.append(fields);
    form.addEventListener("submit", async (event) => {
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
      } finally { key.value = ""; confirmation.checked = false; button.disabled = !canManage; }
    });
    section.append(form);
    $("model-forms").append(section);
  }
}
$("refresh-models").addEventListener("click", loadModels);
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
$("link-form").addEventListener("submit", async (event) => {
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
    button.disabled = false;
    if (epoch === linkEpoch) $("link-confirmed").checked = false;
  }
});
$("link-refresh").addEventListener("click", async () => {
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
  if (connectionVersion) connectionExpiry = setTimeout(() => {
    connectionVersion = null;
    $("self-source-fields").disabled = $("folder-source-fields").disabled = true;
    $("connection-state").textContent = "账号证明已过期，请重新验证独立登录；页面不会自行访问平台。";
  }, Math.max(0, Math.min(900000, new Date(data.expires_at).getTime() - Date.now())));
}
$("refresh-connection").addEventListener("click", loadSources);
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
  if (run.active) connectionPoll = setTimeout(async () => {
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
  $(id).addEventListener("click", async () => {
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
$("connection-cancel").addEventListener("click", async () => {
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
  $(form).addEventListener("submit", async (event) => {
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
  manual.addEventListener("click", async () => {
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
  toggle.addEventListener("click", async () => {
    const enabled = !timer?.enabled;
    if (enabled && !interval.checkValidity()) { interval.reportValidity(); return; }
    if (enabled && !confirm(`允许后台按每${interval.value}分钟访问此固定来源，每批最多${scope.limit}条？休眠后不补跑全部错过批次；不会自动授权模型。`)) return;
    await sourceAction(toggle, "source-timer", { config_id: !enabled && timer ? timer.config_id : scope.config_id, enabled, interval_minutes: !enabled && timer ? timer.interval_minutes : Number(interval.value), reset_blocked: false, source_confirmed: enabled }, enabled ? "定时规则已保存；等待已授权后台，不代表后台在线。" : "定时已暂停，手动任务不受影响，已开始访问在检查点停止。");
  });
  controls.append(manual, intervalLabel, interval, toggle);
  if (timer?.blocked_job_id) {
    const retry = el("button", "确认重新尝试", "secondary");
    retry.type = "button";
    retry.addEventListener("click", async () => {
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
  } finally { button.disabled = !canManage; }
}
$("creator-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await sourceAction(event.submitter, "source-create", { creator_url: $("creator-url").value, limit: Number($("creator-limit").value), download: $("creator-download").checked }, "博主范围已保存；尚未排队或访问来源。已有不同配置不会被覆盖。");
});
$("refresh-sources").addEventListener("click", async () => { sourceOffset = 0; await loadSources(); });
$("sources-more").addEventListener("click", async () => { sourceOffset = Number($("sources-more").dataset.offset); await loadSources(); });
function workerText(worker) {
  if (!worker) return "后台状态未知，请刷新确认。";
  const state = worker.online === true ? "后台持有运行锁" : worker.online === false ? "未检测到运行中的后台" : "后台状态未知，未猜测接管";
  const capability = (value) => value === true ? "允许" : value === false ? "未允许" : "权限未知";
  return `${state}。同步${capability(worker.capabilities.source_sync)}，模型调用${capability(worker.capabilities.model_calls)}。截至 ${worker.observed_at}，这不代表任务有进度或已成功。`;
}
function showWorkerSnapshot(id, worker, prefix = "", suffix = "") {
  clearTimeout(workerSnapshots.get(id));
  $(id).textContent = prefix + workerText(worker) + suffix;
  workerSnapshots.set(id, setTimeout(() => {
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
    data.prepared.forEach((item) => {
      const label = el("label", undefined, "check");
      const input = el("input");
      input.type = "checkbox";
      input.value = item.input_id;
      input.addEventListener("change", updateHistoryBudget);
      label.append(input, el("span", item.title || "无标题资料"));
      $("prepared-items").append(label);
    });
    if (!data.prepared.length) $("prepared-items").append(el("p", "当前页没有已准备媒体的资料。", "muted"));
    const auto = tasks.automatic;
    $("auto-enabled").value = auto.enabled ? "yes" : "no";
    $("auto-calls").value = auto.max_calls_per_task ?? 2;
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
    button.addEventListener("click", async () => {
      if (!confirm("取消此任务的后续处理？已经发出的请求可能继续计费。")) return;
      await managementAction(button, "cancel", { job_id: task.job_id }, "已取消后续处理。");
    });
    row.append(button);
  }
  if (task.mode === "automatic" && task.state === "queued") {
    const button = el("button", "预览恢复", "secondary");
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const preview = await api("/v1/management/resume-preview", { job_ids: [task.job_id] });
        if (confirm(`恢复这1条旧自动任务？原固定模型、最多${preview.max_calls}次请求，金额未知。自动开关需开启。`)) {
          await api("/v1/management/resume", { job_ids: preview.job_ids, preview_token: preview.preview_token, fee_confirmed: true });
          $("management-feedback").textContent = "已授权选定积压，等待后台执行器。";
          await loadManagement();
        }
      } catch (error) { $("management-feedback").textContent = error.message; }
      finally { button.disabled = false; }
    });
    row.append(button);
  }
  $("task-list").append(row);
}
function historySelection() {
  return [...$("prepared-items").querySelectorAll("input:checked")].map((input) => input.value);
}
function updateHistoryBudget() {
  $("history-budget").textContent = `选中 ${historySelection().length} 条，本批请求上限 ${historySelection().length * Number($("history-calls").value)}；金额未知。`;
}
async function managementAction(button, action, value, message) {
  button.disabled = true;
  try {
    await api(`/v1/management/${action}`, value);
    $("management-feedback").textContent = message;
    await loadOverview();
    await loadManagement();
  } catch (error) { $("management-feedback").textContent = error.message; }
  finally { button.disabled = false; }
}
$("auto-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await managementAction(event.submitter, "automatic", {
    enabled: $("auto-enabled").value === "yes", max_calls: Number($("auto-calls").value),
    max_new_tasks: Number($("auto-tasks").value), fee_confirmed: $("auto-fee").checked,
  }, "自动规则已保存。同步计划和已发出的请求不受此开关撤回。");
});
$("history-calls").addEventListener("input", updateHistoryBudget);
$("history-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input_ids = historySelection();
  if (!input_ids.length) { $("management-feedback").textContent = "请先选择资料。"; return; }
  const max_calls = Number($("history-calls").value);
  const fingerprint = JSON.stringify({ input_ids, max_calls });
  if (fingerprint !== historyFingerprint) { historyKey = crypto.randomUUID(); historyFingerprint = fingerprint; }
  await managementAction(event.submitter, "history", {
    input_ids, max_calls, idempotency_key: historyKey, fee_confirmed: $("history-fee").checked,
  }, "历史批次已登记，等待单独授权的后台执行器；重复提交同一批次不会重复建任务。");
});
$("refresh-tasks").addEventListener("click", async () => { taskOffset = preparedOffset = 0; await loadOverview(); await loadManagement(); });
$("tasks-more").addEventListener("click", async () => { taskOffset = Number($("tasks-more").dataset.offset); await loadManagement(); });
$("prepared-more").addEventListener("click", async () => { preparedOffset = Number($("prepared-more").dataset.offset); await loadManagement(); });
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadItems(false);
});
$("reset-search").addEventListener("click", () => {
  $("query").value = "";
  $("source-filter").value = "";
  loadItems(false);
});
$("copy-json").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("json-output").textContent); $("copy-json").textContent = "已复制 JSON"; }
  catch { $("copy-json").textContent = "复制失败，可手动选择文本"; }
});
$("source-filter").addEventListener("change", () => loadItems(false));
$("more-items").addEventListener("click", () => loadItems(true));
api("/v1/session")
  .then(enter)
  .catch(() => showLogin());
