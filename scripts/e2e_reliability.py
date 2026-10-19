"""End-to-end run of the reliability features with the real app (Docker image), real Jira, Slack, GitHub and Gemini.

    docker build -t jira-agent .
    python scripts/e2e_reliability.py            # everything; or only some parts: P1 (P1-P4), P5, P6

P1 Slack unreachable while the first plan is made: the plan is kept · P2 Slack back: the plan arrives "Delayed" and works
P3 the ticket asks for a README change, the owner takes it out of the plan · P4 approve; meanwhile a message to a second
ticket says it waits for the coding run; the PR contains only the planned files · P5 OpenCode hangs (its model can't be
reached), TOOL_FALLBACK off: stopped as stuck, run once more by itself, then reported · P6 the same hang with
TOOL_FALLBACK on: Aider writes the plan; then OpenCode with a broken config while coding: Aider writes the code.

Like scripts/e2e_faults.py: its own port (8001), Docker network, Redis and Jira label (`ai-agent-e2e`), so the live app
and its Redis are not touched; "your" Slack messages and clicks are signed requests sent straight to the test app.
Creates real Jira tickets and PRs (listed at the end, for cleaning up). Checkpoints in e2e-reliability-<time>.jsonl.
"""
import hashlib
import hmac
import sys
import json
import re
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import redis
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
OUT = Path(f"e2e-reliability-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
E = dotenv_values(REPO / ".env")
PORT, LABEL = 8001, "ai-agent-e2e"
URL = f"http://localhost:{PORT}"
NET, REDIS_NAME, APP_NAME, REDIS_PORT = "jira-agent-e2e", "jira-agent-e2e-redis", "jira-agent-e2e", 6390
JIRA = httpx.Client(base_url=E["JIRA_BASE_URL"], auth=(E["JIRA_EMAIL"], E["JIRA_API_TOKEN"]), timeout=30)
GH = httpx.Client(base_url=f"https://api.github.com/repos/{E['GITHUB_REPO']}", timeout=30,
                  headers={"Authorization": f"Bearer {E['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
SLACK = httpx.Client(base_url="https://slack.com/api", headers={"Authorization": f"Bearer {E['SLACK_BOT_TOKEN']}"}, timeout=30)
DM = SLACK.post("/conversations.open", data={"users": E["SLACK_USER_ID"]}).json()["channel"]["id"]
T0 = time.time()
BASE = {
    "TRIGGER_LABEL": LABEL, "REDIS_URL": f"redis://{REDIS_NAME}:6379", "LEASE_SECONDS": "30", "WATCHDOG_SECONDS": "15",
    "PLANNER_BACKEND": "opencode", "CODING_BACKEND": "opencode", "PORT": "8000",
    "SEPARATE_PLAN_QUEUE": "false", "PLAN_WORKERS": "1",  # like the 1 GB server: one job at a time
    "AIDER_MODEL": "gemini/gemini-3.5-flash-lite", "PLANNER_MODEL": "", "OPENCODE_MODEL": "google/gemini-3.5-flash-lite",
    "OPENCODE_PLANNER_MODEL": "", "PLANNER_FALLBACK_MODEL": "gemini/gemini-flash-lite-latest",
    "AIDER_FALLBACK_MODEL": "gemini/gemini-flash-lite-latest", "OPENCODE_FALLBACK_MODEL": "google/gemini-flash-lite-latest",
    "SELF_PING_URL": "", "TOOL_FALLBACK": "true", "JOB_STUCK_SECONDS": "420", "OPENCODE_EXTRA_CONFIG": "",
}
# OpenCode's Google provider pointed at an address that never answers: the run hangs without a sign of life
HANG = json.dumps({"provider": {"google": {"options": {"baseURL": "http://10.255.255.1/v1beta"}}}})
BROKEN = json.dumps({"provider": "not-an-object"})  # OpenCode refuses to start (like a crash: no model request)
results: list[tuple[str, bool, str]] = []
ONLY = set(sys.argv[1:])  # e.g. `P5` (P1 runs P1-P4, which build on each other); none = everything


def want(part: str) -> bool:
    return not ONLY or part in ONLY

created: list[str] = []


def log(stage, msg, **data):
    line = {"t": round(time.time() - T0, 1), "stage": stage, "msg": msg, **data}
    with OUT.open("a") as f:
        f.write(json.dumps(line, default=str) + "\n")
    print(f"[{line['t']:7.1f}s] {stage:3s} {msg} {json.dumps(data, default=str)[:400] if data else ''}", flush=True)


def check(stage, name, ok, detail=""):
    results.append((f"{stage} {name}", bool(ok), str(detail)[:300]))
    log(stage, ("PASS " if ok else "FAIL ") + name, detail=str(detail)[:300])


def docker(*a, check=False):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check)


def infra():
    docker("network", "create", NET)
    docker("rm", "-f", REDIS_NAME)
    docker("run", "-d", "--name", REDIS_NAME, "--network", NET, "-p", f"127.0.0.1:{REDIS_PORT}:6379", "redis:7-alpine", check=True)
    for _ in range(30):
        try:
            r = redis.Redis(port=REDIS_PORT, decode_responses=True)
            r.ping()
            return r
        except redis.ConnectionError:
            time.sleep(0.5)
    raise SystemExit("test Redis did not start")


def app(overrides=None, slack_down=False):
    docker("rm", "-f", APP_NAME)
    cmd = ["run", "-d", "--name", APP_NAME, "--network", NET, "-p", f"{PORT}:8000", "--env-file", str(REPO / ".env")]
    if slack_down:  # Slack's address leads nowhere: connections are refused (an outage, not an answer from Slack)
        cmd += ["--add-host", "slack.com:127.0.0.1"]
    for k, v in {**BASE, **(overrides or {})}.items():
        cmd += ["-e", f"{k}={v}"]
    docker(*cmd, "jira-agent", check=True)
    for _ in range(90):
        try:
            return httpx.get(f"{URL}/health", timeout=3).json()
        except Exception:
            time.sleep(1)
    raise SystemExit("test app did not start")


def session(key):
    raw = R.get(f"jiraagent:session:{key}")
    return json.loads(raw) if raw else None


def wait(key, pred, what, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        s = session(key)
        if s and pred(s):
            return s
        time.sleep(3)
    log("ERR", f"timed out waiting for {what} ({key})", session=session(key), app_log=app_log())
    return None


def sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(E["SLACK_SIGNING_SECRET"].encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def say(key, text):
    body = json.dumps({"type": "event_callback", "event_id": "Ev" + uuid.uuid4().hex[:10], "event": {
        "type": "message", "channel_type": "im", "channel": DM, "user": E["SLACK_USER_ID"], "text": f"{key}: {text}",
        "ts": f"{time.time():.6f}"}}).encode()
    httpx.post(f"{URL}/slack/events", content=body, headers={"Content-Type": "application/json", **sign(body)}, timeout=20)
    log("you", f"{key}: {text}")


def click(key, action, message_ts):
    payload = {"type": "block_actions", "user": {"id": E["SLACK_USER_ID"]}, "channel": {"id": DM},
               "message": {"ts": message_ts}, "actions": [{"action_id": f"{action}:{key}", "value": key}]}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    httpx.post(f"{URL}/slack/interactive", content=body, timeout=20,
               headers={"Content-Type": "application/x-www-form-urlencoded", **sign(body)})
    log("you", f"clicked {action} on {key}")


def approve(key):
    click(key, "plan_proceed", session(key)["plan_msg_ts"])
    if wait(key, lambda s: s["status"] == "confirming", "the confirmation", 60):
        click(key, "plan_confirm", session(key)["plan_msg_ts"])


def webhook(key):
    body = json.dumps({"webhookEvent": "jira:issue_created", "issue": {"key": key, "fields": {"labels": [LABEL]}}}).encode()
    sig = hmac.new(E["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    httpx.post(f"{URL}/jira-webhook", content=body, timeout=30, headers={"Content-Type": "application/json", "X-Hub-Signature": f"sha256={sig}"})


def ticket(summary, description):
    key = JIRA.post("/rest/api/2/issue", json={"fields": {"project": {"key": "CA"}, "summary": summary, "description": description,
                                                       "issuetype": {"name": "Task"}, "labels": [LABEL]}}).json()["key"]
    created.append(key)
    log("new", f"Jira ticket {key}: {summary}")
    webhook(key)
    return key


def slack_since(since, key=""):
    msgs = SLACK.get("/conversations.history", params={"channel": DM, "oldest": f"{since:.6f}", "limit": 200}).json().get("messages", [])
    out = []
    for m in reversed(msgs):
        text = m.get("text", "") + " " + json.dumps(m.get("blocks", []))
        if not key or key in text:
            out.append({"ts": m["ts"], "text": m.get("text", "")[:500], "blocks": json.dumps(m.get("blocks", []))[:3000]})
    return out


def jira_comments(key):
    return [c["body"] for c in JIRA.get(f"/rest/api/2/issue/{key}", params={"fields": "comment"}).json()["fields"]["comment"]["comments"]]


def app_log(pattern=r"WARNING|ERROR|Job finished|stuck|instead|takes over|queued|Delayed|unreachable"):
    out = docker("logs", APP_NAME)
    return [ln[:300] for ln in (out.stdout + out.stderr).splitlines() if re.search(pattern, ln) and "httpx" not in ln][-30:]


def pr_files(number):
    return [f["filename"] for f in GH.get(f"/pulls/{number}/files").json()]


# ---------------------------------------------------------------------------------------------------------------------
R = infra()
R.flushall()

if want("P1"):
    # P1: Slack unreachable while the first plan is made
    since = time.time()
    app(slack_down=True)
    log("P1", "app up with Slack unreachable")
    A = ticket("E2E reliability: state totals script",
               "Create a new file `state_totals.py` in the repo root. It should load "
               "`crime/01_District_wise_crimes_committed_IPC_2001_2012.csv` with pandas (path relative to the script), drop rows "
               "where DISTRICT == 'TOTAL', keep YEAR == 2012, and print the 5 STATE/UT with the most 'TOTAL IPC CRIMES' "
               "(summed over their districts). Put the logic in functions and run them under if __name__ == '__main__'.\n"
               "Also add a line about the new script to README.md.")
    s = wait(A, lambda s: s["status"] in ("awaiting_reply", "cancelled") and (s.get("plan") or s.get("result")), "the plan", 600)
    check("P1", "plan made and kept although Slack is down", s and s.get("plan") and s.get("plan_msg_ts") == "", s and s.get("result"))
    check("P1", "plan held in Redis for later", R.llen("jiraagent:slackpending") >= 1, R.llen("jiraagent:slackpending"))
    check("P1", "Jira says Slack can't be reached", any("Slack can't be reached" in c for c in jira_comments(A)), jira_comments(A)[-1:])

    # P2: Slack back
    since = time.time()
    app()
    log("P2", "app restarted with Slack reachable")
    s = wait(A, lambda s: s.get("plan_msg_ts"), "the delayed plan to be posted", 120)
    delayed = [m for m in slack_since(since, A) if "Delayed" in m["blocks"]]
    check("P2", "plan posted, marked Delayed, with its Proceed button", s and delayed and "plan_proceed" in delayed[-1]["blocks"],
          delayed[-1]["text"] if delayed else slack_since(since, A))

    # P3: take README out of the plan
    since = time.time()
    version = session(A)["plan_version"]
    log("P3", "plan v%s files" % version, plan=session(A)["plan"][-800:])
    say(A, "Please leave README.md out of this: only create the script. Don't change any other file.")
    s = wait(A, lambda s: s["status"] == "awaiting_reply" and s["plan_version"] > version, "the revised plan", 600)
    check("P3", "revised plan doesn't name README.md", s and "README" not in (s.get("plan") or ""), (s or {}).get("plan", "")[-600:])
    B = ticket("E2E reliability: rows per year", "Create a new file `rows_per_year.py` in the repo root that loads "
               "`crime/01_District_wise_crimes_committed_IPC_2001_2012.csv` with pandas and prints how many rows each YEAR has.")
    wait(B, lambda s: s["status"] == "awaiting_reply" and s.get("plan_msg_ts"), "the second ticket's plan", 600)

    # P4: approve A; meanwhile write to B (one job at a time: it has to wait)
    since = time.time()
    approve(A)
    wait(A, lambda s: s["status"] == "executing", "A executing", 60)
    time.sleep(5)
    say(B, "Which column will you count by?")
    time.sleep(8)
    acks = [m for m in slack_since(since, B) if "Looking into it" in m["text"]]
    check("P4", "the message to the second ticket says what it waits for", acks and f"Waiting for {A}'s coding run" in acks[-1]["text"],
          acks[-1]["text"] if acks else slack_since(since, B))
    s = wait(A, lambda s: s["status"] == "delivered", "A's PR", 900)
    files = pr_files(s["pr_number"]) if s and s.get("pr_number") else []
    check("P4", "PR contains only the planned file", files == ["state_totals.py"], files)
    undone = [w for m in slack_since(since, A) for w in [m["text"]] if "doesn't include" in w]
    log("P4", "out-of-plan changes undone by the app" if undone else "the model kept to the plan by itself (nothing to undo)",
        undone=undone[:1])
    check("P4", "the second ticket was answered after the run", wait(B, lambda s: s["status"] == "awaiting_reply" and not s.get("pending_acks"), "B's answer", 600), "")

if want("P5"):
    # P5: OpenCode hangs, TOOL_FALLBACK off: stuck -> once more by itself -> reported
    since = time.time()
    app({"OPENCODE_EXTRA_CONFIG": HANG, "TOOL_FALLBACK": "false", "JOB_STUCK_SECONDS": "60"})
    log("P5", "app restarted: OpenCode can't reach its model (hangs), TOOL_FALLBACK=false, JOB_STUCK_SECONDS=60")
    C = ticket("E2E reliability: stuck planner", "Create `hello_stuck.py` that prints hello.")
    s = wait(C, lambda s: s["status"] == "awaiting_reply" and s.get("result"), "the stuck failure", 900)
    msgs = slack_since(since, C)
    check("P5", "stopped as stuck and tried once more by itself", any("trying once more" in m["text"] for m in msgs), [m["text"][:150] for m in msgs])
    check("P5", "the second stuck attempt is reported", s and any("got stuck" in m["text"] and "couldn't produce the plan" in m["text"] for m in msgs),
          (s or {}).get("result"))
    check("P5", "admin log line", any("made no progress" in ln for ln in app_log()), [ln for ln in app_log() if "progress" in ln][-2:])

if want("P6"):
    # P6: the same hang with TOOL_FALLBACK on -> Aider plans; OpenCode broken while coding -> Aider codes
    since = time.time()
    app({"OPENCODE_EXTRA_CONFIG": HANG, "JOB_STUCK_SECONDS": "60"})
    log("P6", "app restarted: OpenCode hangs, TOOL_FALLBACK=true")
    D = ticket("E2E reliability: fallback to Aider", "Create a new file `district_count.py` in the repo root that loads "
               "`crime/01_District_wise_crimes_committed_IPC_2001_2012.csv` with pandas and prints how many distinct DISTRICT "
               "values there are in 2012 (excluding 'TOTAL').")
    s = wait(D, lambda s: s["status"] == "awaiting_reply" and s.get("plan_msg_ts"), "the Aider plan", 900)
    msgs = slack_since(since)  # (a lone ticket's warnings don't carry its key)
    check("P6", "Aider wrote the plan after OpenCode got stuck", s and any("Aider wrote this plan" in m["text"] for m in msgs), [m["text"][:150] for m in msgs])
    since = time.time()
    app({"OPENCODE_EXTRA_CONFIG": BROKEN})
    log("P6", "app restarted: OpenCode's config is broken (it refuses to start)")
    approve(D)
    s = wait(D, lambda s: s["status"] == "delivered", "D's result", 900)
    msgs = slack_since(since, D)
    check("P6", "Aider did the coding and the PR was opened", s and s["rounds"][-1]["outcome"] == "delivered"
          and any("Aider did the work" in m["text"] for m in msgs), [m["text"][:200] for m in msgs][-2:])
    if s and s.get("pr_number"):
        check("P6", "PR has the planned file", pr_files(s["pr_number"]) == ["district_count.py"], pr_files(s["pr_number"]))

# ---------------------------------------------------------------------------------------------------------------------
prs = [f"#{s['pr_number']}" for k in created if (s := session(k)) and s.get("pr_number")]
log("end", "costs", costs={k: [json.loads(x) for x in R.lrange(f"jiraagent:cost:{k}", 0, -1)] for k in created})
docker("rm", "-f", APP_NAME, REDIS_NAME)
print("\n==== RESULTS ====")
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {detail}"))
print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed. Tickets: {', '.join(created)}. PRs: {', '.join(prs) or 'none'}. "
      f"Checkpoints: {OUT}")
