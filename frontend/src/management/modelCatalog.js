export function searchProviders(providers, text) {
  const query=text.trim().toLowerCase();
  return providers.filter(provider=>!query||`${provider.name} ${provider.id}`.toLowerCase().includes(query));
}

export function modelsForRole(provider, role, remoteModels) {
  if(!provider)return [];
  if(role==='audio')return provider.audio_models||[];
  const models=remoteModels||provider.models||[];
  return role==='vision'?models.filter(model=>!model.input||model.input.includes('image')):models;
}

export function supportsRole(provider,role) {
  if(!provider)return true; // Retain existing user-configured custom services.
  if(role==='audio')return !!provider.audio_protocols?.length;
  return provider.source==='compatible'||modelsForRole(provider,role).length>0;
}

export function defaultProtocol(provider,role) {
  return role==='audio'?(provider?.audio_protocols?.[0]||'transcription'):'pi_chat';
}

export const modelApiOptions=[
  {value:'openai-completions',label:'OpenAI · Chat Completions',path:'/chat/completions',placeholder:'https://api.example.com/v1'},
  {value:'openai-responses',label:'OpenAI · Responses',path:'/responses',placeholder:'https://api.example.com/v1'},
  {value:'anthropic-messages',label:'Anthropic · Messages',path:'/v1/messages',placeholder:'https://api.example.com'},
  {value:'google-generative-ai',label:'Gemini · Generate Content',path:'/models/{模型}:streamGenerateContent',placeholder:'https://api.example.com/v1beta'},
];

export function serviceSupportsRole(service,provider,role) {
  return !(role==='audio'&&service.api&&service.api!=='openai-completions')&&supportsRole(provider,role);
}

export function modelEndpoint(baseUrl,api='openai-completions') {
  const option=modelApiOptions.find(option=>option.value===api);
  if(!option||!baseUrl.trim())return '';
  try {
    let base=baseUrl.trim().replace(/\/+$/,'');
    const suffix=api==='anthropic-messages'?'/messages':option.path;
    if(api!=='google-generative-ai'&&base.endsWith(suffix))base=base.slice(0,-suffix.length);
    if(!new URL(base).pathname.replace(/\//g,''))base+=api==='google-generative-ai'?'/v1beta':api.startsWith('openai-')?'/v1':'';
    if(api==='anthropic-messages'&&base.endsWith('/v1'))base=base.slice(0,-3);
    return base+option.path;
  }catch{return ''}
}
