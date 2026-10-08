"""One explicit, recorded model-interface probe through the normal background executor."""
from __future__ import annotations
import base64
import io
import wave
from collection_context.application.contracts import ContextError,digest,valid_id
from collection_context.processing.profiles import ModelCatalog
from collection_context.workflows.executor import Stage,StageOutcome,plan
from collection_context.workflows.jobs import JobManager

VERSION='model_check_v1'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMQ0bD5DwACRAF4aig0hQAAAABJRU5ErkJggg==')

def build(store,resolve,context,*,planning=False):
    if not isinstance(context,dict) or set(context)!={'schema_version','workflow','processor_version','model_profiles'} or context.get('workflow')!='model_check' or context.get('schema_version')!=1 or context.get('processor_version')!=VERSION:
        raise ContextError('invalid_extraction_plan','模型检测计划无效。')
    profiles=context['model_profiles']
    if not isinstance(profiles,dict) or len(profiles)!=1 or set(profiles)-{'audio','vision','summary'}:
        raise ContextError('invalid_extraction_plan','一次只检测一种用途。')
    role,identity=next(iter(profiles.items()));value=ModelCatalog(store).get(identity)
    if value['role']!=role:raise ContextError('model_config_changed','用途模型已变化。')
    def call(_):
        client=ModelCatalog(store).client(identity,resolve)
        if role=='summary':result=client.text('这是接口连接测试。请只回复：连接成功。')
        elif role=='vision':result=client.image(PNG,'请简短描述这个测试像素的颜色。',mime_type='image/png')
        else:
            buffer=io.BytesIO()
            with wave.open(buffer,'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b'\0'*32000)
            result=client.audio(buffer.getvalue(),'请转写测试音频。如果没有人声，请明确返回无语音。')
        return StageOutcome({'interface_responded':True,'role':role,'text':result.text[:1000]},status=result.status,
            actual_model=result.actual_model,usage=result.usage,upstream_request_id=result.upstream_request_id,elapsed_seconds=result.elapsed_seconds)
    return [Stage('model_check',digest(context),VERSION,call,paid=True,cacheable=False)]

def submit(store,*,role,expected_profile_id,idempotency_key,allow_model_calls=False):
    if allow_model_calls is not True:raise ContextError('processing_authorization_required','请确认一次检测的模型费用。')
    if not isinstance(role,str) or role not in {'audio','vision','summary'}:raise ContextError('invalid_argument','请选择转写、画面或总结模型。')
    profiles=ModelCatalog(store).pin((role,));identity=profiles.get(role)
    if identity!=valid_id(expected_profile_id):raise ContextError('model_config_conflict','请先保存当前用途，再检测。')
    context={'schema_version':1,'workflow':'model_check','processor_version':VERSION,'model_profiles':profiles}
    stages=build(store,lambda _:'planning-only',context,planning=True)
    return JobManager(store).submit('process',{'plan':plan(stages),'extraction':context},idempotency_key=idempotency_key,max_calls=1)
