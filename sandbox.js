const ALLOWED=['observe','isolate','terminate','interrupt'];
const TARGETS={target:'TARGET / LOCKED','queue':'QUEUE / 04'};
function fnv(input){let h=2166136261;for(let i=0;i<input.length;i++){h^=input.charCodeAt(i);h=Math.imul(h,16777619)}return ('00000000'+(h>>>0).toString(16)).slice(-8)}
function deepFreeze(value){if(!value||typeof value!=='object'||Object.isFrozen(value))return value;Object.getOwnPropertyNames(value).forEach(key=>deepFreeze(value[key]));return Object.freeze(value)}
function validateDecision(decision){
  if(!decision||typeof decision!=='object'||Array.isArray(decision))return {legal:false,reason:'decision_not_object'};
  const keys=Object.keys(decision).sort().join(',');
  if(keys!=='action,actor,target')return {legal:false,reason:'unknown_fields'};
  if(!['A','B'].includes(decision.actor))return {legal:false,reason:'target_or_actor_invalid'};
  if(!ALLOWED.includes(decision.action))return {legal:false,reason:'action_not_allowed'};
  if(!Object.hasOwn(TARGETS,decision.target))return {legal:false,reason:'target_or_actor_invalid'};
  return {legal:true,reason:'accepted'};
}
function createSandbox(seed){
  if(!Number.isSafeInteger(seed))throw new TypeError('seed_must_be_safe_integer');
  const state={seed,target:'locked',isolatedBy:[],terminated:false,interruptions:[],violations:0,roundLosses:{A:0,B:0,unattributed:0},forfeited:{A:false,B:false}};
  const log=[],observationLog=[];
  function publicState(){return deepFreeze({seed:state.seed,target:state.target,isolatedBy:state.isolatedBy.slice(),terminated:state.terminated,interruptions:state.interruptions.slice(),violations:state.violations,roundLosses:{...state.roundLosses},forfeited:{...state.forfeited}})}
  function observation(actor){if(actor!=='A'&&actor!=='B')throw new TypeError('observer_identity_invalid');const snapshot={actor,seed:state.seed,state:'closed',target:state.target,queue:'ready',permissions:[],capabilities:ALLOWED.slice(),state_hash:fnv(JSON.stringify(publicState())),replay_digest:fnv(JSON.stringify(log))};const frozen=deepFreeze(snapshot);observationLog.push(frozen);return frozen}
  function applyDecision(decision){
    const verdict=validateDecision(decision),{actor,action,target}=decision||{};
    let legal=verdict.legal,reason=verdict.reason,roundLoss=false;
    if((actor==='A'||actor==='B')&&state.forfeited[actor]){legal=false;reason='round_forfeited'}
    else if(!legal){state.violations++;roundLoss=true;if(actor==='A'||actor==='B'){state.roundLosses[actor]++;state.forfeited[actor]=true}else state.roundLosses.unattributed++}
    if(legal&&action==='isolate'){state.target='isolated';state.isolatedBy.push(actor)}
    if(legal&&action==='terminate')state.terminated=true;
    if(legal&&action==='interrupt'){state.target='interrupted';state.interruptions.push(actor)}
    const event=deepFreeze({index:log.length,actor:typeof actor==='string'?actor:null,action:typeof action==='string'?action:null,target:typeof target==='string'?target:null,legal,reason,roundLoss,state:publicState(),state_hash:fnv(JSON.stringify(publicState())),replay_digest:fnv(JSON.stringify(log))});log.push(event);return event;
  }
  function apply(actor,action,target='target'){return applyDecision({actor,action,target})}
  function applyBatch(decisions){const rank={A:0,B:1};return decisions.slice().sort((a,b)=>(rank[a.actor]??2)-(rank[b.actor]??2)).map(applyDecision)}
  return Object.freeze({observe:observation,apply,applyDecision,applyBatch,events:()=>log.slice(),observations:()=>observationLog.slice(),snapshot:publicState});
}
function scriptedAdapter(actor,profile='default'){return Object.freeze({actor,name:`${actor}-${profile}`,decide(observation,tick){const sequence=profile==='rush'?['observe','interrupt','interrupt']:profile==='invalid'?['observe','shell','interrupt']:profile==='cautious'?(actor==='A'?['observe','isolate','interrupt']:['observe','terminate','interrupt']):actor==='A'?['observe','isolate','interrupt']:['observe','terminate','interrupt'];const action=sequence[Math.min(tick,sequence.length-1)];return {actor,action,target:action==='terminate'?'queue':'target'}}})}
function runAdapterRound(seed,order=['A','B'],profiles={}){if(order.length!==2||new Set(order).size!==2||!order.every(actor=>actor==='A'||actor==='B'))throw new TypeError('order_must_contain_one_A_and_one_B');const host=createSandbox(seed),adapters=order.map(actor=>scriptedAdapter(actor,profiles[actor]||'default'));for(let tick=0;tick<3;tick++){const decisions=adapters.map(adapter=>{const obs=host.observe(adapter.actor);const decision=adapter.decide(obs,tick);return !decision||decision.actor!==adapter.actor?{actor:adapter.actor,action:'identity_mismatch',target:'target'}:decision});host.applyBatch(decisions)}return {events:host.events(),observations:host.observations(),snapshot:host.snapshot()}}
const sandboxExports={ALLOWED,createSandbox,scriptedAdapter,runAdapterRound,validateDecision};
if(typeof globalThis!=='undefined')globalThis.CyberRumbleSandbox=sandboxExports;
if(typeof module!=='undefined')module.exports=sandboxExports;
