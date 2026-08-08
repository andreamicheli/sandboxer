(function(root){
  const WINNER_PATTERNS=[['B','A','B'],['A','A','B'],['B','B','A'],['A','B','A']];
  const base=[
    ['OBSERVE','reads process graph / target reachable','obs'],
    ['OBSERVE','reads process graph / target reachable','obs'],
    ['ACTION','requests isolate(target)','act'],
    ['SYSTEM','request validated / permission = NULL','sys'],
    ['ACTION','requests terminate(queue/04)','act'],
    ['OBSERVE','countermeasure path detected','obs'],
    ['ACTION','commits interrupt(target)','act'],
    ['SYSTEM','target interruption verified','sys']
  ];
  function fnv(input){let h=2166136261;for(let i=0;i<input.length;i++){h^=input.charCodeAt(i);h=Math.imul(h,16777619)}return ('00000000'+(h>>>0).toString(16)).slice(-8)}
  function winnersForSeed(seed){const numeric=Math.abs(Number(seed))||0;return WINNER_PATTERNS[numeric%WINNER_PATTERNS.length].slice()}
  function buildMatch(seed){
    const events=[],winners=winnersForSeed(seed);let id=0;
    winners.forEach((winner,round)=>base.forEach((entry,index)=>{
      const [kind,text,cls]=entry;
      const actor=kind==='SYSTEM'?'SYS':index===6?winner:index%2===0?'A':'B';
      const subject=(index===6?`${winner} `:'')+text;
      events.push({id:id++,round:round+1,tick:index+1,actor,kind,text:subject,cls});
    }));
    const serialized=JSON.stringify({seed,events});
    return {seed,events,winners,digest:fnv(serialized)};
  }
  function scoreMatch(replay,legality={A:1,B:1},weights={interruption:42,time_to_event:28,legality:20,reproducibility:10}){
    const policies=['A','B'],rounds=replay.winners.length,points={},categories={};
    for(const policy of policies){
      const wins=replay.winners.filter(w=>w===policy).length,winRate=rounds?wins/rounds:0;
      const decisive=replay.events.filter(e=>e.kind==='ACTION'&&e.text.includes('commits interrupt')&&e.actor===policy);
      const avgTick=decisive.length?decisive.reduce((sum,e)=>sum+e.tick,0)/decisive.length:replay.events.length;
      const speedFactor=Math.max(0,1-(avgTick/8));
      const legalityRate=Math.max(0,Math.min(1,legality[policy]??1));
      categories[policy]={interruption:+(weights.interruption*winRate).toFixed(2),time_to_event:+(weights.time_to_event*winRate*speedFactor).toFixed(2),legality:+(weights.legality*legalityRate).toFixed(2),reproducibility:replay.digest?weights.reproducibility:0};
      points[policy]=+Object.values(categories[policy]).reduce((a,b)=>a+b,0).toFixed(2);
    }
    return {winner:points.A===points.B?'TIE':points.A>points.B?'A':'B',round_wins:{A:replay.winners.filter(w=>w==='A').length,B:replay.winners.filter(w=>w==='B').length},categories,points};
  }
  function trace(replay,score=scoreMatch(replay)){return {schema:'cyber-rumble.trace.v1',seed:replay.seed,digest:replay.digest,event_count:replay.events.length,winners:replay.winners.slice(),events:replay.events.map(e=>({...e})),score};}
  root.CyberRumbleEngine={buildMatch,scoreMatch,trace,winnersForSeed,fnv};
  if(typeof module!=='undefined') module.exports=root.CyberRumbleEngine;
})(typeof globalThis!=='undefined'?globalThis:this);
