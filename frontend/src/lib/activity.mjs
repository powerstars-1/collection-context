// Shared in-memory progress only. Never persist credentials or content in browser storage.
export const ACTIVE_MS=5000, IDLE_MS=30000, BACKOFF_MS=60000;
export const activityDelay=data=>data?.active_count>0||data?.connection_active?ACTIVE_MS:IDLE_MS;
export function mergeLiveJobs(prior=[],next=[]){
  const rows=new Map(prior.map(job=>[job.job_id,job]));
  for(const job of next){const old=rows.get(job.job_id);if(!old?.updated_at||!job.updated_at||old.updated_at<=job.updated_at)rows.set(job.job_id,{...old,...job})}
  return [...rows.values()].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''));
}
export function createActivityMonitor(api,{document:doc=globalThis.document,events=globalThis.window,schedule=setTimeout,cancel=clearTimeout,now=Date.now,locks=globalThis.navigator?.locks,channel=typeof BroadcastChannel==='function'?new BroadcastChannel('collection-context-activity-v1'):null}={}){
  let snapshot={data:null,error:''},timer=null,inflight=null,stamp=0,closed=false,rerun=false;
  const listeners=new Set();
  function emit(){for(const fn of listeners)fn(snapshot)}
  function plan(delay){cancel(timer);timer=null;if(listeners.size&&!doc?.hidden&&!closed)timer=schedule(tick,delay)}
  function receive(event){const value=event.data;if(value?.type==='changed'){invalidate();return;}if(value?.type!=='activity'||!Array.isArray(value.data?.jobs)||typeof value.at!=='number'||value.at<stamp||value.at>now()+1000)return;stamp=value.at;snapshot={data:value.data,error:''};if(!doc?.hidden)emit();if(!inflight)plan(activityDelay(snapshot.data))}
  if(channel)channel.onmessage=receive;
  async function tick(force=false){
    if(closed||!listeners.size||doc?.hidden)return;
    if(inflight){if(force)rerun=true;return inflight}
    inflight=(async()=>{
      try{
        const read=async()=>{if(!force&&stamp&&now()-stamp<activityDelay(snapshot.data)-1000)return;const data=await api('/v1/management/activity',{});if(closed)return;stamp=now();snapshot={data,error:''};channel?.postMessage({type:'activity',at:stamp,data});if(!doc?.hidden)emit()};
        if(locks)await locks.request('collection-context-activity-v1',read);else await read();
      }catch(err){if(!closed){snapshot={...snapshot,error:err.message};if(!doc?.hidden)emit()}}
      finally{inflight=null;if(rerun){rerun=false;stamp=0;plan(150)}else plan(snapshot.error?BACKOFF_MS:activityDelay(snapshot.data))}
    })();return inflight;
  }
  function visible(){cancel(timer);timer=null;if(!doc?.hidden){emit();void tick()}}
  function invalidate(){stamp=0;if(inflight)rerun=true;else plan(150)}
  function changed(){invalidate();channel?.postMessage({type:'changed'})}
  doc?.addEventListener?.('visibilitychange',visible);
  events?.addEventListener?.('collection-context-changed',changed);
  return {
    subscribe(fn){listeners.add(fn);fn(snapshot);if(listeners.size===1){void tick();}return()=>{listeners.delete(fn);if(!listeners.size){cancel(timer);timer=null}}},
    refresh(){cancel(timer);timer=null;return tick(true)},
    dispose(){closed=true;cancel(timer);listeners.clear();channel?.close();doc?.removeEventListener?.('visibilitychange',visible);events?.removeEventListener?.('collection-context-changed',changed)},
  };
}
