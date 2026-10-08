// Folder membership comes from saved source relations, never title/keyword guesses.
export function collectionFolders(scopes=[],coverage=[]){
  const rows=new Map();
  for(const scope of coverage){
    if(scope.kind==='collection'&&scope.scope_id)rows.set(scope.scope_id,{id:scope.scope_id,name:'历史收藏夹（名称未读取）',historical:true});
  }
  for(const scope of scopes){
    if(scope.kind==='collection'&&scope.scope_id)rows.set(scope.scope_id,{
      id:scope.scope_id,name:scope.title||'未命名收藏夹',historical:false,
      previousAccount:scope.account_matches===false,
    });
  }
  return [...rows.values()];
}
export function folderLabel(folder){
  return folder.name+(folder.previousAccount?' · 之前的账号':'');
}
