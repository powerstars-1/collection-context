import {useEffect,useRef,useState} from 'react';
import {Plus,Server,Check,ChevronDown,X} from 'lucide-react';
import {button,primary,input,Page,Card,Field,Notice,useData,useAction} from '../ui';
import {managementApi} from './Sources';
import {searchProviders,modelsForRole,serviceSupportsRole,defaultProtocol,modelApiOptions,modelEndpoint} from './modelCatalog';
import {ModelSelect} from './ModelSelect';

const roles={audio:'音频转写',vision:'画面识别',summary:'内容总结'};
const emptyEditor=(mode='provider')=>({provider:mode==='custom'?'custom':'',name:'',base_url:'',api_key:'',api:'openai-completions'});

export function ModelSettings({api}) {
  const query=useData(api,'/v1/management/model-services'),action=useAction();
  const [editing,setEditing]=useState(null),[mode,setMode]=useState('provider'),[choices,setChoices]=useState({});
  const [lists,setLists]=useState({}),[custom,setCustom]=useState({}),[check,setCheck]=useState(null);
  const editor=useRef(null);
  const services=query.data?.services||[],providers=query.data?.providers||[];
  const enabled=query.data?.configuration_enabled===true;
  const selected=providers.find(provider=>provider.id===editing?.provider);
  const providerOptions=searchProviders(providers,'').filter(provider=>provider.id!=='custom').map(provider=>({
    value:provider.id,label:provider.name,disabled:provider.configurable===false,
    description:provider.unavailable_reason||(services.some(service=>service.provider===provider.id)?'已配置':undefined),
  }));

  useEffect(()=>{
    if(!query.data)return;
    setChoices(query.data.assignments);
    if(!query.data.services.length)setEditing(emptyEditor());
  },[query.data]);
  useEffect(()=>{if(editing)editor.current?.scrollIntoView({block:'nearest',behavior:'smooth'})},[editing?.id]);
  const checkProgress=useData(api,'/v1/management/model-check-status',{job_id:check?.job_id},{enabled:['queued','running'].includes(check?.state),poll:value=>['queued','running'].includes(value?.state)?3000:false});
  useEffect(()=>{if(checkProgress.data?.job_id===check?.job_id)setCheck(checkProgress.data)},[checkProgress.data]);
  useEffect(()=>{if(checkProgress.error)action.setNotice(checkProgress.error)},[checkProgress.error]);

  function chooseProvider(id){
    const provider=providers.find(provider=>provider.id===id),saved=services.find(service=>service.provider===id);
    if(!provider)return;
    setEditing(saved?{...saved,api:saved.api||'openai-completions',api_key:''}:{...emptyEditor(),provider:id,name:provider.name,base_url:provider.base_url});
  }
  function editService(service){setMode(service.provider==='custom'?'custom':'provider');setEditing({...service,api:service.api||'openai-completions',api_key:''})}
  function choice(role,patch){setChoices(old=>({...old,[role]:{...old[role],...patch}}))}
  async function saveService(event){
    event.preventDefault();await action.run(async()=>{
      if(!selected)throw new Error('请选择供应商。');
      const payload={service_id:editing.id||null,name:editing.name.trim()||selected.name,provider:editing.provider,
        base_url:editing.base_url,api_key:editing.api_key,expected_revision:editing.revision||null};
      if(selected.source==='compatible')payload.api=editing.api;
      await managementApi(api,'model-service-save',payload);
      setEditing(null);query.refresh();action.setNotice('供应商已保存。');
    });
  }
  async function saveRoles(){await action.run(async()=>{
    const assignments={};for(const [role,value] of Object.entries(choices))if(value.service_id&&value.model){
      assignments[role]={service_id:value.service_id,model:value.model,protocol:role==='audio'?value.protocol:'pi_chat',
        timeout:Number(value.timeout)||120,parameters:value.parameters||{}};
    }
    if(!Object.keys(assignments).length)throw new Error('请先选择模型。');
    await managementApi(api,'model-assignments',{assignments,expected_profiles:Object.fromEntries(Object.entries(query.data.assignments).map(([role,value])=>[role,value.profile_id]))});
    query.refresh();action.setNotice('模型设置已保存。');
  })}
  async function refreshModels(service){await action.run(async()=>{
    const value=await managementApi(api,'model-list',{service_id:service.id});setLists(old=>({...old,[service.id]:value.models}));
    action.setNotice(value.notice||'模型列表已更新。');
  })}
  async function checkModel(role,label,value){await action.run(async()=>{
    const saved=query.data.assignments[role];
    if(JSON.stringify(value)!==JSON.stringify(saved))throw new Error('请先保存模型设置，再检测。');
    if(!confirm(`检测${label}？只发送一个测试样本，最多一次请求，可能产生费用。`))return;
    setCheck(await managementApi(api,'model-check',{role,expected_profile_id:saved.profile_id,idempotency_key:crypto.randomUUID(),fee_confirmed:true}));
  })}

  return <Page title="模型服务" action={!editing&&<button className={button} disabled={!enabled||action.busy} onClick={()=>{setMode('provider');setEditing(emptyEditor())}}><Plus size={15} className="mr-2" aria-hidden="true"/>接入供应商</button>}>
    <Notice error>{query.error||action.error||query.data?.catalog_error}</Notice><Notice>{action.notice}</Notice>
    {query.busy&&!query.data&&<Notice>正在读取模型配置…</Notice>}
    {query.data?.configuration_enabled===false&&<Notice error>模型配置暂不可用，请重新打开应用。</Notice>}

    {editing&&<div ref={editor}><Card title={editing.id?'编辑供应商':'接入模型服务'} action={services.length>0&&<button type="button" className="rounded-lg p-2 text-zinc-500 hover:bg-zinc-100 focus-visible:outline-2" disabled={action.busy} onClick={()=>setEditing(null)} aria-label="关闭接入表单"><X size={18}/></button>}>
      <form className="mx-auto max-w-2xl space-y-5" onSubmit={saveService}>
        <div role="tablist" aria-label="接入方式" className="flex rounded-xl bg-zinc-100 p-1">
          {[['provider','服务商接入'],['custom','自定义接入']].map(([key,label])=><button key={key} type="button" role="tab" id={`model-tab-${key}`} aria-controls="model-connect-panel" aria-selected={mode===key} disabled={action.busy}
            className={'flex-1 rounded-lg px-4 py-2.5 text-sm font-medium transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 '+(mode===key?'bg-white text-zinc-900 shadow-sm':'text-zinc-500 hover:text-zinc-900')}
            onClick={()=>{if(mode!==key){setMode(key);setEditing(emptyEditor(key))}}}>{label}</button>)}
        </div>
        <div id="model-connect-panel" role="tabpanel" aria-labelledby={`model-tab-${mode}`} className="space-y-5">
          {mode==='provider'?<ModelSelect label="供应商" options={providerOptions} value={editing.provider} onChange={chooseProvider} placeholder="选择供应商" disabled={!enabled||action.busy}/>:
            <Field label="接口协议"><select className={input} aria-label="接口协议" value={editing.api} disabled={action.busy} onChange={event=>setEditing({...editing,api:event.target.value})}>
              {modelApiOptions.map(option=><option value={option.value} key={option.value}>{option.label}</option>)}
            </select></Field>}
          {selected?.source==='compatible'&&<div>
            <Field label="接口地址"><input className={input} aria-label="接口地址" value={editing.base_url} disabled={action.busy} onChange={event=>setEditing({...editing,base_url:event.target.value})} required placeholder={modelApiOptions.find(option=>option.value===editing.api)?.placeholder}/></Field>
            {mode==='custom'&&modelEndpoint(editing.base_url,editing.api)&&<p className="mt-2 break-all text-xs text-zinc-400">请求地址：{modelEndpoint(editing.base_url,editing.api)}</p>}
          </div>}
          <Field label="API Key"><input className={input} aria-label="API Key" type="password" autoComplete="off" required={!editing.id} disabled={!selected||action.busy}
            value={editing.api_key} onChange={event=>setEditing({...editing,api_key:event.target.value})} placeholder={editing.id?'已设置，留空保留':'填写 API Key'}/></Field>
          <details className="text-sm text-zinc-500"><summary className="flex w-fit cursor-pointer list-none items-center gap-1.5"><ChevronDown size={14} aria-hidden="true"/>高级设置</summary><div className="mt-4 space-y-4">
            <Field label="显示名称"><input className={input} aria-label="显示名称" value={editing.name} onChange={event=>setEditing({...editing,name:event.target.value})} placeholder={selected?.name||'自定义接口'} maxLength={100}/></Field>
            {selected?.source==='sdk'&&<Field label="接口地址"><input className={input} aria-label="接口地址" value={editing.base_url} onChange={event=>setEditing({...editing,base_url:event.target.value})} required/></Field>}
          </div></details>
        </div>
        <div className="flex justify-end border-t border-zinc-100 pt-4"><button className={primary} disabled={!enabled||!selected||action.busy}>{action.busy?'保存中…':'保存供应商'}</button></div>
      </form>
    </Card></div>}

    {!!services.length&&<Card title="已接入供应商">
      <div className="divide-y divide-zinc-100">{services.map(service=><article className="flex flex-wrap items-center justify-between gap-4 py-4 first:pt-0 last:pb-0" key={service.id}>
        <div className="flex min-w-0 items-center gap-3"><span className="rounded-xl bg-zinc-100 p-2.5 text-zinc-500"><Server size={18} aria-hidden="true"/></span>
          <div className="min-w-0"><h3 className="truncate text-sm font-medium text-zinc-900">{service.name}</h3><p className="mt-1 flex items-center gap-1 text-xs text-zinc-500"><Check size={12} aria-hidden="true"/>Key 已保存</p></div>
        </div>
        <div className="flex flex-wrap items-center gap-2"><button className={button} disabled={action.busy} onClick={()=>editService(service)}>编辑</button>
          <button className={button} disabled={action.busy} onClick={()=>refreshModels(service)}>获取模型</button>
          <button className={button+' cc-button--danger'} disabled={action.busy} onClick={()=>action.run(async()=>{if(!confirm(`移除“${service.name}”？`))return;await managementApi(api,'model-service-remove',{service_id:service.id});query.refresh()})}>移除</button>
        </div>
      </article>)}</div>
    </Card>}

    <Card title="默认模型">
      {!services.length?<p className="text-sm text-zinc-500">接入供应商后，选择以下三种用途的模型。</p>:
        <div className="divide-y divide-zinc-100">{Object.entries(roles).map(([role,label])=>{
          const value=choices[role]||{},service=services.find(service=>service.id===value.service_id);
          const provider=providers.find(provider=>provider.id===service?.provider);
          const available=modelsForRole(provider,role,lists[service?.id]);
          const selectedServices=services.filter(service=>service.id===value.service_id||serviceSupportsRole(service,providers.find(provider=>provider.id===service.provider),role));
          const isCustom=provider?.source!=='sdk'||role==='audio';
          const unchanged=JSON.stringify(value)===JSON.stringify(query.data?.assignments[role]);
          return <section key={role} className="space-y-4 py-5 first:pt-0 last:pb-0">
            <h3 className="text-sm font-semibold text-zinc-900">{label}</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="供应商"><select className={input} aria-label={`${label}供应商`} value={value.service_id||''} disabled={action.busy||!selectedServices.length} onChange={event=>{
                const picked=services.find(service=>service.id===event.target.value),catalog=providers.find(provider=>provider.id===picked?.provider);
                setCustom(old=>({...old,[role]:false}));choice(role,{service_id:event.target.value,model:'',parameters:{},protocol:defaultProtocol(catalog,role)});
              }}><option value="">{selectedServices.length?'选择供应商':'暂无音频服务'}</option>{selectedServices.map(service=><option value={service.id} key={service.id}>{service.name}</option>)}</select></Field>
              <ModelSelect label={`${label}模型`} placeholder="选择模型" disabled={!service||action.busy} value={value.model||''}
                options={available.map(model=>({value:model.id,label:model.name||model.id,description:model.name&&model.name!==model.id?model.id:undefined}))}
                manual={isCustom} onManual={()=>setCustom(old=>({...old,[role]:true}))}
                onChange={model=>{setCustom(old=>({...old,[role]:false}));choice(role,{model})}}/>
            </div>
            {custom[role]&&<Field label="模型名称"><input className={input} aria-label={`${label}模型名称`} autoFocus value={value.model||''} onChange={event=>choice(role,{model:event.target.value})} placeholder="填写供应商提供的模型 ID"/></Field>}
            {service&&available.length===0&&!custom[role]&&isCustom&&<button type="button" className="text-xs text-zinc-500 underline underline-offset-4" onClick={()=>setCustom(old=>({...old,[role]:true}))}>手动填写模型名称</button>}
            <div className="flex flex-wrap items-start justify-between gap-4">
              <details className="text-sm text-zinc-500"><summary className="flex w-fit cursor-pointer list-none items-center gap-1.5"><ChevronDown size={14} aria-hidden="true"/>高级设置</summary><div className="mt-4 grid gap-4 sm:grid-cols-2">
                {role==='audio'&&(provider?.audio_protocols?.length!==1)&&<Field label="音频接口"><select className={input} value={value.protocol||'transcription'} onChange={event=>choice(role,{protocol:event.target.value})}><option value="transcription">音频转写</option><option value="chat_audio">多模态音频</option></select></Field>}
                <Field label="超时（秒）"><input className={input} type="number" min={1} max={600} value={value.timeout||120} onChange={event=>choice(role,{timeout:event.target.value})}/></Field>
                {role!=='audio'&&<Field label="温度"><input className={input} type="number" min={0} max={2} step="0.1" value={value.parameters?.temperature??''} placeholder="默认" onChange={event=>{
                  const parameters={...value.parameters};if(event.target.value==='')delete parameters.temperature;else parameters.temperature=Number(event.target.value);choice(role,{parameters});
                }}/></Field>}
              </div></details>
              <button className={button} disabled={action.busy||!value.profile_id||!unchanged||!query.data?.model_execution_enabled} onClick={()=>checkModel(role,label,value)}>检测模型</button>
            </div>
          </section>;
        })}</div>}
      {check&&<Notice error={['failed','blocked','partial'].includes(check.state)}>{['queued','running'].includes(check.state)?'正在检测…':check.state==='succeeded'?'模型响应正常。':'检测未通过，请检查供应商与模型。'}</Notice>}
      <div className="flex justify-end border-t border-zinc-100 pt-4"><button className={primary} disabled={action.busy||!enabled||!services.length} onClick={saveRoles}>保存模型设置</button></div>
    </Card>
  </Page>;
}
