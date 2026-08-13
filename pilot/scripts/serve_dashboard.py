#!/usr/bin/env python3
"""Serve the private operator dashboard behind a loopback-only proxy."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server

from sandboxer_v0.dashboard import DashboardApp, ProjectionStore


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--projection",type=Path,required=True)
    parser.add_argument("--published-root",type=Path,required=True)
    parser.add_argument("--audit-path",type=Path,required=True)
    parser.add_argument("--token-file",type=Path,required=True)
    args=parser.parse_args()
    if args.host not in {"127.0.0.1","::1"}: raise SystemExit("dashboard must bind to loopback")
    token=args.token_file.read_text(encoding="utf-8").strip()
    if len(token)<32: raise SystemExit("dashboard token is missing or too short")
    app=DashboardApp(ProjectionStore(args.projection,published_root=args.published_root),token_hash=DashboardApp.hash_token(token),audit_path=args.audit_path)
    with make_server(args.host,args.port,app) as server:
        os.chmod(args.token_file,0o600)
        server.serve_forever()
    return 0


if __name__=="__main__": raise SystemExit(main())
