// Pi owns provider protocols; this process owns no files, credentials or task state.
import { createModels, createProvider } from '@earendil-works/pi-ai';
import { builtinProviders } from '@earendil-works/pi-ai/providers/all';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import { openAIResponsesApi } from '@earendil-works/pi-ai/api/openai-responses.lazy';
import { anthropicMessagesApi } from '@earendil-works/pi-ai/api/anthropic-messages.lazy';
import { googleGenerativeAIApi } from '@earendil-works/pi-ai/api/google-generative-ai.lazy';

const compatibleApis={'openai-completions':openAICompletionsApi,'openai-responses':openAIResponsesApi,
  'anthropic-messages':anthropicMessagesApi,'google-generative-ai':googleGenerativeAIApi};

const aliases={xiaomi_plan:'xiaomi-token-plan-cn',ali_plan:'qwen-token-plan-cn'};
function baseUrl(provider) {return provider.baseUrl||provider.getModels()[0]?.baseUrl||''}
export function catalog() {
  // Offline reads only: no auth lookup, network refresh, or hand-picked providers.
  return builtinProviders().map(provider=>{
    const models=provider.getModels(),url=baseUrl(provider);
    const unavailable=!models.length?'不提供内容理解模型':!provider.auth.apiKey?'需要登录授权':
      ['amazon-bedrock','google-vertex','azure-openai-responses','cloudflare-ai-gateway','cloudflare-workers-ai'].includes(provider.id)?'需要专用云配置':null;
    return {id:provider.id,name:provider.name,base_url:url,source:'sdk',configurable:!unavailable,
      unavailable_reason:unavailable,auth_methods:Object.keys(provider.auth),
      models:models.map(model=>({id:model.id,name:model.name,input:model.input,reasoning:model.reasoning,api:model.api}))};
  });
}
export async function complete(value) {
  const profile=value.profile;
  const models=createModels();
  let model;
  if(profile.provider) {
    const id=aliases[profile.provider]||profile.provider;
    const provider=builtinProviders().find(provider=>provider.id===id);
    if(!provider||!provider.auth.apiKey)throw new Error('unsupported_provider');
    const registered=provider.getModels().find(model=>model.id===profile.model);
    // An unknown model must be explicitly registered as a compatible service,
    // not guessed into the first native model's protocol.
    if(!registered)throw new Error('unknown_provider_model');
    model={...registered,baseUrl:profile.base_url===baseUrl(provider)?registered.baseUrl:profile.base_url};
    models.setProvider(provider);
  } else {
    const api=profile.api||'openai-completions';
    if(!compatibleApis[api])throw new Error('unsupported_custom_protocol');
    model={id:profile.model,name:profile.model,api,provider:'configured',baseUrl:profile.base_url,
      input:['text','image'],reasoning:false,contextWindow:128000,maxTokens:8192,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0},compat:{supportsDeveloperRole:false,maxTokensField:'max_tokens'}};
    models.setProvider(createProvider({id:'configured',name:'Configured service',baseUrl:profile.base_url,
      auth:{apiKey:{name:'Configured service',resolve:async()=>({auth:{apiKey:profile.api_key}})}},
      models:[model],api:compatibleApis[api]()}));
  }
  let rawUsage=null,actualModel=null,requestId=null,rawPromise=Promise.resolve();
  const deadline=AbortSignal.timeout(profile.timeout*1000);
  const originalFetch=globalThis.fetch;
  const responseFetch=async(url,options)=>{
    const response=await originalFetch(url,{...options,signal:deadline,redirect:'error'});
    requestId=response.headers.get('x-request-id')||response.headers.get('request-id');
    const clone=response.clone();
    rawPromise=(async()=>{const reader=clone.body.getReader();let size=0,text='';const decoder=new TextDecoder();
      while(true){const {done,value:chunk}=await reader.read();if(done)break;size+=chunk.length;if(size>4000000){await reader.cancel();break}text+=decoder.decode(chunk,{stream:true})}
      for(const line of text.split('\n')){if(!line.startsWith('data:'))continue;try{
        const payload=JSON.parse(line.slice(5).trim());
        const usage=payload.usage||payload.message?.usage||payload.response?.usage;
        if(usage)rawUsage={...rawUsage,...usage};
        if(payload.usageMetadata)rawUsage={input_tokens:payload.usageMetadata.promptTokenCount,
          output_tokens:payload.usageMetadata.candidatesTokenCount,total_tokens:payload.usageMetadata.totalTokenCount,
          cache_read_input_tokens:payload.usageMetadata.cachedContentTokenCount};
        const returned=payload.model||payload.message?.model||payload.response?.model||payload.modelVersion;
        if(typeof returned==='string')actualModel=returned;
      }catch{}}
    })().catch(()=>{});
    return response;
  };
  const content=value.content.map(part=>part.type==='text'?part:{type:'image',data:part.image_url.url.split(',')[1],mimeType:part.image_url.url.slice(5).split(';')[0]});
  const context={messages:[{role:'user',content,timestamp:Date.now()}]};
  const options={apiKey:profile.api_key,env:{},signal:deadline,timeoutMs:profile.timeout*1000,maxRetries:0,fetch:responseFetch};
  let result;
  try {
    // Google SDK uses the global fetch rather than a custom fetch option. This
    // isolated one-request process gives every provider the same timeout and
    // redirect behavior without changing its protocol implementation.
    globalThis.fetch=responseFetch;
    if(profile.provider || (profile.api && profile.api!=='openai-completions')) {
      const parameters=profile.parameters||{};
      const reasoning=parameters.thinking?.type==='disabled'?'off':parameters.reasoning_effort==='none'?'off':parameters.reasoning_effort;
      result=await models.completeSimple(model,context,{...options,temperature:parameters.temperature,
        maxTokens:parameters.max_completion_tokens||parameters.max_tokens||Math.min(model.maxTokens,8192),reasoning});
    } else {
      result=await models.complete(model,context,{...options,onPayload:payload=>({...payload,...profile.parameters})});
    }
    await rawPromise;
  } finally {
    globalThis.fetch=originalFetch;
  }
  if(['error','aborted'].includes(result.stopReason))throw new Error('provider_request_failed');
  const text=result.content.filter(part=>part.type==='text').map(part=>part.text).join('\n').trim();
  if(!text)throw new Error('empty_model_output');
  return {text,actual_model:actualModel,usage:rawUsage,upstream_request_id:requestId,finish_reason:result.stopReason,status:result.stopReason==='length'?'partial':'ready'};
}
if(process.argv[1]&&import.meta.url===new URL('file://'+process.argv[1]).href) {
  try {let body='';for await(const chunk of process.stdin){body+=chunk;if(body.length>45000000)throw new Error('input_limit')}
    const value=JSON.parse(body);const result=value.action==='catalog'?catalog():await complete(value);process.stdout.write(JSON.stringify(result));
  }catch {process.stdout.write(JSON.stringify({error:'pi_request_failed'}));process.exitCode=1}
}
