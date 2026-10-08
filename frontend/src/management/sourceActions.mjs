// Each intentional refresh is a new batch; source settings remain a fixed snapshot.
export function canSyncSource(scope,connected){
  return !scope.pending_count&&!['queued','running'].includes(scope.latest_job?.state)
    &&scope.account_matches!==false&&(scope.kind==='creator'||connected);
}

export async function loadSourceScopes(api,{requireWorker=false}={}){
  const scopes=[],seen=new Set();let offset=0;
  do{
    if(seen.has(offset))throw new Error('来源分页异常，请刷新后重试。');
    seen.add(offset);
    const page=await api('/v1/management/sources',{offset});
    if(requireWorker&&page.worker?.online!==true)throw new Error('后台未运行，请重新打开应用。');
    scopes.push(...page.scopes);offset=page.next_offset;
  }while(offset!=null);
  return scopes;
}

export async function syncAllSources(api,connected){
  const scopes=await loadSourceScopes(api,{requireWorker:true});
  const result={queued:0,skipped:0,failed:[]};
  for(const scope of scopes){
    if(!canSyncSource(scope,connected)){result.skipped++;continue;}
    try{
      await api('/v1/management/source-submit',{config_id:scope.config_id,idempotency_key:crypto.randomUUID(),source_confirmed:true});
      result.queued++;
    }catch(error){result.failed.push({title:scope.title||scope.kind,message:error.message});}
  }
  return result;
}

export async function ensureSource(api,choice,scopes,connection,options,preserveExisting=false){
  const kind=choice.startsWith('folder:')?'collection':choice;
  const collection_id=kind==='collection'?choice.slice(7):null;
  const current=scopes.find(scope=>scope.kind===kind&&scope.account_matches!==false&&(kind!=='collection'||scope.collection_id===collection_id));
  if(current){
    if(preserveExisting||current.limit===options.limit&&current.download===options.download)return current;
    const result=await api('/v1/management/source-edit',{config_id:current.config_id,...options});
    const updated=result.updated_source||result.scopes?.find(scope=>scope.scope_id===current.scope_id);
    if(!updated)throw new Error('来源已更新，请刷新来源列表后再同步。');
    return updated;
  }
  return api('/v1/management/self-source-create',{kind,collection_id,connection_version:connection.version,...options,source_confirmed:true});
}
