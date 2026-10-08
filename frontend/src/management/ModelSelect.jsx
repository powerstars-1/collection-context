import {useEffect,useId,useRef,useState} from 'react';
import {Check,ChevronDown,Search} from 'lucide-react';
import {input} from '../ui';

// One selection control for both the complete provider catalog and role models.
export function ModelSelect({label,value,options,onChange,placeholder='请选择',disabled=false,manual=false,onManual}) {
  const id=useId(),root=useRef(null),trigger=useRef(null),searchInput=useRef(null);
  const [open,setOpen]=useState(false),[query,setQuery]=useState(''),[active,setActive]=useState(-1);
  const selected=options.find(option=>option.value===value);
  const filtered=options.filter(option=>!query.trim()||`${option.label} ${option.value} ${option.keywords||''}`.toLowerCase().includes(query.trim().toLowerCase()));
  const available=filtered.map((option,index)=>option.disabled?-1:index).filter(index=>index>=0);
  useEffect(()=>{
    if(!open)return;
    searchInput.current?.focus();
    function outside(event){if(!root.current?.contains(event.target))setOpen(false)}
    document.addEventListener('pointerdown',outside);return ()=>document.removeEventListener('pointerdown',outside);
  },[open]);
  useEffect(()=>{setActive(-1)},[query]);
  useEffect(()=>{if(active>=0)root.current?.querySelector(`[data-option-index="${active}"]`)?.scrollIntoView({block:'nearest'})},[active]);
  useEffect(()=>{if(disabled)setOpen(false)},[disabled]);
  function close(){setOpen(false);trigger.current?.focus()}
  function choose(option){if(option.disabled)return;onChange(option.value);close()}
  function keys(event){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close()}
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();const current=available.indexOf(active),direction=event.key==='ArrowDown'?1:-1;
      setActive(available.length?available[(current<0?(direction===1?0:available.length-1):(current+direction+available.length)%available.length)]:-1);
    }
    if(event.key==='Enter'){event.preventDefault();if(active>=0)choose(filtered[active])}
  }
  return <div ref={root} className="relative min-w-0" onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget))setOpen(false)}}>
    <span id={`${id}-label`} className="mb-2 block text-sm font-medium text-zinc-700">{label}</span>
    <button ref={trigger} type="button" role="combobox" aria-labelledby={`${id}-label`} aria-expanded={open} aria-controls={`${id}-list`}
      disabled={disabled} className={input+' flex items-center justify-between gap-3 text-left'}
      onClick={()=>{setQuery('');setActive(-1);setOpen(!open)}}
      onKeyDown={event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();setQuery('');setOpen(true)}}}>
      <span className={'truncate '+(!value?'text-zinc-400':'')}>{selected?.label||value||placeholder}</span>
      <ChevronDown size={16} aria-hidden="true" className={'shrink-0 text-zinc-400 transition '+(open?'rotate-180':'')}/>
    </button>
    {open&&<div className="absolute left-0 right-0 top-full z-30 mt-2 overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-lg" onKeyDown={keys}>
      <div className="flex items-center gap-2 border-b border-zinc-100 px-3 py-2.5"><Search size={15} className="shrink-0 text-zinc-400" aria-hidden="true"/>
        <input ref={searchInput} type="text" value={query} onChange={event=>setQuery(event.target.value)} aria-label={`搜索${label}`} aria-controls={`${id}-list`}
          aria-activedescendant={active>=0?`${id}-option-${active}`:undefined} autoComplete="off" className="min-w-0 flex-1 bg-transparent text-sm text-zinc-800 outline-none" placeholder={`搜索${label}…`}/>
      </div>
      <div id={`${id}-list`} role="listbox" aria-labelledby={`${id}-label`} className="max-h-64 overflow-y-auto p-1.5">
        {filtered.map((option,index)=><button type="button" role="option" id={`${id}-option-${index}`} data-option-index={index} key={option.value}
          tabIndex={-1} aria-selected={value===option.value} aria-disabled={option.disabled||undefined}
          onClick={()=>choose(option)} onMouseEnter={()=>setActive(option.disabled?-1:index)}
          className={'flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left text-sm '+
            (option.disabled?'cursor-not-allowed text-zinc-400':active===index?'bg-zinc-100 text-zinc-900':'text-zinc-800 hover:bg-zinc-50')}>
          <span className="min-w-0"><span className="block truncate font-medium">{option.label}</span>{option.description&&<span className="mt-0.5 block truncate text-xs text-zinc-500">{option.description}</span>}</span>
          {value===option.value&&<Check size={16} className="shrink-0" aria-hidden="true"/>}
        </button>)}
        {!filtered.length&&<p className="px-3 py-4 text-sm text-zinc-500">没有匹配的{label}。</p>}
      </div>
      {manual&&<button type="button" className="w-full border-t border-zinc-100 px-4 py-3 text-left text-sm font-medium text-zinc-700 hover:bg-zinc-50" onClick={()=>{onManual?.();close()}}>手动填写模型名称</button>}
    </div>}
  </div>;
}
