const fs=require('fs'),vm=require('vm');
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
const fixtures=JSON.parse(fs.readFileSync('fixtures.json','utf8'));
const golden=JSON.parse(fs.readFileSync('golden-replay.json','utf8'));
const differential=JSON.parse(fs.readFileSync('differential-report.json','utf8'));
const adapterContract=JSON.parse(fs.readFileSync('adapter-contract.json','utf8'));
const {buildMatch,scoreMatch,trace,winnersForSeed}=require('./engine.js');
const {ALLOWED,createSandbox,runAdapterRound,validateDecision}=require('./sandbox.js');
const {runCompetitiveMatch,runIsolatedCompetitiveMatch}=require('./competition.js');
const {evaluate:isolationGate}=require('./isolation-gate.js');
const {selfTest:isolatedAdapterTest}=require('./isolated-adapter-runner.js');
const first=buildMatch(manifest.scenario.seed), second=buildMatch(manifest.scenario.seed);
const adapterRun=runAdapterRound(manifest.scenario.seed,fixtures.fairness_orders[0]);
const swappedRun=runAdapterRound(manifest.scenario.seed,fixtures.fairness_orders[1]);
const actionSignature=run=>run.events.map(e=>`${e.action}:${e.legal}`).sort().join('|');
function summarize(run){const interrupts=run.events.filter(e=>e.action==='interrupt'&&e.legal);return {events:run.events.length,legal:run.events.filter(e=>e.legal).length,violations:run.snapshot.violations,round_losses:run.snapshot.roundLosses,interrupts:interrupts.length,first_interrupt_index:run.events.findIndex(e=>e.action==='interrupt'&&e.legal)}}
function normalizedObservations(run){return run.observations.map(({actor,...observation})=>JSON.stringify(observation)).sort().join('|')}
function legalityByActor(run){return ['A','B'].reduce((out,actor)=>{const decisions=run.events.filter(e=>e.actor===actor);out[actor]=decisions.length?decisions.filter(e=>e.legal).length/decisions.length:0;return out}, {})}
const score=scoreMatch(first,legalityByActor(adapterRun));
const exportedTrace=trace(first,score);
const competitive=runCompetitiveMatch(manifest.scenario.seed,[{A:'fast',B:'guarded'},{A:'guarded',B:'fast'},{A:'fast',B:'guarded'}]);
const isolatedCompetitive=runIsolatedCompetitiveMatch(manifest.scenario.seed,[{A:'fast',B:'guarded'},{A:'guarded',B:'fast'},{A:'fast',B:'guarded'}]);
const browserContext={CyberRumbleEngine:require('./engine.js'),CyberRumbleSandbox:require('./sandbox.js')};browserContext.globalThis=browserContext;vm.runInNewContext(fs.readFileSync('competition-browser.js','utf8'),browserContext);const browserCompetitive=browserContext.CyberRumbleCompetition.runCompetitiveMatch(manifest.scenario.seed,[{A:'fast',B:'guarded'},{A:'guarded',B:'fast'},{A:'fast',B:'guarded'}]);
const isolatedTest=isolatedAdapterTest();
const forfeitureHost=createSandbox(manifest.scenario.seed);forfeitureHost.applyDecision({actor:'A',action:'shell',target:'target'});const postLoss=forfeitureHost.applyDecision({actor:'A',action:'interrupt',target:'target'});
let duplicateOrderRejected=false;try{runAdapterRound(manifest.scenario.seed,['A','A'])}catch(error){duplicateOrderRejected=error.message==='order_must_contain_one_A_and_one_B'}
let invalidSeedRejected=false;try{createSandbox({seed:manifest.scenario.seed})}catch(error){invalidSeedRejected=error.message==='seed_must_be_safe_integer'}
const seedFixtures=[42771,42772,42773,42774].map(seed=>winnersForSeed(seed).join('/'));
const differentialOk=differential.seed_set.every(seed=>differential.profiles.every(profile=>{const run=runAdapterRound(seed,['A','B'],{A:profile,B:profile});const got=summarize(run),want=differential.expected_per_seed[profile];return JSON.stringify(got)===JSON.stringify(want)}));
const checks=[
  ['manifest id',manifest.benchmark_id==='cyber-rumble'],
  ['adapter contract disabled',adapterContract.schema==='cyber-rumble.adapter.v1'&&adapterContract.execution_status==='disabled_until_external_review'&&adapterContract.runner_policy.network==='deny'&&adapterContract.runner_policy.real_process_control==='deny'],
  ['three rounds',first.winners.length===manifest.scenario.rounds],
  ['24 ordered events',first.events.length===24 && first.events.every((e,i)=>e.id===i)],
  ['replay digest stable',first.digest===second.digest],
  ['seed changes deterministic fixture',new Set(seedFixtures).size===4&&seedFixtures[0]==='A/B/A'],
  ['golden replay matches',golden.seed===manifest.scenario.seed&&golden.digest===first.digest&&golden.event_count===first.events.length&&golden.winners.join('/')===first.winners.join('/')&&JSON.stringify(golden.score)===JSON.stringify({winner:score.winner,points:score.points,categories:score.categories})],
  ['score agrees with replay',score.winner==='A'&&score.round_wins.A===2&&score.round_wins.B===1&&score.points.A>score.points.B],
  ['trace schema agrees',exportedTrace.schema==='cyber-rumble.trace.v1'&&exportedTrace.digest===first.digest&&exportedTrace.event_count===first.events.length&&JSON.stringify(exportedTrace.score)===JSON.stringify(score)],
  ['synthetic policy-to-score path',competitive.replay.winners.join('/')==='A/B/A'&&competitive.score.winner==='A'&&competitive.score.round_wins.A===2&&competitive.score.round_wins.B===1&&competitive.replay.events.length>0&&competitive.replay.events.every(e=>e.state_hash&&e.replay_digest)],
  ['isolated policy-to-score path',JSON.stringify(isolatedCompetitive.replay)===JSON.stringify(competitive.replay)&&JSON.stringify(isolatedCompetitive.score)===JSON.stringify(competitive.score)],
  ['browser and host policy paths agree',JSON.stringify(browserCompetitive.replay)===JSON.stringify(competitive.replay)&&JSON.stringify(browserCompetitive.score)===JSON.stringify(competitive.score)],
  ['round winners A/B/A',first.winners.join('/')==='A/B/A'],
  ['sandbox denies real control',manifest.sandbox.denied_capabilities.includes('real_process_control')],
  ['no side effects',manifest.sandbox.side_effects===false],
  ['adapter uses bounded actions',adapterRun.events.every(e=>e.legal&&ALLOWED.includes(e.action))],
  ['adapter records zero violations',adapterRun.snapshot.violations===0],
  ['invalid decisions become round losses',fixtures.invalid_decisions.every(f=>{const host=createSandbox(manifest.scenario.seed),decision={actor:f.actor,action:f.action,target:f.target};if(Object.hasOwn(f,'extra'))decision.extra=f.extra;const event=host.applyDecision(decision),snapshot=host.snapshot();return event.legal===false&&event.reason===f.reason&&event.roundLoss===true&&snapshot.violations===1})],
  ['round loss prevents later mutation',postLoss.reason==='round_forfeited'&&postLoss.legal===false&&forfeitureHost.snapshot().target==='locked'&&forfeitureHost.snapshot().forfeited.A===true],
  ['adapter identity is unique and bound',duplicateOrderRejected],
  ['sandbox input types are bounded',invalidSeedRejected],
  ['isolation gate fails closed',isolationGate().allowed===false&&isolationGate().reason==='model_execution_disabled'],
  ['isolated adapter boundary enforces limits',isolatedTest.normal.ok===true&&isolatedTest.timeout.reason==='timeout_enforced'&&isolatedTest.output.reason==='output_limit_enforced'],
  ['required observability is emitted',adapterRun.observations.every(o=>typeof o.state_hash==='string'&&typeof o.replay_digest==='string')&&adapterRun.events.every(e=>typeof e.state_hash==='string'&&typeof e.replay_digest==='string')],
  ['fairness order symmetric',adapterRun.events.length===swappedRun.events.length&&actionSignature(adapterRun)===actionSignature(swappedRun)&&adapterRun.snapshot.violations===swappedRun.snapshot.violations],
  ['observations order-equivalent',normalizedObservations(adapterRun)===normalizedObservations(swappedRun)&&adapterRun.observations.length===swappedRun.observations.length],
  ['policy differential report matches',differentialOk]
];
for(const [name,ok] of checks){console.log(`${ok?'PASS':'FAIL'}  ${name}`);if(!ok)process.exitCode=1}
console.log(`DIGEST   ${first.digest}`);
console.log(`EVENTS   ${first.events.length}`);
console.log(`SCORE    A ${score.points.A} / B ${score.points.B}`);
