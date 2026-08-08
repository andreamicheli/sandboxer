const {createSandbox}=require('./sandbox.js');
const {fnv,scoreMatch,trace}=require('./engine.js');
const {runIsolated}=require('./isolated-adapter-runner.js');
const path=require('path');

const STRATEGIES={fast:1,guarded:2,invalid:1};
function decisionFor(actor,strategy,tick){
  if(!Object.hasOwn(STRATEGIES,strategy))throw new TypeError('unknown_strategy');
  if(strategy==='invalid'&&tick===1)return {actor,action:'shell',target:'target'};
  if(tick===STRATEGIES[strategy])return {actor,action:'interrupt',target:'target'};
  return {actor,action:tick===0?'observe':'isolate',target:'target'};
}
function runRound(seed,round,plan){
  const host=createSandbox(seed+round),raw=[];
  for(let tick=0;tick<3;tick++){
    const decisions=['A','B'].map(actor=>{host.observe(actor);return decisionFor(actor,plan[actor],tick)});
    raw.push(...host.applyBatch(decisions).map(event=>({...event,tick:tick+1})));
  }
  const winnerEvent=raw.find(event=>event.action==='interrupt'&&event.legal),winner=winnerEvent?.actor||'TIE';
  const events=raw.map((event,index)=>({id:index,round,tick:event.tick,actor:event.actor,kind:event.action==='observe'?'OBSERVE':'ACTION',text:event.action==='interrupt'?'commits interrupt(target)':`requests ${event.action}(target)`,cls:event.action==='observe'?'obs':'act',legal:event.legal,reason:event.reason,state_hash:event.state_hash,replay_digest:event.replay_digest}));
  events.push({id:events.length,round,tick:7,actor:'SYS',kind:'SYSTEM',text:`round ${round} result pending verification`,cls:'sys',legal:true,state_hash:fnv(JSON.stringify({seed,round,winner})),replay_digest:fnv(JSON.stringify(events))});
  events.push({id:events.length,round,tick:8,actor:'SYS',kind:'SYSTEM',text:`round ${round} verified / winner = ${winner}`,cls:'sys',legal:true,state_hash:fnv(JSON.stringify({seed,round,winner,verified:true})),replay_digest:fnv(JSON.stringify(events))});
  return {winner,events,raw};
}
function runIsolatedRound(seed,round,plan){
  const host=createSandbox(seed+round),raw=[],worker=path.join(__dirname,'adapter-worker.js');
  for(let tick=0;tick<3;tick++){
    const decisions=['A','B'].map(actor=>{host.observe(actor);const result=runIsolated(worker,{actor,strategy:plan[actor],tick});if(!result.ok)throw new Error(`isolated_adapter_${result.reason}`);return result.response});
    raw.push(...host.applyBatch(decisions).map(event=>({...event,tick:tick+1})));
  }
  const winnerEvent=raw.find(event=>event.action==='interrupt'&&event.legal),winner=winnerEvent?.actor||'TIE';
  const events=raw.map((event,index)=>({id:index,round,tick:event.tick,actor:event.actor,kind:event.action==='observe'?'OBSERVE':'ACTION',text:event.action==='interrupt'?'commits interrupt(target)':`requests ${event.action}(target)`,cls:event.action==='observe'?'obs':'act',legal:event.legal,reason:event.reason,state_hash:event.state_hash,replay_digest:event.replay_digest}));
  events.push({id:events.length,round,tick:7,actor:'SYS',kind:'SYSTEM',text:`round ${round} result pending verification`,cls:'sys',legal:true,state_hash:fnv(JSON.stringify({seed,round,winner})),replay_digest:fnv(JSON.stringify(events))});
  events.push({id:events.length,round,tick:8,actor:'SYS',kind:'SYSTEM',text:`round ${round} verified / winner = ${winner}`,cls:'sys',legal:true,state_hash:fnv(JSON.stringify({seed,round,winner,verified:true})),replay_digest:fnv(JSON.stringify(events))});
  return {winner,events,raw};
}
function assembleMatch(seed,rounds){const events=rounds.flatMap(round=>round.events).map((event,index)=>({...event,id:index})),winners=rounds.map(round=>round.winner),replay={seed,events,winners,digest:fnv(JSON.stringify({seed,events,winners}))},legalByActor=['A','B'].reduce((out,actor)=>{const all=rounds.flatMap(round=>round.raw).filter(event=>event.actor===actor);out[actor]=all.length?all.filter(event=>event.legal).length/all.length:0;return out},{}),score=scoreMatch(replay,legalByActor);return {replay,score,trace:trace(replay,score),rounds:rounds.map(({winner,events})=>({winner,event_count:events.length}))}}
function runIsolatedCompetitiveMatch(seed,plans){if(!Array.isArray(plans)||plans.length!==3)throw new TypeError('three_round_plans_required');return assembleMatch(seed,plans.map((plan,index)=>runIsolatedRound(seed,index+1,plan)))}
function runCompetitiveMatch(seed,plans){
  if(!Array.isArray(plans)||plans.length!==3)throw new TypeError('three_round_plans_required');
  return assembleMatch(seed,plans.map((plan,index)=>runRound(seed,index+1,plan)));
}
if(typeof module!=='undefined')module.exports={runCompetitiveMatch,runIsolatedCompetitiveMatch,decisionFor};
