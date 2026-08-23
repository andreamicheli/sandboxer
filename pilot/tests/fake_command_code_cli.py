#!/usr/bin/env python3
import json, os, sys, time

mode = os.environ.get("SANDBOXER_FAKE_COMMAND_CODE", "success")
model = sys.argv[sys.argv.index("--model") + 1]
# Providers may echo a canonical casing different from the requested id.
reported_model = model.upper() if mode == "model_casing" else model
def emit(value): print(json.dumps(value), flush=True)
if mode == "timeout": time.sleep(5)
elif mode == "malformed": print("{")
elif mode in {"auth","credits","rate","server_error","mcp_disconnect"}:
    messages={"auth":"not authenticated: private account","credits":"credit balance exhausted","rate":"rate limit reached","server_error":"request failed after 8 retries: server error 500","mcp_disconnect":"mcp server disconnected"}
    emit({"type":"result","subtype":"error","error":messages[mode]})
    raise SystemExit(3)
else:
    emit({"type":"event","event":{"type":"model_request_start","model":reported_model}})
    if mode == "streaming": time.sleep(.15)
    if mode == "tool": emit({"type":"event","event":{"type":"tool_decision","tool":"shell_command","allowed":True,"reason_code":None}})
    if mode == "native_tool_queued": emit({"type":"event","event":{"type":"tool_queued","toolName":"read_directory","toolCallId":"native-1"}})
    if mode == "native_tool_denied": emit({"type":"event","event":{"type":"tool_denied","toolName":"read_directory","toolCallId":"native-1"}})
    if mode == "runner_tool": emit({"type":"event","event":{"type":"tool_running","toolName":"mcp__runner__read_note"}})
    if mode == "runner_tool_lifecycle":
        emit({"type":"event","event":{"type":"tool_start","toolName":"mcp__runner__read_note","toolCallId":"call-1"}})
        emit({"type":"event","event":{"type":"tool_input_delta","toolCallId":"call-1"}})
        emit({"type":"event","event":{"type":"tool_end","toolCallId":"call-1"}})
    emit({"type":"event","event":{"type":"turn_end","turnNumber":1,"hadToolCalls":mode in {"runner_tool","runner_tool_lifecycle"}}})
    if mode == "multi_turn":
        emit({"type":"event","event":{"type":"turn_start","turnNumber":2}})
        emit({"type":"event","event":{"type":"turn_end","turnNumber":2,"hadToolCalls":False}})
    emit({"type":"event","event":{"type":"model_request_end","model":reported_model}})
    emit({"type":"result","subtype":"success","stopReason":"end_turn","usage":{"inputTokens":10,"outputTokens":25 if mode == "overshoot" else 5,"cacheReadTokens":2,"cacheWriteTokens":0},"durationMs":20,"finalText":"ok"})
