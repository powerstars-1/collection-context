// Display-only contract for a browser-authorized response; never executes argv.
const tools = {
  search_collections:'搜索收藏',
  read_collection:'受控读取',
  collection_status:'查看处理状态',
};
const runtimeLabels = {
  desktop_companion:'桌面安装 · 控制台伴侣',
  frozen_console:'独立控制台程序',
  development_source:'开发源码环境（需现有 Python 与依赖）',
  unavailable:'运行入口未确认',
};
const readPaths = [
  {method:'POST',path:'/v1/collections/search'},
  {method:'POST',path:'/v1/collections/read'},
  {method:'GET',path:'/v1/collections/{material_ref}/status'},
];
function requireValue(condition) {
  if(!condition)throw new Error('当前接入配置格式不兼容，请重新读取。');
}
const object = value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const text = value=>typeof value==='string'&&value.length>0&&value.length<=8192&&!/[\u0000-\u001f\u007f]/.test(value);
function command(value,withAction=false) {
  requireValue(object(value));
  const allowed = new Set(['command','args','env',...(withAction?['action']:[])]);
  requireValue(Object.keys(value).every(key=>allowed.has(key)));
  requireValue(text(value.command)&&(value.command.startsWith('/')||/^[A-Za-z]:[\\/]/.test(value.command)));
  requireValue(Array.isArray(value.args)&&value.args.length<=64&&value.args.every(text));
  if(withAction)requireValue(text(value.action));
  if(value.env!==undefined) {
    requireValue(object(value.env)&&Object.keys(value.env).every(key=>key==='PYTHONPATH'));
    requireValue(Object.values(value.env).every(text));
  }
  return {...(withAction?{action:value.action}:{}),command:value.command,args:[...value.args],
    ...(value.env===undefined?{}:{env:{...value.env}})};
}

export function agentSetupView(data,origin,legacyReadonly=false) {
  requireValue(object(data)&&['managed','legacy_readonly'].includes(data.library_mode));
  requireValue(Object.hasOwn(runtimeLabels,data.runtime_kind));
  requireValue((data.library_mode==='legacy_readonly')===legacyReadonly);
  requireValue(data.model_requests===0&&data.platform_requests===0);
  requireValue(Array.isArray(data.read_only_tools)&&data.read_only_tools.length===3
    &&new Set(data.read_only_tools).size===3&&data.read_only_tools.every(name=>Object.hasOwn(tools,name)));
  requireValue(object(data.mcp)&&typeof data.mcp.available==='boolean'&&data.mcp.same_computer_only===true);
  const mcp = {available:data.mcp.available,text:'',reason:data.mcp.available?'':'当前安装没有可用的本机控制台程序。'};
  if(text(data.mcp.reason))mcp.reason=data.mcp.reason;
  if(mcp.available) {
    const configuration = data.mcp.configuration;
    requireValue(object(configuration)&&Object.keys(configuration).length===1&&object(configuration.mcpServers));
    const entries = Object.entries(configuration.mcpServers);
    requireValue(entries.length===1&&text(entries[0][0]));
    mcp.text = JSON.stringify({mcpServers:{[entries[0][0]]:command(entries[0][1])}},null,2);
  } else {
    requireValue(data.mcp.configuration===null);
  }
  requireValue(object(data.cli)&&typeof data.cli.available==='boolean'&&Array.isArray(data.cli.examples));
  requireValue(data.cli.examples.length<=10);
  const examples = data.cli.examples.map(value=>command(value,true));
  requireValue(data.cli.available?examples.length>0:examples.length===0);
  const http = data.http;
  requireValue(object(http)&&text(http.base_url));
  const base = new URL(http.base_url);
  requireValue(['http:','https:'].includes(base.protocol)&&base.origin===origin
    &&!base.username&&!base.password&&!base.search&&!base.hash&&base.pathname==='/');
  requireValue(http.authentication==='Authorization: Bearer <专用只读产品口令>');
  requireValue(Array.isArray(http.read_paths)&&http.read_paths.length===readPaths.length
    &&http.read_paths.every((path,index)=>object(path)&&path.method===readPaths[index].method&&path.path===readPaths[index].path));
  requireValue(typeof http.same_computer_only==='boolean');
  requireValue(http.remote_note==null||typeof http.remote_note==='string'&&http.remote_note.length<=2000);
  return {
    libraryMode:data.library_mode,
    runtimeLabel:runtimeLabels[data.runtime_kind],
    tools:data.read_only_tools.map(name=>({name,label:tools[name]})),
    mcp,
    cli:{available:data.cli.available,text:JSON.stringify(examples,null,2)},
    http:{baseUrl:base.origin,sameComputerOnly:http.same_computer_only,remoteNote:http.remote_note||'远程 AI 需要单独配置受保护的访问方式；不要直接公开本地服务。',
      pathsText:http.read_paths.map(path=>`${path.method} ${path.path}`).join('\n'),
      example:`POST ${base.origin}/v1/collections/search\nAuthorization: Bearer <专用只读产品口令>\nContent-Type: application/json\n\n{"query":"UI 提示词","limit":3}`},
  };
}
