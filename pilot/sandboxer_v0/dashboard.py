"""Sanitized read-only operations projection and mobile dashboard WSGI app."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs

from .operations import OperationSnapshot


def project_operation(snapshot: OperationSnapshot, *, auditor_alerts: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    alerts=[{"code":str(item["code"])} for item in auditor_alerts if isinstance(item.get("code"),str)]
    runners={"total":len(snapshot.tracked_runners),"terminal":snapshot.series_state in {"COMPLETED","BLOCKED","QUARANTINED","CANCELLED"}}
    return {
        "schema":"sandboxer.dashboard.v1","series_id":snapshot.series_id,"mode":snapshot.mode.value,
        "series_state":snapshot.series_state,"phase":snapshot.series_state,"queue":{"position":snapshot.queue_position},
        "budgets":dict(snapshot.match_policy),
        "capacity":{"wait_attempts":snapshot.wait_attempts,"next_attempt_at":snapshot.next_attempt_at,"reason_code":snapshot.reason_code},
        "auditor_alerts":alerts,"cleanup":runners,
        "artifact":{"state":snapshot.publication_state,"label":snapshot.artifact_label},
    }


class ProjectionStore:
    def __init__(self,path:Path,*,published_root:Path)->None:
        self.path=path; self.published_root=published_root
    def write(self,value:Mapping[str,Any])->None:
        if value.get("schema")!="sandboxer.dashboard.v1": raise ValueError("invalid projection schema")
        self.path.parent.mkdir(parents=True,exist_ok=True); temporary=self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value,sort_keys=True,separators=(",",":")),encoding="utf-8"); os.chmod(temporary,0o600); temporary.replace(self.path)
    def read(self)->dict[str,Any]: return json.loads(self.path.read_text(encoding="utf-8"))
    def published(self,result_id:str)->dict[str,Any]|None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",result_id): return None
        path=self.published_root/f"{result_id}.json"
        if not path.is_file(): return None
        value=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value,dict): return None
        allowed={"schema","series_id","winner","score","summary","published_at"}
        return {key:value[key] for key in allowed if key in value}


class DashboardApp:
    def __init__(self,store:ProjectionStore,*,token_hash:str,audit_path:Path,rate_limit:int=60)->None:
        self.store=store; self.token_hash=token_hash; self.audit_path=audit_path; self.rate_limit=rate_limit; self._requests=defaultdict(deque)
    @staticmethod
    def hash_token(token:str)->str: return hashlib.sha256(("sandboxer-dashboard:"+token).encode()).hexdigest()
    def __call__(self,environ,start_response):
        method=environ.get("REQUEST_METHOD","GET"); path=environ.get("PATH_INFO","/"); remote=environ.get("REMOTE_ADDR","")
        if method not in {"GET","POST"} or method=="POST" and path!="/login": return self._reply(start_response,405,{"error":"METHOD_NOT_ALLOWED"},"private, no-store",remote,path)
        if not self._rate_allowed(remote): return self._reply(start_response,429,{"error":"RATE_LIMITED"},"private, no-store",remote,path)
        if path=="/login": return self._login(environ,start_response,remote,method)
        if path.startswith("/results/"):
            value=self.store.published(path.removeprefix("/results/"))
            return self._reply(start_response,200,value,"public, max-age=60",remote,path) if value is not None else self._reply(start_response,404,{"error":"NOT_FOUND"},"public, max-age=30",remote,path)
        if not self._authorized(environ.get("HTTP_AUTHORIZATION",""),environ.get("HTTP_COOKIE","")):
            return self._reply(start_response,401,{"error":"AUTH_REQUIRED"},"private, no-store",remote,path)
        if path=="/api/status": return self._reply(start_response,200,self.store.read(),"private, no-store",remote,path)
        if path=="/":
            value=self.store.read(); document=self._html(value).encode()
            return self._raw(start_response,200,document,"text/html; charset=utf-8","private, no-store",remote,path)
        return self._reply(start_response,404,{"error":"NOT_FOUND"},"private, no-store",remote,path)
    def _session_value(self)->str:
        return hmac.new(self.token_hash.encode(),b"sandboxer-browser-session",hashlib.sha256).hexdigest()
    def _authorized(self,header:str,cookie_header:str="")->bool:
        supplied=header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if supplied and hmac.compare_digest(self.hash_token(supplied),self.token_hash): return True
        cookie=SimpleCookie(); cookie.load(cookie_header); session=cookie.get("sandboxer_session")
        return session is not None and hmac.compare_digest(session.value,self._session_value())
    def _login(self,environ,start_response,remote:str,method:str):
        if method=="GET":
            document=b'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sandboxer operator access</title><style>body{margin:0;background:#050b18;color:#dce8ff;font:16px ui-monospace,monospace;display:grid;min-height:100vh;place-items:center}form{width:min(28rem,85vw);border:1px solid #164e9b;background:#07142a;padding:2rem}label,input,button{display:block;width:100%;box-sizing:border-box}input,button{margin-top:1rem;padding:.9rem;background:#050b18;color:#dce8ff;border:1px solid #2463ad}button{cursor:pointer}</style><form method="post" action="/login"><h1>Operator access</h1><label>Dashboard token<input name="token" type="password" required autocomplete="current-password"></label><button type="submit">Enter private dashboard</button></form>'''
            return self._raw(start_response,200,document,"text/html; charset=utf-8","private, no-store",remote,"/login")
        length=min(int(environ.get("CONTENT_LENGTH") or 0),4096); body=environ.get("wsgi.input").read(length); token=parse_qs(body.decode("utf-8",errors="replace")).get("token",[""])[0]
        if not hmac.compare_digest(self.hash_token(token),self.token_hash): return self._reply(start_response,401,{"error":"AUTH_REQUIRED"},"private, no-store",remote,"/login")
        cookie=f"sandboxer_session={self._session_value()}; Path=/; Secure; HttpOnly; SameSite=Strict"
        return self._raw(start_response,303,b"","text/plain","private, no-store",remote,"/login",(("Location","/"),("Set-Cookie",cookie)))
    def _rate_allowed(self,remote:str)->bool:
        now=time.monotonic(); queue=self._requests[remote]
        while queue and queue[0]<now-60: queue.popleft()
        if len(queue)>=self.rate_limit: return False
        queue.append(now); return True
    def _reply(self,start,status:int,value:Any,cache:str,remote:str,path:str):
        return self._raw(start,status,json.dumps(value,sort_keys=True,separators=(",",":")).encode(),"application/json",cache,remote,path)
    def _raw(self,start,status:int,body:bytes,content_type:str,cache:str,remote:str,path:str,extra_headers:tuple[tuple[str,str],...]=()):
        labels={200:"OK",303:"See Other",401:"Unauthorized",404:"Not Found",405:"Method Not Allowed",429:"Too Many Requests"}
        headers=[("Content-Type",content_type),("Content-Length",str(len(body))),("Cache-Control",cache),("X-Content-Type-Options","nosniff"),("X-Frame-Options","DENY"),("Referrer-Policy","no-referrer"),("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")]
        start(f"{status} {labels[status]}",headers+list(extra_headers))
        self.audit_path.parent.mkdir(parents=True,exist_ok=True)
        with self.audit_path.open("a",encoding="utf-8") as sink: sink.write(json.dumps({"at":int(time.time()),"remote_hash":hashlib.sha256(remote.encode()).hexdigest()[:16],"path":path[:160],"status":status},separators=(",",":"))+"\n")
        os.chmod(self.audit_path,0o600)
        return [body]
    @staticmethod
    def _html(value:Mapping[str,Any])->str:
        state=html.escape(str(value.get("series_state","UNKNOWN"))); series=html.escape(str(value.get("series_id","")))
        cards=(("SERIES",{"id":series,"state":state}),("PHASE",value.get("phase",{})),("QUEUE",value.get("queue",{})),("BUDGETS",value.get("budgets",{})),("CAPACITY",value.get("capacity",{})),("AUDITOR",value.get("auditor_alerts",[])),("CLEANUP",value.get("cleanup",{})),("ARTIFACT",value.get("artifact",{})))
        sections="".join(f'<section><small>{title}</small><pre>{html.escape(json.dumps(content,indent=2))}</pre></section>' for title,content in cards)
        return f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sandboxer operations</title><style>body{{margin:0;background:#050b18;color:#dce8ff;font:16px ui-monospace,monospace}}main{{max-width:64rem;margin:auto;padding:1rem}}section{{min-width:0;border:1px solid #164e9b;background:#07142a;padding:1rem;margin:.75rem 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}@media(min-width:700px){{main{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}h1{{grid-column:1/-1}}}}</style><main><h1>Sandboxer // operations</h1>{sections}</main>'''
