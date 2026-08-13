"""Authoritative operator CLI for durable Series workflows."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .dashboard import ProjectionStore, project_operation
from .operations import OperationControls, OperationMode, SeriesOperations, _decode_spec


def _confirm(action:str,yes:bool)->None:
    if yes: return
    if not sys.stdin.isatty() or input(f"Confirm {action} [type YES]: ")!="YES":
        raise SystemExit("CONFIRMATION_REQUIRED")


def main(argv:Sequence[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--state",type=Path,required=True)
    sub=parser.add_subparsers(dest="action",required=True)
    submit=sub.add_parser("submit"); submit.add_argument("spec",type=Path); submit.add_argument("--mode",choices=[item.value for item in OperationMode],required=True); submit.add_argument("--yes",action="store_true")
    inspect=sub.add_parser("inspect"); inspect.add_argument("series_id")
    for name in ("approve-spend","approve-publication","cancel"):
        command=sub.add_parser(name); command.add_argument("series_id"); command.add_argument("--revision",type=int,required=True); command.add_argument("--yes",action="store_true")
    reconcile=sub.add_parser("reconcile"); reconcile.add_argument("--now",type=float,default=None); reconcile.add_argument("--yes",action="store_true")
    projection=sub.add_parser("project"); projection.add_argument("series_id"); projection.add_argument("--output",type=Path,required=True); projection.add_argument("--published-root",type=Path,required=True)
    args=parser.parse_args(argv); operations=SeriesOperations(args.state)
    if args.action=="submit":
        _confirm("Series submission",args.yes); data=json.loads(args.spec.read_text(encoding="utf-8")); result=operations.submit(_decode_spec(data),mode=OperationMode(args.mode),controls=OperationControls())
    elif args.action=="inspect": result=operations.snapshot(args.series_id)
    elif args.action=="approve-spend": _confirm("spend approval",args.yes); result=operations.approve_spend(args.series_id,expected_revision=args.revision)
    elif args.action=="approve-publication": _confirm("publication",args.yes); result=operations.approve_publication(args.series_id,expected_revision=args.revision)
    elif args.action=="cancel": _confirm("Series cancellation",args.yes); result=operations.cancel(args.series_id,expected_revision=args.revision)
    elif args.action=="reconcile": _confirm("Runner reconciliation",args.yes); result=operations.reconcile(now=time.time() if args.now is None else args.now)
    else:
        result=operations.snapshot(args.series_id); ProjectionStore(args.output,published_root=args.published_root).write(project_operation(result))
    if isinstance(result,tuple): payload=[asdict(item) for item in result]
    else: payload=asdict(result)
    print(json.dumps(payload,sort_keys=True,default=str,separators=(",",":")))
    return 0


if __name__=="__main__": raise SystemExit(main())
