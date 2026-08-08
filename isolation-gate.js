const fs=require('fs'),cp=require('child_process'),os=require('os');
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
let probe={supported:false,reason:'not_run'};try{probe=require('./isolation-launcher.js').runProbe()}catch(error){probe={supported:false,reason:error.code||'probe_error'}}
function hasNodeFlag(flag){const result=cp.spawnSync(process.execPath,['--help'],{encoding:'utf8'});return (result.stdout||'').includes(flag)||(result.stderr||'').includes(flag)}
function capabilities(){return {platform:process.platform,node:process.version,seatbelt:process.platform==='darwin'&&fs.existsSync('/usr/bin/sandbox-exec'),node_permission:hasNodeFlag('--experimental-permission'),filesystem_default_deny:hasNodeFlag('--allow-fs-read'),child_default_deny:hasNodeFlag('--allow-child-process'),worker_default_deny:hasNodeFlag('--allow-worker'),network_default_deny:probe.supported,probe}}
function evaluate(){const runtime=capabilities(),required=['seatbelt','node_permission','filesystem_default_deny','child_default_deny','worker_default_deny','network_default_deny'],missing=required.filter(key=>!runtime[key]);if(manifest.status==='pre-model-safe')return {allowed:false,reason:'model_execution_disabled',runtime,missing};if(missing.length===0)return {allowed:true,reason:'enforcement_primitives_present',runtime,missing};return {allowed:false,reason:'enforcement_primitives_missing',runtime,missing}}
if(require.main===module)console.log(JSON.stringify({schema:'cyber-rumble.isolation-gate.v1',...evaluate()},null,2));
module.exports={capabilities,evaluate};
