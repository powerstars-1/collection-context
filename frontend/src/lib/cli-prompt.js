export function cliPrompt(setup,mode='use') {
  if(mode==='install')return `请帮我安装“收藏上下文”的 CLI，并让你能读取我的收藏库。
先检查这台电脑是否已经安装；若已安装，复用现有程序和资料库，不重新初始化。
若未安装，请使用我提供的本项目源码或正式安装包，按照其中的安装文档完成安装；没有安装包时先向我要，不要猜测公共下载地址或安装同名第三方包。
安装后用帮助命令和一次只读检索验证。保留已有文件、账号与模型设置，不启动同步或收费提取。
最后告诉我是否安装成功，并演示如何找到一条内容、读到总结和保留来源。`;
  if(!setup?.cli.available)return '';
  const examples=JSON.parse(setup.cli.text),first=examples[0],index=first.args.lastIndexOf(first.action);
  const entry={command:first.command,base_args:first.args.slice(0,index),...(first.env?{env:first.env}:{}),actions:Object.fromEntries(examples.map(row=>[row.action,row.args.slice(row.args.lastIndexOf(row.action))]))};
  return `请把“收藏上下文”当作我的本地资料库使用。
当我问以前收藏过的内容时，先检索 3～5 条，再按返回的 material_ref 读取相关总结、转写或画面文字。默认优先总结；缺少总结时读取已有证据，不猜测缺失内容。
回答保留原作品来源，区分原文、模型总结和你自己的判断；收藏不等于我认可其中全部观点。
使用下方已确认的本机程序：把 base_args 与对应 actions 参数拼接，以参数数组执行；read/status 的占位引用替换为 search 返回的 material_ref。需要总结时将 read 的 artifact 改为 summary。不要把参数当作一整段 shell 文本。
读取不需要另填产品密钥，不会调用平台或云模型。添加链接、同步、删除、重新收费处理，需要我另外明确提出。

${JSON.stringify(entry,null,2)}`;
}
