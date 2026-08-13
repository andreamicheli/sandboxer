from __future__ import annotations

import json
from pathlib import Path

from sandboxer_v0.dashboard import DashboardApp, ProjectionStore, project_operation
from sandboxer_v0.operations import OperationMode, SeriesOperations
from test_operations import _spec


def _call(app, path, *, token=None, remote="203.0.113.4"):
    status=[]; headers=[]
    environ={"PATH_INFO":path,"REQUEST_METHOD":"GET","REMOTE_ADDR":remote}
    if token: environ["HTTP_AUTHORIZATION"]=f"Bearer {token}"
    body=b"".join(app(environ,lambda value,items:(status.append(value),headers.extend(items))))
    return int(status[0].split()[0]),dict(headers),body


def test_projection_is_closed_and_excludes_restricted_fields(tmp_path):
    ops=SeriesOperations(tmp_path/"operations.json")
    snap=ops.submit(_spec("monitor-1"),mode=OperationMode.BATCH_LAB)
    projection=project_operation(snap,auditor_alerts=({"code":"SAFE_ALERT","prompt":"secret","raw":"no"},))
    encoded=json.dumps(projection)
    assert set(projection)=={"schema","series_id","mode","series_state","phase","queue","budgets","capacity","auditor_alerts","cleanup","artifact"}
    assert "secret" not in encoded and "prompt" not in encoded and "raw" not in encoded
    assert projection["queue"]["position"]==1


def test_dashboard_requires_auth_except_published_results_and_sets_safe_cache(tmp_path):
    store=ProjectionStore(tmp_path/"projection.json",published_root=tmp_path/"published")
    store.write({"schema":"sandboxer.dashboard.v1","series_id":"s","mode":"batch-lab","series_state":"QUEUED","phase":"QUEUED","budgets":{},"capacity":{},"auditor_alerts":[],"cleanup":{},"artifact":{}})
    published=tmp_path/"published"; published.mkdir(); (published/"public-1.json").write_text('{"winner":"model-a","prompt":"must-not-leak"}')
    app=DashboardApp(store,token_hash=DashboardApp.hash_token("correct"),audit_path=tmp_path/"audit.jsonl",rate_limit=2)
    assert _call(app,"/api/status")[0]==401
    status,headers,body=_call(app,"/api/status",token="correct")
    assert status==200 and headers["Cache-Control"]=="private, no-store" and b"QUEUED" in body
    status,headers,body=_call(app,"/results/public-1",remote="198.51.100.8")
    assert status==200 and headers["Cache-Control"]=="public, max-age=60" and b"must-not-leak" not in body
    assert _call(app,"/results/missing",remote="198.51.100.9")[0]==404

def test_mobile_html_renders_every_operational_panel(tmp_path):
    store=ProjectionStore(tmp_path/"projection.json",published_root=tmp_path/"published")
    store.write({"schema":"sandboxer.dashboard.v1","series_id":"s","series_state":"RUNNING","phase":"RED","queue":{},"budgets":{},"capacity":{},"auditor_alerts":[],"cleanup":{},"artifact":{}})
    app=DashboardApp(store,token_hash=DashboardApp.hash_token("correct"),audit_path=tmp_path/"audit",rate_limit=5)
    status,_headers,body=_call(app,"/",token="correct")
    assert status==200
    for heading in (b"QUEUE",b"BUDGETS",b"CAPACITY",b"AUDITOR",b"CLEANUP",b"ARTIFACT"): assert heading in body


def test_dashboard_is_read_only_rate_limited_and_audited(tmp_path):
    store=ProjectionStore(tmp_path/"projection.json",published_root=tmp_path/"published")
    store.write({"schema":"sandboxer.dashboard.v1"})
    app=DashboardApp(store,token_hash=DashboardApp.hash_token("correct"),audit_path=tmp_path/"audit.jsonl",rate_limit=1)
    assert _call(app,"/api/status",token="correct")[0]==200
    assert _call(app,"/api/status",token="correct")[0]==429
    environ={"PATH_INFO":"/api/status","REQUEST_METHOD":"POST","REMOTE_ADDR":"x"}; status=[]
    b"".join(app(environ,lambda value,_headers:status.append(value)))
    assert status[0].startswith("405")
    audit=(tmp_path/"audit.jsonl").read_text()
    assert "correct" not in audit and '"status":429' in audit
