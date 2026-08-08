const fs=require('fs');
const [leftPath,rightPath]=process.argv.slice(2);
if(!leftPath||!rightPath){console.error('usage: node compare-portable.js first.json second.json');process.exit(2)}
const left=JSON.parse(fs.readFileSync(leftPath,'utf8')),right=JSON.parse(fs.readFileSync(rightPath,'utf8'));
const comparable=['schema','benchmark_id','version','seed','digest','event_count','winners','score'];
const mismatches=comparable.filter(key=>JSON.stringify(left[key])!==JSON.stringify(right[key]));
const leftHashes=left.artifact_sha256||{},rightHashes=right.artifact_sha256||{},hashKeys=[...new Set([...Object.keys(leftHashes),...Object.keys(rightHashes)])].sort();
const hashMismatches=hashKeys.filter(key=>leftHashes[key]!==rightHashes[key]);
const result={schema:'cyber-rumble.portable-compare.v1',equivalent:mismatches.length===0&&hashMismatches.length===0,compared:comparable,field_mismatches:mismatches,artifact_hash_mismatches:hashMismatches,left_environment:left.environment||null,right_environment:right.environment||null};
console.log(JSON.stringify(result,null,2));
if(!result.equivalent)process.exitCode=1;
