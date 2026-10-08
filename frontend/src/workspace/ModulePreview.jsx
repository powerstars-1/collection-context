import {ArrowLeft,ArrowRight,Bookmark,FileText,Settings2,Sparkles} from 'lucide-react';
import {modules} from './structure.mjs';

export function ModulePreview({id,compact=false,onGo,onClose}) {
  const module=modules.find(x=>x.id===id);
  if(!module?.planned)return null;
  return <section className={compact?'reader-pane module-preview compact-preview':'module-preview'} aria-label={module.name+'规划预览'}>
    {compact&&<div className="reader-bar"><button className="text-button" onClick={onClose}><ArrowLeft size={14}/>返回工作台</button><span className="planned-badge">规划中</span></div>}
    <div className="module-body">
      <div className="module-kicker"><span>{module.english.toUpperCase()}</span>{!compact&&<span className="planned-badge">规划中</span>}</div>
      <h1>{module.name}</h1><p className="module-tagline">{module.tagline}</p>
      <p className="module-purpose">{module.purpose}</p>
      <div className="module-flow"><div><small>从这里开始</small><strong>{module.input}</strong></div><ArrowRight size={18}/><div><small>在这里留下</small><strong>{module.output}</strong></div></div>
      <section className="module-relations"><FileText size={18}/><div><h2>与工作台的关联</h2><p>{module.relation}</p></div></section>
      <div className="module-boundary">当前可预览模块定位，尚未接入真实数据和编辑能力。</div>
      <div className="module-actions"><button className="secondary" onClick={()=>onGo('library')}><Bookmark size={14}/>浏览现有收藏</button>{!compact&&<button className="text-button" onClick={onClose}><ArrowLeft size={14}/>返回工作台</button>}</div>
    </div>
  </section>;
}
export function SettingsPreview({onGo,onScope}){return <div className="utility workspace-settings"><div className="section-title"><div className="eyebrow">WORKSPACE SETTINGS</div><h1>设置</h1></div><div className="setting-entry"><Sparkles size={21}/><div><h2>AI 接入</h2><p>复制资料，或把本地 CLI 交给你的 AI。</p></div><button className="secondary" onClick={()=>onGo('ai')}>查看接入 <ArrowRight size={14}/></button></div><div className="setting-entry"><Settings2 size={21}/><div><h2>模型服务</h2><p>沿用现有产品的模型设置；此样稿不读取或修改配置。</p></div><span className="planned-badge">未接线</span></div><button className="text-button scope-link" onClick={onScope}>查看当前实现范围 <ArrowRight size={14}/></button></div>}
