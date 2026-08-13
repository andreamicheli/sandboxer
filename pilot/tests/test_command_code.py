from __future__ import annotations
import asyncio, os, sys
from pathlib import Path
import pytest
from sandboxer_v0.command_code import CommandCodeAdapter, CommandCodeError, CommandCodePreflight

FAKE = Path(__file__).with_name("fake_command_code_cli.py")

def run(mode="success"):
    old=os.environ.get("SANDBOXER_FAKE_COMMAND_CODE"); os.environ["SANDBOXER_FAKE_COMMAND_CODE"]=mode
    try: return asyncio.run(CommandCodeAdapter((sys.executable,str(FAKE))).run(prompt="defend",model="example/model",max_turns=2,timeout_seconds=.2,output_token_budget=10))
    finally:
        if old is None: os.environ.pop("SANDBOXER_FAKE_COMMAND_CODE",None)
        else: os.environ["SANDBOXER_FAKE_COMMAND_CODE"]=old

def test_command_code_normalizes_usage_and_identity():
    result=run(); assert result.requested_model==result.observed_model=="example/model"; assert result.output_tokens==5; assert result.turn_count==1

def test_command_code_process_contract_is_ephemeral_and_fail_closed():
    command=CommandCodeAdapter().command(prompt="x",model="example/model",max_turns=2)
    assert "--no-session" in command and "--no-skills" in command and "plan" in command
    assert not {"--yolo","--trust","--auto-accept"}&set(command)

@pytest.mark.parametrize(("mode","reason"),[("malformed","COMMAND_CODE_NDJSON_MALFORMED"),("tool","COMMAND_CODE_NATIVE_TOOL_REJECTED"),("overshoot","COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"),("timeout","COMMAND_CODE_TIMEOUT"),("auth","COMMAND_CODE_AUTH_REQUIRED"),("credits","COMMAND_CODE_CREDITS_INSUFFICIENT"),("rate","COMMAND_CODE_CAPACITY_UNAVAILABLE")])
def test_command_code_failures_have_stable_codes(mode,reason):
    with pytest.raises(CommandCodeError,match=reason): run(mode)

def test_preflight_rejects_catalog_drift_and_missing_exact_models():
    preflight=CommandCodePreflight("v1","a"*64,"1.15.1","acct-redacted","b"*64,("deepseek/v4","xiaomi/mimo"),("inputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"),2,10,True)
    preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash="b"*64)
    with pytest.raises(CommandCodeError,match="COMMAND_CODE_CATALOG_DRIFT"): preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash="c"*64)

def test_preflight_snapshot_redacts_account_and_binds_binary_catalog(tmp_path):
    binary=tmp_path/"cmd"; binary.write_bytes(b"pinned-cli")
    preflight=CommandCodePreflight.from_observations(binary=binary,cli_version="1.15.1",account_identifier="private@example.test",catalog=("xiaomi/mimo","deepseek/v4"),accounting_categories=("inputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"),concurrency_limit=2,credit_allowance=10,policy_compatible=True)
    assert "private" not in preflight.account_fingerprint and len(preflight.snapshot_hash)==64
    preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash=preflight.catalog_hash)

def test_multi_turn_continuation_is_counted():
    assert run("multi_turn").turn_count==2

def test_pair_execution_is_concurrent_and_identity_preserving():
    adapter=CommandCodeAdapter((sys.executable,str(FAKE)))
    async def pair():
        return await asyncio.gather(*[adapter.run(prompt="x",model=model,max_turns=2,timeout_seconds=1,output_token_budget=10) for model in ("deepseek/v4","xiaomi/mimo")])
    results=asyncio.run(pair())
    assert [item.observed_model for item in results]==["deepseek/v4","xiaomi/mimo"]
