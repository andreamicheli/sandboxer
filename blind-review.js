const fs=require('fs');
const cp=require('child_process');

// This runner deliberately labels the local artifact only as CANDIDATE until
// all evidence is collected. It is an independent process, not a builder
// summary, and its report keeps the largest unresolved gap explicit.
const html=fs.readFileSync('index.html','utf8');
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
const contract=JSON.parse(fs.readFileSync('adapter-contract.json','utf8'));
const run=cp.spawnSync(process.execPath,['verify.js'],{encoding:'utf8'});
const output=(run.stdout||'')+(run.stderr||'');
const pass=(text)=>output.includes(`PASS  ${text}`);
const criteria=[
  {id:'reproducibility',reference:'fixed scenario, declared environment, checker-verifiable result, repeatable replay',candidate:pass('replay digest stable')&&pass('golden replay matches')&&pass('trace schema agrees')},
  {id:'fairness',reference:'same observation boundary, validated actions, symmetric ordering, invalid behavior penalized',candidate:pass('fairness order symmetric')&&pass('observations order-equivalent')&&pass('invalid decisions become round losses')},
  {id:'safety',reference:'bounded execution with no uncontrolled side effects',candidate:pass('sandbox denies real control')&&pass('no side effects')&&contract.execution_status==='disabled_until_external_review'},
  {id:'spectator_legibility',reference:'score, clock, phase, event feed, replay controls, and observer focus',candidate:/id="runBtn"/.test(html)&&/id="scrubber"/.test(html)&&/id="viewBtn"/.test(html)&&/id="exportBtn"/.test(html)&&/id="callout"/.test(html)},
  {id:'narrative',reference:'the match is understandable as a sequence of opening read, counterplay, and decisive window',candidate:/OPENING READ/.test(html)&&/COUNTERPLAY/.test(fs.readFileSync('app.js','utf8'))&&/INTERRUPT WINDOW/.test(fs.readFileSync('app.js','utf8'))},
  {id:'end_to_end_policy_path',reference:'submitted bounded decisions determine simulated winners, timing, and score before real models are enabled',candidate:pass('synthetic policy-to-score path')},
  {id:'production_adapter_enforcement',reference:'future adapters run behind enforced worker, timeout, capability, identity, and output limits',candidate:pass('isolated adapter boundary enforces limits')&&pass('isolated policy-to-score path')},
  {id:'independent_isolation_review',reference:'a second OS/runtime reproduces capability denial and isolated-worker behavior before enablement',candidate:false},
  {id:'external_reviewability',reference:'an independent environment can reproduce the package and inspect the rendered viewer',candidate:false}
];
const gaps=criteria.filter(c=>!c.candidate).map(c=>c.id);
const report={schema:'cyber-rumble.blind-review.v1',review_mode:'fresh_process_candidate_vs_reference_bar',labels:{candidate:'CANDIDATE',reference:'REFERENCE_BAR'},candidate_process:{command:'node verify.js',exit_code:run.status},criteria,verdict:{candidate_wins_on:criteria.filter(c=>c.candidate).map(c=>c.id),largest_gap:gaps[0]||null,unresolved_gaps:gaps,publication_status:gaps.length?'not_perfect':'candidate_meets_declared_local_bar'},sources:[
  'https://docs.mlcommons.org/inference/',
  'https://docs.mlcommons.org/inference/submission/',
  'https://news.blizzard.com/en-us/article/23013835/overwatch-league-replay-viewer-see-matches-from-a-new-perspective',
  'https://scorebug.tv/'
]};
fs.writeFileSync('blind-review.json',JSON.stringify(report,null,2)+'\n');
console.log(`BLIND REVIEW ${report.verdict.publication_status}`);
console.log(`CANDIDATE WINS ${report.verdict.candidate_wins_on.join(', ')}`);
console.log(`LARGEST GAP ${report.verdict.largest_gap||'none'}`);
if(run.status!==0||gaps.length===0)process.exitCode=run.status||0;
