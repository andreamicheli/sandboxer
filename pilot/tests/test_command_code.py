from __future__ import annotations
import asyncio, json, os, sys, time
from pathlib import Path
import pytest
from sandboxer_v0.command_code import CommandCodeAdapter, CommandCodeBudget, CommandCodeError, CommandCodePreflight

FAKE = Path(__file__).with_name("fake_command_code_cli.py")

def run(mode="success", *, budget=None):
    old=os.environ.get("SANDBOXER_FAKE_COMMAND_CODE"); os.environ["SANDBOXER_FAKE_COMMAND_CODE"]=mode
    try: return asyncio.run(CommandCodeAdapter((sys.executable,str(FAKE))).run(prompt="defend",model="example/model",max_turns=2,timeout_seconds=.2 if mode=="timeout" else 2,output_token_budget=10,budget=budget,runner_socket=Path("/tmp/fake-runner.sock"),allowed_tools=("read_note",)))
    finally:
        if old is None: os.environ.pop("SANDBOXER_FAKE_COMMAND_CODE",None)
        else: os.environ["SANDBOXER_FAKE_COMMAND_CODE"]=old

def test_command_code_normalizes_usage_and_identity():
    result=run(); assert result.requested_model==result.observed_model=="example/model"; assert result.output_tokens==5; assert result.turn_count==1

def test_command_code_process_contract_is_ephemeral_and_fail_closed():
    command=CommandCodeAdapter().command(prompt="x",model="example/model",max_turns=2)
    assert "--no-session" in command and "--no-skills" in command and "dont-ask" in command
    assert not {"--yolo","--trust","--auto-accept"}&set(command)


def test_command_code_supports_a_tool_free_interview_boundary():
    adapter = CommandCodeAdapter((sys.executable, str(FAKE)))
    result = asyncio.run(adapter.run(
        prompt="reflect", model="example/model", max_turns=1, timeout_seconds=2,
        output_token_budget=10, tool_free=True,
    ))
    assert result.observed_model == "example/model"

def test_command_code_writes_deny_by_default_runner_bridge_contract(tmp_path):
    root=tmp_path/"ephemeral"; root.mkdir()
    adapter=CommandCodeAdapter()
    adapter.prepare_workspace(root,runner_bridge=("/usr/bin/python3","-m","sandboxer_v0.command_code_bridge"),runner_socket=tmp_path/"runner.sock",allowed_tools=("read_note","submit_flag"))
    settings=json.loads((root/".commandcode/settings.json").read_text())
    mcp=json.loads((root/".mcp.json").read_text())
    assert settings["permissions"]["defaultMode"]=="dont-ask"
    assert settings["permissions"]["allow"]==["mcp__runner__read_note","mcp__runner__submit_flag"]
    assert {"shell_command","read_file","read_directory","write_file","edit_file","web_fetch"} <= set(settings["permissions"]["deny"])
    assert mcp["mcpServers"]["runner"]["env"]["SANDBOXER_RUNNER_SOCKET"]==str(tmp_path/"runner.sock")


def test_default_runner_bridge_is_an_absolute_executable_script(tmp_path, monkeypatch):
    captured = {}
    original = CommandCodeAdapter.prepare_workspace
    def record(root, **kwargs):
        captured.update(kwargs); original(root, **kwargs)
    monkeypatch.setattr(CommandCodeAdapter, "prepare_workspace", staticmethod(record))
    with pytest.raises(CommandCodeError, match="COMMAND_CODE_STREAM_EMPTY"):
        asyncio.run(CommandCodeAdapter(("/usr/bin/true",)).run(
            prompt="x", model="example/model", max_turns=1, timeout_seconds=2,
            output_token_budget=10, runner_socket=Path("/tmp/fake.sock"), allowed_tools=("inspect_service",),
        ))
    assert Path(captured["runner_bridge"][1]).is_absolute()
    assert Path(captured["runner_bridge"][1]).name == "command_code_bridge.py"

@pytest.mark.parametrize(("mode","reason"),[("malformed","COMMAND_CODE_NDJSON_MALFORMED"),("tool","COMMAND_CODE_NATIVE_TOOL_REJECTED"),("overshoot","COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"),("timeout","COMMAND_CODE_TIMEOUT"),("auth","COMMAND_CODE_AUTH_REQUIRED"),("credits","COMMAND_CODE_CREDITS_INSUFFICIENT"),("rate","COMMAND_CODE_CAPACITY_UNAVAILABLE")])
def test_command_code_failures_have_stable_codes(mode,reason):
    with pytest.raises(CommandCodeError,match=reason): run(mode)

def result_error(message):
    return CommandCodeAdapter._result([{"type":"result","subtype":"error","error":message}],3,"example/model",10)

@pytest.mark.parametrize("message",["provider overloaded","upstream unavailable","request timeout","request timed out","connection reset while reading response","network unreachable","gateway returned 5xx","bad gateway from upstream","service error at provider","server error 500","internal server error"])
def test_transport_failure_classifies_as_capacity_not_tool_boundary(message):
    with pytest.raises(CommandCodeError) as raised: result_error(message)
    assert raised.value.reason_code=="COMMAND_CODE_CAPACITY_UNAVAILABLE"
    assert raised.value.reason_code!="COMMAND_CODE_TOOL_BOUNDARY_FAILURE"

def test_mcp_disconnect_still_classifies_as_genuine_tool_boundary_failure():
    with pytest.raises(CommandCodeError) as raised: result_error("mcp server disconnected")
    assert raised.value.reason_code=="COMMAND_CODE_TOOL_BOUNDARY_FAILURE"

def test_preflight_rejects_catalog_drift_and_missing_exact_models():
    controls=("no_session","no_update","no_skills","skip_onboarding","dont_ask")
    preflight=CommandCodePreflight("v1","a"*64,"1.15.1","acct-redacted","b"*64,("deepseek/v4","xiaomi/mimo"),("inputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"),2,10,True,("deepseek/v4","xiaomi/mimo"),controls,("deepseek/v4","xiaomi/mimo"))
    preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash="b"*64)
    with pytest.raises(CommandCodeError,match="COMMAND_CODE_CATALOG_DRIFT"): preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash="c"*64)

def test_preflight_snapshot_redacts_account_and_binds_binary_catalog(tmp_path):
    binary=tmp_path/"cmd"; binary.write_bytes(b"pinned-cli")
    preflight=CommandCodePreflight.from_observations(binary=binary,cli_version="1.15.1",account_identifier="private@example.test",catalog=("xiaomi/mimo","deepseek/v4"),accounting_categories=("inputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"),concurrency_limit=2,credit_allowance=10,policy_compatible=True,entitled_models=("xiaomi/mimo","deepseek/v4"),session_controls=("no_session","no_update","no_skills","skip_onboarding","dont_ask"),live_probe_models=("xiaomi/mimo","deepseek/v4"))
    assert "private" not in preflight.account_fingerprint and len(preflight.snapshot_hash)==64
    preflight.require_pair(("deepseek/v4","xiaomi/mimo"),expected_catalog_hash=preflight.catalog_hash)

def test_named_matchup_stays_unschedulable_without_same_live_preflight():
    controls=("no_session","no_update","no_skills","skip_onboarding","dont_ask")
    preflight=CommandCodePreflight("v1","a"*64,"1.15.1","acct","b"*64,("deepseek/deepseek-v4-pro","xiaomi/mimo-v2.5-pro"),("inputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"),2,10,True,("deepseek/deepseek-v4-pro","xiaomi/mimo-v2.5-pro"),controls,())
    with pytest.raises(CommandCodeError,match="COMMAND_CODE_LIVE_PREFLIGHT_REQUIRED"):
        preflight.require_pair(("deepseek/deepseek-v4-pro","xiaomi/mimo-v2.5-pro"),expected_catalog_hash="b"*64)

def test_multi_turn_continuation_is_counted():
    assert run("multi_turn").turn_count==2

def test_provider_model_casing_is_not_treated_as_a_mismatch():
    # A provider that echoes a canonical casing (e.g. Qwen/Qwen3.7-Flash for
    # qwen/qwen3.7-flash) is the same model; only a genuinely different id
    # should fail the identity check.
    result = run("model_casing")
    assert result.requested_model == "example/model"
    assert result.observed_model == "EXAMPLE/MODEL"

def test_allowlisted_runner_tool_is_preserved_not_rejected():
    assert "tool_running" in run("runner_tool").event_types


def test_allowlisted_runner_tool_lifecycle_binds_nameless_frames_to_call_id():
    result = run("runner_tool_lifecycle")
    assert {"tool_start", "tool_input_delta", "tool_end"} <= set(result.event_types)


def test_native_tool_proposal_can_be_denied_before_execution():
    assert "tool_queued" in run("native_tool_queued").event_types
    assert "tool_denied" in run("native_tool_denied").event_types

def test_pair_execution_is_concurrent_and_identity_preserving():
    adapter=CommandCodeAdapter((sys.executable,str(FAKE)))
    async def pair():
        return await asyncio.gather(*[adapter.run(prompt="x",model=model,max_turns=2,timeout_seconds=1,output_token_budget=10,runner_socket=Path("/tmp/fake-runner.sock"),allowed_tools=("read_note",)) for model in ("deepseek/v4","xiaomi/mimo")])
    results=asyncio.run(pair())
    assert [item.observed_model for item in results]==["deepseek/v4","xiaomi/mimo"]

def test_output_budget_is_cumulative_across_continuations():
    budget=CommandCodeBudget(output_tokens=9)
    run("success", budget=budget)
    with pytest.raises(CommandCodeError,match="COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"):
        run("success", budget=budget)

def test_ndjson_frames_are_delivered_incrementally():
    adapter=CommandCodeAdapter((sys.executable,str(FAKE))); seen=[]
    old=os.environ.get("SANDBOXER_FAKE_COMMAND_CODE"); os.environ["SANDBOXER_FAKE_COMMAND_CODE"]="streaming"
    started=time.monotonic()
    try:
        asyncio.run(adapter.run(prompt="x",model="example/model",max_turns=2,timeout_seconds=1,output_token_budget=10,on_frame=lambda frame: seen.append((frame["type"],time.monotonic()-started)),runner_socket=Path("/tmp/fake-runner.sock"),allowed_tools=("read_note",)))
    finally:
        if old is None: os.environ.pop("SANDBOXER_FAKE_COMMAND_CODE",None)
        else: os.environ["SANDBOXER_FAKE_COMMAND_CODE"]=old
    assert seen[0][0]=="event" and seen[0][1] < .1
    assert seen[-1][0]=="result" and seen[-1][1] >= .1
