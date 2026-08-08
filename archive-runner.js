const fs=require('fs'),crypto=require('crypto'),{spawnSync}=require('child_process');
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
const required=['manifest.json','engine.js','sandbox.js',...manifest.artifacts];
const missing=required.filter(file=>!fs.existsSync(file));
if(missing.length){console.error('ARCHIVE FAIL missing:',missing.join(', '));process.exit(1)}
console.log(`ARCHIVE ${manifest.benchmark_id} ${manifest.version}`);
for(const file of required){const digest=crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');console.log(`${digest}  ${file}`)}
const result=spawnSync(process.execPath,['verify.js'],{stdio:'inherit'});
if(result.error)throw result.error;
process.exit(result.status||0);
