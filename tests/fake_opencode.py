"""A stand-in for the `opencode` executable, used by the tests (no network, no LLM).

It speaks the same JSON event stream as `opencode run --format json` and acts out scenarios chosen by a marker in the
prompt, so the runner and the agent can be tested end to end. See FAKE_SOURCE: tests write it out as an executable file.
"""
FAKE_SOURCE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
if args[:1] == ["models"]:  # `opencode models <provider>`: the model list
    for m in ("google/gemini-2.5-flash", "google/primary", "google/fallback"):
        if m.startswith(args[1] + "/"):
            print(m)
    sys.exit(0)
prompt = args[-1]
cfg = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))

def emit(**ev):
    print(json.dumps(ev), flush=True)

def step(inp=100, out=10, cost=0.001, cr=0, cw=0, reasoning=0):
    emit(type="step_finish", part={"type": "step-finish", "reason": "tool-calls", "cost": cost,
         "tokens": {"total": inp + out, "input": inp, "output": out, "reasoning": reasoning, "cache": {"read": cr, "write": cw}}})

def tool(name, **inp):
    emit(type="tool_use", part={"type": "tool", "tool": name, "state": {"status": "completed", "input": inp, "output": "ok"}})

def say(text):
    emit(type="text", part={"type": "text", "text": text})

if "FAKE_STDIN" in prompt:
    open("stdin_len.txt", "w").write(str(len(sys.stdin.read())))  # blocks forever if stdin is left open
if "FAKE_DUMP" in prompt:
    remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    open("dump.json", "w").write(json.dumps({"env": sorted(os.environ), "remote": remote, "args": args, "config": cfg,
                                              "git_config": open(".git/config").read()}))
if "FAKE_PRIMARY_DOWN" in prompt and args[args.index("--model") + 1] == "google/primary":
    emit(type="error", error={"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}})
    sys.exit(1)
if "FAKE_DAILY" in prompt:  # Google's free-tier daily quota, as OpenCode reports it (no "PerDay" detail, unlike Aider)
    emit(type="error", error={"name": "APIError", "data": {"statusCode": 429, "message":
         "You exceeded your current quota, please check your plan and billing details.\n* Quota exceeded for metric: "
         "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-2.5-flash\n"
         "Please retry in 19h15m48.181751106s."}})
    sys.exit(1)
if "FAKE_RATELIMIT" in prompt:
    emit(type="error", error={"name": "APIError", "data": {"message": "Rate limit exceeded: tokens per minute", "statusCode": 429}})
    sys.exit(1)
if "FAKE_BADCONFIG" in prompt:
    sys.stderr.write("Error: Configuration is invalid at opencode.json\n")
    sys.exit(1)
if "FAKE_HANG" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    open("child.pid", "w").write(str(child.pid))
    time.sleep(300)
if "FAKE_MANY" in prompt:
    for _ in range(10):
        step()
        time.sleep(0.05)
    say("never reached")
    sys.exit(0)
if "FAKE_NOTHING" in prompt:
    step(inp=50, out=5)
    say("I looked at the code and nothing needs to change.")
    sys.exit(0)
if "FAKE_PARTIAL" in prompt:
    open("partial.txt", "w").write("half done\n")
    tool("write", filePath="partial.txt")
    step()
    emit(type="error", error={"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}})
    sys.exit(1)
if "FAKE_SNEAKY" in prompt:
    open("sneaky.txt", "w").write("work\n")
    subprocess.run(["git", "add", "-A"])
    subprocess.run(["git", "-c", "user.name=agent", "-c", "user.email=a@b.c", "commit", "-qm", "agent's own commit"])
    subprocess.run(["git", "checkout", "-q", "-b", "agent-branch"])
    os.makedirs(".git/hooks", exist_ok=True)
    open(".git/hooks/pre-commit", "w").write("#!/bin/sh\ntouch /tmp/HOOK_RAN\n")
    os.chmod(".git/hooks/pre-commit", 0o755)
    subprocess.run(["git", "config", "core.fsmonitor", "touch /tmp/FSMONITOR_RAN"])
    step()
    say("Committed it myself.")
    sys.exit(0)

# default: do some work
step(inp=1000, out=50, cost=0.002, cr=200)
open("hello.txt", "w").write("hello from the fake agent\n")
tool("write", filePath="hello.txt")
tool("bash", command="python -m pytest -q")
step(inp=1500, out=60, cost=0.003, cw=300, reasoning=40)
say("Created hello.txt as asked.")
step(inp=1600, out=20, cost=0.001)
'''
