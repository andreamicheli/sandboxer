const fs=require('fs');
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
const files=['index.html','progress.html','styles.css','app.js','engine.js','sandbox.js','critique.md',...manifest.artifacts];
const checks=[
  ['all declared artifacts exist',files.every(fs.existsSync)],
  ['landing explains MLPerf bar',/MLPERF \/ INFERENCE/.test(fs.readFileSync('index.html','utf8'))&&/MLPERF \/ SUBMISSION/.test(fs.readFileSync('index.html','utf8'))],
  ['landing explains broadcast bar',/ESPORTS \/ OBSERVER/.test(fs.readFileSync('index.html','utf8'))&&/BROADCAST \/ SCOREBUG/.test(fs.readFileSync('index.html','utf8'))],
  ['spectator controls present',/id="runBtn"/.test(fs.readFileSync('index.html','utf8'))&&/id="scrubber"/.test(fs.readFileSync('index.html','utf8'))&&/id="viewBtn"/.test(fs.readFileSync('index.html','utf8'))&&/id="exportBtn"/.test(fs.readFileSync('index.html','utf8'))],
  ['progress ledger present',/BLIND COMPARISON/.test(fs.readFileSync('progress.html','utf8'))&&/COMPLIANCE EVIDENCE/.test(fs.readFileSync('progress.html','utf8'))],
  ['coverage matrix present',/PROVEN LOCAL/.test(fs.readFileSync('coverage-matrix.md','utf8'))&&/OPEN EXTERNAL/.test(fs.readFileSync('coverage-matrix.md','utf8'))],
  ['safety declarations present',manifest.sandbox.side_effects===false&&manifest.sandbox.denied_capabilities.includes('real_process_control')],
  ['model execution disabled',JSON.parse(fs.readFileSync('adapter-contract.json','utf8')).execution_status==='disabled_until_external_review'],
  ['browser core has no network/process hooks',!/(fetch\s*\(|WebSocket|child_process|require\s*\()/.test(fs.readFileSync('app.js','utf8')+fs.readFileSync('engine.js','utf8')+fs.readFileSync('sandbox.js','utf8')+fs.readFileSync('competition-browser.js','utf8'))],
  ['spectator accessibility hooks present',/aria-live="polite"/.test(fs.readFileSync('index.html','utf8'))&&/aria-label="Replay frame"/.test(fs.readFileSync('index.html','utf8'))&&/:focus-visible/.test(fs.readFileSync('styles.css','utf8'))]
];
checks.push(['viewer CSP and safe source links present',/Content-Security-Policy/.test(fs.readFileSync('index.html','utf8'))&&/rel="noopener noreferrer"/.test(fs.readFileSync('index.html','utf8'))]);
const blind=JSON.parse(fs.readFileSync('blind-review.json','utf8'));
checks.push(['blind review names largest gap',blind.schema==='cyber-rumble.blind-review.v1'&&blind.verdict.largest_gap==='independent_isolation_review']);
checks.push(['isolation gate fails closed',/model_execution_disabled/.test(require('child_process').execFileSync(process.execPath,['isolation-gate.js'],{encoding:'utf8'}))]);
checks.push(['isolated runner is declared',manifest.artifacts.includes('isolated-adapter-runner.js')&&manifest.artifacts.includes('adapter-worker.js')]);
for(const [name,ok] of checks){console.log(`${ok?'PASS':'FAIL'}  ${name}`);if(!ok)process.exitCode=1}
console.log(`ARTIFACTS ${files.length}`);
