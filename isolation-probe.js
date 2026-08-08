const fs=require('fs'),cp=require('child_process'),net=require('net');
const result={fs_write_denied:false,child_denied:false,network_denied:false};
try{fs.writeFileSync('/tmp/cyber-rumble-isolation-probe','x')}catch(error){result.fs_write_denied=error.code==='ERR_ACCESS_DENIED'||error.code==='EPERM'}
try{cp.execFileSync(process.execPath,['-e','process.stdout.write(\"child\")'],{stdio:'ignore'})}catch(error){result.child_denied=error.code==='ERR_ACCESS_DENIED'||error.code==='EPERM'||error.status!==0}
const socket=net.createConnection({host:'127.0.0.1',port:9});socket.on('error',error=>{result.network_denied=error.code==='EPERM'||error.code==='EACCES';console.log(JSON.stringify(result));});socket.setTimeout(150,()=>{socket.destroy();console.log(JSON.stringify(result))});
