import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Network, Layers, Video, Lightbulb, PenTool, Bookmark, ArrowLeft, ArrowRight, Plus, Search, SlidersHorizontal, Clock, Settings2, MoreHorizontal, ChevronDown, X, RefreshCw, Check, ExternalLink, Copy, FileText, Headphones, Image, Play, Pause, Link2, Heart, Folder, LoaderCircle, AlertCircle, Trash2 } from "lucide-react";
import { NeuralWorkspace } from "../src/workspace/NeuralWorkspace.jsx";
import "./style.css";
import "./spatial.css";
const modules = [["concepts", "\u6982\u5FF5\u7F51\u7EDC", Network, "\u8FDE\u63A5\u503C\u5F97\u53CD\u590D\u601D\u8003\u7684\u6982\u5FF5"], ["methods", "\u65B9\u6CD5\u5E93", Layers, "\u79EF\u7D2F\u53EF\u4EE5\u590D\u7528\u7684\u65B9\u6CD5"], ["works", "\u4F5C\u54C1\u5E93", Video, "\u7559\u4E0B\u6BCF\u4E00\u6B21\u521B\u4F5C"], ["ideas", "\u9009\u9898\u6C60", Lightbulb, "\u8BA9\u4E00\u4E2A\u5FF5\u5934\u7EE7\u7EED\u751F\u957F"], ["drafts", "\u521B\u4F5C\u53F0", PenTool, "\u4ECE\u7075\u611F\u8D70\u5411\u4F5C\u54C1"], ["library", "\u6536\u85CF\u5E93", Bookmark, "\u4FDD\u5B58\u3001\u8BFB\u61C2\uFF0C\u518D\u4F7F\u7528"]];
const samples = [["\u8BA9\u6536\u85CF\u7684\u6559\u7A0B\uFF0C\u53D8\u6210\u968F\u65F6\u80FD\u7528\u7684\u65B9\u6CD5", "\u5DE5\u4F5C\u6D41\u7814\u7A76\u5BA4", "\u5DF2\u5B8C\u6210"], ["\u7528\u4E00\u675F\u5149\uFF0C\u642D\u5EFA\u6709\u7A7A\u95F4\u611F\u7684\u4EA7\u54C1\u753B\u9762", "\u5149\u7EBF\u7B14\u8BB0", "\u5DF2\u5B8C\u6210"], ["\u4E00\u4E2A\u77ED\u7247\u7684\u5206\u955C\uFF0C\u662F\u600E\u6837\u63A8\u6F14\u51FA\u6765\u7684", "\u6162\u955C\u5934", "\u5F85\u63D0\u53D6"], ["\u628A\u6563\u843D\u7684\u7075\u611F\uFF0C\u6574\u7406\u6210\u81EA\u5DF1\u7684\u9009\u9898\u6C60", "\u5468\u672B\u521B\u4F5C\u8BA1\u5212", "\u90E8\u5206\u5B8C\u6210"], ["\u4ECE\u53C2\u8003\u56FE\u5230\u53EF\u590D\u7528\u7684\u8BBE\u8BA1\u8BED\u8A00", "\u754C\u9762\u89C2\u5BDF", "\u5DF2\u5B8C\u6210"]];
function Button({ children, icon: Icon, kind = "secondary", small = false, busy = false, ...props }) {
  return <button {...props} className={`btn ${kind} ${small ? "small" : ""} ${props.className || ""}`} aria-busy={busy || void 0}>{busy ? <LoaderCircle size={16} className="spinner" /> : Icon ? <Icon size={16} /> : null}{children}</button>;
}
function IconButton({ label, icon: Icon, ...props }) {
  return <button {...props} className="icon-btn" aria-label={label} title={label}><Icon size={18} /></button>;
}
function Tag({ children, kind = "neutral" }) {
  return <span className={"tag " + kind}>{children}</span>;
}
function Modal({ title, children, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    ref.current?.focus();
    return () => previous?.focus?.();
  }, []);
  return <div className="overlay" onClick={(e) => {
    if (e.target === e.currentTarget) onClose();
  }} onKeyDown={(e) => {
    if (e.key === "Escape") onClose();
    if (e.key === "Tab") {
      const els = [...ref.current.querySelectorAll('button:not(:disabled),input,select,[tabindex="0"]')];
      const first = els[0], last = els.at(-1);
      if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
    }
  }}><section className="dialog" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}><header><h2>{title}</h2><IconButton label="关闭弹窗" icon={X} onClick={onClose} /></header>{children}</section></div>;
}
function App() {
  const [page, setPage] = useState("workspace"), [module, setModule] = useState(""), [detail, setDetail] = useState(false), [tab, setTab] = useState("\u603B\u7ED3"), [modal, setModal] = useState(""), [toast, setToast] = useState(""), [paused, setPaused] = useState(false), [query, setQuery] = useState(""), [added, setAdded] = useState(["ai", "saved"]), [menu, setMenu] = useState(""), [auto, setAuto] = useState(["ai"]);
  const timer = useRef(), travel = useRef();
  const [returning, setReturning] = useState(false), [source, setSource] = useState("all"), [filter, setFilter] = useState("all"), [selectedId, setSelectedId] = useState("demo-0");
  useEffect(() => () => clearTimeout(travel.current), []);
  function expand(id) { clearTimeout(travel.current); setReturning(false); setModule(id); setDetail(false); }
  function overview() {
    if (page !== "workspace") { go("workspace"); return; }
    clearTimeout(travel.current); setDetail(false); setReturning(true);
    travel.current = setTimeout(() => { setModule(""); setReturning(false); }, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 420);
  }
  const demoItems = samples.map(([title, author, state], i) => ({id: "demo-"+i, title, author, state: i===2 ? "missing" : i===3 ? "partial" : "ready", sources: ["saved"]}));
  const visibleItems = demoItems.filter(x => x.title.includes(query) && (source==="all" || x.sources.includes(source)) && (filter==="all" || (filter==="readable" ? x.state!=="missing" : x.state==="missing")));
  const selectedItem = demoItems.find(x=>x.id===selectedId);
  useEffect(() => {
    document.querySelector('.utility')?.scrollTo(0, 0);
  }, [page]);
  useEffect(() => () => clearTimeout(timer.current), []);
  function notify(t) {
    clearTimeout(timer.current);
    setToast(t);
    timer.current = setTimeout(() => setToast(""), 4e3);
  }
  function go(p) {
    clearTimeout(travel.current); setReturning(false);
    setPage(p);
    setModule("");
    setDetail(false);
    setModal("");
    setMenu("");
  }
  const expanded = page === "workspace" && module, current = modules.find((m) => m[0] === module);
  return <><div className="preview-ribbon"><span><i /> 视觉预览 · 示例数据，不连接真实资料库</span><div><button onClick={overview}>工作台</button><button onClick={() => {
    go("workspace");
    setModule("library");
    setDetail(true);
  }}>三栏阅读</button><button onClick={() => go("sync")}>同步来源</button><button onClick={() => go("components")}>组件规范</button></div></div><div className={"shell " + (page === "workspace" ? "scene-shell" : "utility-shell")}><div className="starfield" aria-hidden="true" /><header className="top"><button className="brand" onClick={overview}><span className="brand-symbol">✧</span>创作空间</button><nav aria-label="主导航">{[["workspace", "\u5DE5\u4F5C\u53F0"], ["works", "\u4F5C\u54C1\u5E93"], ["drafts", "\u521B\u4F5C\u53F0"], ["ideas", "\u9009\u9898\u6C60"], ["library", "\u6536\u85CF\u5E93"]].map(([key, title]) => <button key={key} aria-current={(page === "workspace" ? module || page : page) === key ? "page" : void 0} onClick={() => {
    if (key === "workspace") overview();
    else { go("workspace"); setModule(key); }
  }}>{title}</button>)}</nav><div className="top-actions"><IconButton label="任务状态" icon={Clock} onClick={() => notify("\u6837\u5F0F\u9884\u89C8\u672A\u63A5\u5165\u771F\u5B9E\u4EFB\u52A1")} /><IconButton label="设置" icon={Settings2} onClick={() => go("components")} /></div></header>
 {page === "workspace" ? <main className={"spatial-preview " + (expanded ? "expanded " : "") + (detail ? "detail-open" : "")}>
 <NeuralWorkspace allItems={demoItems} items={visibleItems} selected={detail ? selectedItem : null} source={source} module={module}
 onSource={setSource} onExpand={expand} onOverview={overview} onOpen={id=>{setSelectedId(id);setDetail(true);setTab("总结")}}
 onLibrary={()=>notify("这里已是收藏列表；本预览展示视觉与阅读交互，不连接真实资料。")} onSync={()=>go("sync")}
 query={query} onQuery={setQuery} filter={filter} onFilter={setFilter} returning={returning} detailOpen={detail} totalCount={5} paused={paused} onPauseChange={setPaused}/>
 {expanded && module !== "library" && !returning && <section className="planned"><Tag>规划中</Tag><h1>{current?.[1]}</h1><p>{current?.[3]}。</p><div className="plan-rule" /><span>这个入口保留在工作台中。当前先把收藏、提取与阅读做好。</span><Button icon={Bookmark} onClick={()=>expand("library")}>浏览现有收藏</Button></section>}
 <aside className={"reader " + (detail && module === "library" ? "visible" : "")} aria-hidden={!detail} inert={!detail ? true : void 0}><header><Button small kind="ghost" icon={ArrowLeft} onClick={() => setDetail(false)}>收起详情</Button><div><IconButton label="复制示例总结" icon={Copy} onClick={() => {
    navigator.clipboard.writeText("\u793A\u4F8B\u603B\u7ED3\uFF1A\u6536\u85CF\u4E4B\u540E\uFF0C\u4FDD\u7559\u4E0A\u4E0B\u6587\u3001\u5173\u952E\u6B65\u9AA4\u4E0E\u6765\u6E90\uFF0C\u624D\u80FD\u5728\u9700\u8981\u65F6\u518D\u6B21\u4F7F\u7528\u3002").then(() => notify("\u5DF2\u590D\u5236\u793A\u4F8B\u603B\u7ED3")).catch(() => notify("\u8BF7\u5728\u9875\u9762\u9009\u4E2D\u6587\u5B57\u590D\u5236"));
  }} /><IconButton label="更多资料操作" icon={MoreHorizontal} onClick={() => notify("\u8FD9\u662F\u6837\u5F0F\u9884\u89C8\uFF0C\u672A\u63A5\u5165\u8D44\u6599\u64CD\u4F5C")} /></div></header><article><div className="article-eyebrow"><Bookmark size={14} /> 我的收藏 <span>已完成提取</span></div><h1>{selectedItem?.title}</h1><p className="meta">{selectedItem?.author} · 排版示例（非该视频真实提取）</p><div className="reader-tabs" role="tablist">{["\u603B\u7ED3", "\u97F3\u9891\u8F6C\u5199", "\u753B\u9762\u63D0\u53D6"].map((t) => <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t}</button>)}</div>{tab === "\u603B\u7ED3" ? <><div className="summary-lead">收藏并不是终点。真正有价值的是，下一次遇到同样的问题时，还能找到它、理解它，并用起来。</div><h2>这条内容讲了什么</h2><p>为教程保留三个层次：原始来源、关键操作，以及适用场景。把零散收藏变成一份可以随时调用的个人资料。</p><h2>可以直接照做的流程</h2><ol><li><strong>保存上下文</strong><p>留下原链接和作者，知道方法从哪里来。</p></li><li><strong>提取关键步骤</strong><p>将音频与画面里的要点对应起来。</p></li><li><strong>记录使用条件</strong><p>工具、输入要求与局限，和步骤一起保存。</p></li></ol><div className="prompt"><header><span>可复用提示词</span><Button small kind="ghost" icon={Copy} onClick={() => notify("\u8FD9\u662F\u4E00\u6BB5\u793A\u4F8B\u63D0\u793A\u8BCD")}>复制</Button></header><p>根据已有资料，整理适用场景、执行步骤与注意事项。保留来源；没有依据的部分明确标注。</p></div><div className="article-foot"><FileText size={14} />示例阅读稿 · 用于检查排版与可读性</div></> : tab === "\u97F3\u9891\u8F6C\u5199" ? <><h2>音频转写</h2><p className="transcript"><span>00:12</span>我们每天收藏很多教程，但真正需要用的时候，却想不起来放在哪里了。</p><p className="transcript"><span>00:28</span>可以给每条内容留下用途、步骤和来源，这样下次找到它，就能继续行动。</p></> : <><h2>画面提取</h2>{["\u4FDD\u7559\u6765\u6E90\u4E0E\u4E0A\u4E0B\u6587", "\u62C6\u51FA\u53EF\u64CD\u4F5C\u6B65\u9AA4", "\u6574\u7406\u590D\u7528\u6761\u4EF6"].map((s, i) => <details className="frame-group" key={s}><summary><span>0{i}:12</span>{s}<ChevronDown size={14} /></summary><p>示例画面文字。每一组独立展开，避免大量画面一次铺满阅读区。</p></details>)}</>}</article></aside>
</main> : page === "sync" ? <main className="utility"><div className="page-title"><div><span className="eyebrow">资料从这里开始</span><h1>同步来源</h1><p>把喜欢的内容留下来，慢慢变成自己的积累。</p></div><Button icon={ArrowLeft} onClick={() => {
    go("workspace");
    setModule("library");
  }}>返回收藏库</Button></div><section className="account"><div className="avatar"><span>沐</span></div><div><h2>我的抖音账号 <Tag kind="success">已连接 · 示例</Tag></h2><p>已添加的来源会出现在下方，随时可以调整。</p></div><Button icon={RefreshCw} onClick={() => notify("\u9884\u89C8\uFF1A\u6536\u85CF\u5939\u5217\u8868\u5DF2\u5237\u65B0\uFF0C\u672A\u8BF7\u6C42\u6296\u97F3")}>刷新收藏夹</Button><Button kind="ghost" onClick={() => notify("\u9884\u89C8\u4E0D\u4F1A\u66F4\u6539\u4F60\u7684\u767B\u5F55\u72B6\u6001")}>退出登录</Button></section><div className="section-heading"><div><h2>已添加的来源 <span>{added.length}</span></h2><p>自动同步与单次同步，各自独立控制。</p></div><div><Button icon={Plus} onClick={() => setModal("add")}>添加来源</Button><Button kind="primary" icon={RefreshCw} onClick={() => notify("\u8FD9\u662F\u89C6\u89C9\u9884\u89C8\uFF0C\u4E0D\u4F1A\u540C\u6B65\u771F\u5B9E\u6536\u85CF")}>同步全部</Button></div></div><div className="source-list">{[["ai", "AI \u7075\u611F\u4E0E\u6559\u7A0B", Folder, "12 \u6761\u5DF2\u4FDD\u5B58", "\u5DF2\u5B8C\u6210"], ["saved", "\u6211\u7684\u6536\u85CF", Bookmark, "8 \u6761\u5DF2\u4FDD\u5B58 \xB7 2 \u6761\u5F85\u91CD\u8BD5", "\u90E8\u5206\u5B8C\u6210"], ["liked", "\u6211\u7684\u559C\u6B22", Heart, "\u5C1A\u672A\u540C\u6B65", "\u5F85\u540C\u6B65"]].filter((s) => added.includes(s[0])).map(([id, name, Icon, count, state]) => <section className="source-card" key={id}><div className="source-icon"><Icon size={20} /></div><div className="source-info"><h3>{name}<Tag kind={state === "\u5DF2\u5B8C\u6210" ? "success" : state === "\u90E8\u5206\u5B8C\u6210" ? "warning" : "neutral"}>{state}</Tag></h3><p>{count}</p><small>{id === "saved" ? "\u90E8\u5206\u5185\u5BB9\u672A\u80FD\u4FDD\u5B58\uFF0C\u53EF\u5728\u4E0B\u4E00\u6B21\u540C\u6B65\u65F6\u7EE7\u7EED\u3002" : "\u4FDD\u7559\u6587\u5B57\u3001\u56FE\u7247\u4E0E\u539F\u59CB\u89C6\u9891"}</small></div><div className="source-actions"><label className="switch-label"><button role="switch" aria-checked={auto.includes(id)} aria-label={name + "\u81EA\u52A8\u540C\u6B65"} className={"switch " + (auto.includes(id) ? "on" : "")} onClick={() => setAuto((a) => a.includes(id) ? a.filter((x) => x !== id) : [...a, id])}><span /></button>自动同步</label><div><Button small icon={RefreshCw} onClick={() => notify("\u9884\u89C8\uFF1A\u5355\u6B21\u540C\u6B65\u64CD\u4F5C\uFF0C\u4E0D\u89E6\u53D1\u771F\u5B9E\u8BF7\u6C42")}>立即同步</Button><Button small icon={MoreHorizontal} aria-expanded={menu === id} onClick={() => setMenu(menu === id ? "" : id)}>更多</Button></div>{menu === id && <div className="menu">{["\u67E5\u770B\u5185\u5BB9", "\u540C\u6B65\u8BB0\u5F55", "\u540C\u6B65\u8BBE\u7F6E"].map((s) => <button key={s} onClick={() => {
    setMenu("");
    notify("\u9884\u89C8\uFF1A" + s);
  }}>{s}<ArrowRight size={14} /></button>)}<button className="danger-text" onClick={() => {
    setAdded(added.filter((x) => x !== id));
    setMenu("");
    notify("\u5DF2\u79FB\u9664\u793A\u4F8B\u6765\u6E90\uFF1B\u5237\u65B0\u9875\u9762\u53EF\u6062\u590D");
  }}><Trash2 size={14} />移除来源</button></div>}</div></section>)}</div><div className="subtle-note"><FileText size={16} /><p>移除来源只会停止后续同步，已经保存的内容仍留在收藏库中。</p></div></main> : <main className="utility component-page"><div className="page-title"><div><span className="eyebrow">Quiet Observatory · v1</span><h1>统一的控件，清楚的状态</h1><p>8 px 按钮圆角 · 默认高 40 px · 哑光表面 · 无弹跳</p></div><Button onClick={() => go("workspace")} icon={ArrowLeft}>返回工作台</Button></div><section className="sample-block"><h2>主要操作与次要操作</h2><div className="component-row"><Button kind="primary" icon={Plus} onClick={() => notify("\u4E3B\u6309\u94AE\u53CD\u9988")}>添加来源</Button><Button icon={RefreshCw} onClick={() => notify("\u6B21\u6309\u94AE\u53CD\u9988")}>刷新收藏夹</Button><Button kind="ghost" onClick={() => notify("\u8F85\u52A9\u64CD\u4F5C\u53CD\u9988")}>查看说明</Button><Button kind="danger" icon={Trash2} onClick={() => notify("\u5371\u9669\u64CD\u4F5C\u4EC5\u6F14\u793A")}>移除来源</Button><IconButton label="图标操作示例" icon={Settings2} onClick={() => notify("\u56FE\u6807\u6309\u94AE\u53CD\u9988")} /></div></section><section className="sample-block"><h2>处理中与不可用</h2><div className="component-row"><Button kind="primary" busy disabled>正在保存</Button><Button disabled>暂无可同步来源</Button><Button small icon={Copy} onClick={() => notify("\u5DF2\u590D\u5236\u793A\u4F8B")}>复制这一段</Button></div><p className="helper">不可用状态保持文字可读；处理状态保留按钮宽度。</p></section><section className="sample-block"><h2>输入、选择与状态</h2><div className="form-grid"><label>搜索收藏<input placeholder="标题、作者或工具名" /></label><label>处理范围<select><option>音频与画面</option><option>仅音频</option></select></label></div><div className="component-row"><Tag kind="success"><Check size={12} />已完成</Tag><Tag kind="warning"><AlertCircle size={12} />部分完成</Tag><Tag kind="danger">处理失败</Tag><Tag>等待处理</Tag></div></section><section className="sample-block"><h2>表格与长内容</h2><table><thead><tr><th>环节</th><th>得到什么</th><th>状态</th></tr></thead><tbody><tr><td>音频转写</td><td>带时间定位的可读文字</td><td>已完成</td></tr><tr><td>画面提取</td><td>分组后的要点与原始证据</td><td>已完成</td></tr></tbody></table></section></main>}
 </div>{modal === "add" && <Modal title="添加同步来源" onClose={() => setModal("")}><div className="dialog-body"><p>选择你希望留在本地的内容。</p><div className="dialog-tabs"><button aria-selected="true">账号内容</button><button onClick={() => notify("\u535A\u4E3B\u89E3\u6790\u5C5E\u4E8E\u4E1A\u52A1\u6D41\u7A0B\uFF0C\u672C\u6837\u5F20\u53EA\u6F14\u793A\u8D26\u53F7\u6765\u6E90")}>博主作品</button></div>{[["saved", "\u6211\u7684\u6536\u85CF", Bookmark, "\u4FDD\u5B58\u8D26\u53F7\u6536\u85CF\u7684\u4F5C\u54C1"], ["liked", "\u6211\u7684\u559C\u6B22", Heart, "\u4FDD\u7559\u8FD1\u671F\u611F\u5174\u8DA3\u7684\u5185\u5BB9"], ["ai", "AI \u7075\u611F\u4E0E\u6559\u7A0B", Folder, "\u6307\u5B9A\u6536\u85CF\u5939"]].map(([id, title, Icon, desc]) => <div className="candidate" key={id}><Icon size={20} /><div><strong>{title}</strong><p>{desc}</p></div>{added.includes(id) ? <span className="already"><Check size={14} />已添加</span> : <Button small icon={Plus} onClick={() => setAdded([...added, id])}>添加</Button>}</div>)}<details className="new-settings"><summary>新来源同步设置 <ChevronDown size={14} /></summary><label>每次同步<select><option>最近10条</option><option>最近20条</option></select></label><label><input type="checkbox" defaultChecked />保留原始媒体</label></details></div><footer><span>添加后，由你选择何时同步。</span><Button kind="primary" onClick={() => setModal("")}>完成</Button></footer></Modal>}{toast && <div role="status" className="toast"><Check size={16} />{toast}<IconButton label="关闭提示" icon={X} onClick={() => setToast("")} /></div>}</>;
}
createRoot(document.getElementById("root")).render(<App />);
