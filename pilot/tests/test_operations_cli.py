from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from sandboxer_v0.operations_cli import main
from test_operations import _spec


def test_cli_submit_inspect_project_and_consequential_confirmation(tmp_path,capsys):
    state=tmp_path/"operations.json"; spec=tmp_path/"spec.json"; spec.write_text(json.dumps(asdict(_spec("cli-1"))))
    with pytest.raises(SystemExit,match="CONFIRMATION_REQUIRED"):
        main(["--state",str(state),"submit",str(spec),"--mode","batch-lab"])
    assert main(["--state",str(state),"submit",str(spec),"--mode","batch-lab","--yes"])==0
    assert main(["--state",str(state),"inspect","cli-1"])==0
    projection=tmp_path/"projection.json"
    assert main(["--state",str(state),"project","cli-1","--output",str(projection),"--published-root",str(tmp_path/"published")])==0
    assert json.loads(projection.read_text())["series_id"]=="cli-1"
