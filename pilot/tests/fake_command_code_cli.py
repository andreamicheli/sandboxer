#!/usr/bin/env python3
import json, os, sys, time

mode = os.environ.get("SANDBOXER_FAKE_COMMAND_CODE", "success")
model = sys.argv[sys.argv.index("--model") + 1]
def emit(value): print(json.dumps(value), flush=True)
if mode == "timeout": time.sleep(5)
elif mode == "malformed": print("{")
elif mode in {"auth","credits","rate"}:
    messages={"auth":"not authenticated: private account","credits":"credit balance exhausted","rate":"rate limit reached"}
    emit({"type":"result","subtype":"error","error":messages[mode]})
    raise SystemExit(3)
else:
    emit({"type":"event","event":{"type":"model_request_start","model":model}})
    if mode == "streaming": time.sleep(.15)
    if mode == "tool": emit({"type":"event","event":{"type":"tool_running","toolName":"shell_command"}})
    if mode == "runner_tool": emit({"type":"event","event":{"type":"tool_running","toolName":"mcp__runner__read_note"}})
    emit({"type":"event","event":{"type":"turn_end","turnNumber":1,"hadToolCalls":mode=="runner_tool"}})
    if mode == "multi_turn":
        emit({"type":"event","event":{"type":"turn_start","turnNumber":2}})
        emit({"type":"event","event":{"type":"turn_end","turnNumber":2,"hadToolCalls":False}})
    emit({"type":"event","event":{"type":"model_request_end","model":model}})
    emit({"type":"result","subtype":"success","stopReason":"end_turn","usage":{"inputTokens":10,"outputTokens":25 if mode == "overshoot" else 5,"cacheReadTokens":2,"cacheWriteTokens":0},"durationMs":20,"finalText":"ok"})
