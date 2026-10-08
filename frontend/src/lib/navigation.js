export function navigateTo(url) {
  history.pushState({},'',url);
  window.dispatchEvent(url.startsWith('#')?new HashChangeEvent('hashchange'):new PopStateEvent('popstate'));
}
export function libraryTitle(kind) {return ({liked:'喜欢',saved:'收藏',collection:'收藏夹',creator:'博主作品',link:'单条链接'})[kind]||'全部资料'}
export function filtersFor(kind,scope) {return {...(kind?{source_kinds:[kind]}:{}),...(scope?{scope_id:scope}:{})}}
export function cloudPlan(items) {
  if(!items.length||items.some(item=>!Number.isInteger(item.planned_calls_before_reuse)))return null;
  return {perItem:Math.max(...items.map(item=>item.planned_calls_before_reuse)),total:items.reduce((sum,item)=>sum+item.planned_calls_before_reuse,0)};
}
