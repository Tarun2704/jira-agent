"""End-to-end run of the real app (Docker image) with a fault injected at each stage. See docs/e2e-test-report.md.

    docker build -t jira-agent .
    python scripts/e2e_faults.py aider            # or: opencode
    python scripts/e2e_faults.py opencode --until S3   # stop after the chat stage (no code, no PR)

Stages: S1 plan with a missing main model (backup takes over) · S2 duplicate webhook · S3 a question about the plan ·
S4 coding with main and backup models missing (clean failure, Retry button) · S5 config fixed, retry the same plan
(aider: "can u try again" with Slack down; opencode: "retry" with OPENCODE_MAX_STEPS=1, then the step-limit failure's Retry button) · S6 follow-up round, container killed
mid-coding, recovered · C1 another round, container killed the moment its commit reaches GitHub (recorded, not redone).

What it touches: creates a real Jira ticket in the CA project (label `ai-agent-e2e`, so the live webhook filter
`labels = ai-agent` ignores it), opens a real PR from S5 on, and the bot posts to your Slack DM. "Your" messages and
clicks are signed requests sent straight to the test app, as Slack sends them. It runs on its own port (8001), Docker
network and Redis, so the live app on port 8000 and its Redis are not touched. Clean up the ticket and PR afterwards.
Each stage ends with a checkpoint (session, Jira status and comments, the bot's Slack messages, the app log) in --out.
"""
import argparse
import hashlib
import hmac
import json
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import redis
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
STAGES = ["S1", "S2", "S3", "S4", "S5", "S6", "C1"]
parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("backend", choices=["aider", "opencode"])
parser.add_argument("--until", choices=STAGES, default="C1", help="last stage to run")
parser.add_argument("--port", type=int, default=8001)
parser.add_argument("--image", default="jira-agent")
parser.add_argument("--out", default=None, help="checkpoint file (JSON lines); default e2e-<backend>-<time>.jsonl")
parser.add_argument("--keep", action="store_true", help="leave the test containers running at the end")
args = parser.parse_args()
LAST = STAGES.index(args.until)
BACKEND = args.backend
OUT = Path(args.out or f"e2e-{BACKEND}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")

E = dotenv_values(REPO / ".env")
URL, LABEL = f"http://localhost:{args.port}", "ai-agent-e2e"
NET, REDIS_NAME, APP_NAME, REDIS_PORT = "jira-agent-e2e", "jira-agent-e2e-redis", "jira-agent-e2e", 6390
JIRA = httpx.Client(base_url=E["JIRA_BASE_URL"], auth=(E["JIRA_EMAIL"], E["JIRA_API_TOKEN"]), timeout=30)
GH = httpx.Client(base_url=f"https://api.github.com/repos/{E['GITHUB_REPO']}", timeout=30,
                  headers={"Authorization": f"Bearer {E['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
SLACK = httpx.Client(base_url="https://slack.com/api", headers={"Authorization": f"Bearer {E['SLACK_BOT_TOKEN']}"}, timeout=30)
DM = SLACK.post("/conversations.open", data={"users": E["SLACK_USER_ID"]}).json()["channel"]["id"]
T0, KEY = time.time(), ""
MISSING = {"aider": "gemini/gemini-0-no-such-model", "opencode": "google/gemini-0-no-such-model"}[BACKEND]
BASE = {  # main models work; each stage breaks what it tests
    "TRIGGER_LABEL": LABEL, "REDIS_URL": f"redis://{REDIS_NAME}:6379", "LEASE_SECONDS": "30", "WATCHDOG_SECONDS": "15",
    "RATE_LIMIT_WAIT_SECONDS": "45", "PLANNER_BACKEND": BACKEND, "CODING_BACKEND": BACKEND, "PORT": "8000",
    "AIDER_MODEL": "gemini/gemini-3.5-flash-lite", "PLANNER_MODEL": "", "OPENCODE_MODEL": "google/gemini-3.5-flash-lite",
    "OPENCODE_PLANNER_MODEL": "", "PLANNER_FALLBACK_MODEL": "gemini/gemini-3.5-flash-lite",
    "AIDER_FALLBACK_MODEL": "gemini/gemini-flash-lite-latest", "OPENCODE_FALLBACK_MODEL": "google/gemini-flash-lite-latest",
}


def log(stage, msg, **data):
    line = {"t": round(time.time() - T0, 1), "backend": BACKEND, "key": KEY, "stage": stage, "msg": msg, **data}
    with OUT.open("a") as f:
        f.write(json.dumps(line, default=str) + "\n")
    short = {k: v for k, v in data.items() if k in ("http", "outcome", "pr", "detail", "status", "tick", "body")}
    print(f"[{line['t']:6.1f}s] {stage:4s} {msg} {json.dumps(short, default=str)[:300] if short else ''}", flush=True)


def docker(*a, check=False):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check)


def infra():
    docker("network", "create", NET)
    docker("rm", "-f", REDIS_NAME)
    docker("run", "-d", "--name", REDIS_NAME, "--network", NET, "-p", f"127.0.0.1:{REDIS_PORT}:6379", "redis:7-alpine", check=True)
    for _ in range(30):
        try:
            if redis.Redis(port=REDIS_PORT).ping():
                return redis.Redis(port=REDIS_PORT, decode_responses=True)
        except redis.ConnectionError:
            time.sleep(0.5)
    raise SystemExit("test Redis did not start")


def app(overrides=None, kill=False):
    """(Re)start the test app with BASE + overrides. kill=True: SIGKILL first (a crash), not a clean stop."""
    if kill:
        docker("kill", APP_NAME)
    docker("rm", "-f", APP_NAME)
    cmd = ["run", "-d", "--name", APP_NAME, "--network", NET, "-p", f"{args.port}:8000", "--env-file", str(REPO / ".env")]
    for k, v in {**BASE, **(overrides or {})}.items():
        cmd += ["-e", f"{k}={v}"]
    docker(*cmd, args.image, check=True)
    for _ in range(90):
        try:
            return httpx.get(f"{URL}/health", timeout=3).json()
        except Exception:
            time.sleep(1)
    raise SystemExit("test app did not start")


def session():
    raw = R.get(f"jiraagent:session:{KEY}")
    return json.loads(raw) if raw else None


def wait(pred, what, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        s = session()
        if s and pred(s):
            return s
        time.sleep(3)
    log("error", f"timed out waiting for {what}", session=session(), app_log=app_log())
    raise SystemExit(1)


def sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(E["SLACK_SIGNING_SECRET"].encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def say(text):
    body = json.dumps({"type": "event_callback", "event_id": "Ev" + uuid.uuid4().hex[:10], "event": {
        "type": "message", "channel_type": "im", "channel": DM, "user": E["SLACK_USER_ID"], "text": f"{KEY}: {text}",
        "ts": f"{time.time():.6f}"}}).encode()
    r = httpx.post(f"{URL}/slack/events", content=body, headers={"Content-Type": "application/json", **sign(body)}, timeout=20)
    log("slack", f"I said: {text!r}", http=r.status_code)


def click(action, message_ts):
    payload = {"type": "block_actions", "user": {"id": E["SLACK_USER_ID"]}, "channel": {"id": DM},
               "message": {"ts": message_ts}, "actions": [{"action_id": f"{action}:{KEY}", "value": KEY}]}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    r = httpx.post(f"{URL}/slack/interactive", content=body, timeout=20,
                   headers={"Content-Type": "application/x-www-form-urlencoded", **sign(body)})
    log("slack", f"I clicked {action}", http=r.status_code)


def approve():
    click("plan_proceed", session()["plan_msg_ts"])
    wait(lambda s: s["status"] == "confirming", "the confirmation", 60)
    click("plan_confirm", session()["plan_msg_ts"])


def webhook():
    body = json.dumps({"webhookEvent": "jira:issue_created", "issue": {"key": KEY, "fields": {"labels": [LABEL]}}}).encode()
    sig = hmac.new(E["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    r = httpx.post(f"{URL}/jira-webhook", content=body, timeout=30,
                   headers={"Content-Type": "application/json", "X-Hub-Signature": f"sha256={sig}"})
    return r.status_code, r.json()


def tick():
    return httpx.get(f"{URL}/tick", params={"token": E["TICK_TOKEN"]}, timeout=60).json()


def bot_messages(since):
    msgs = SLACK.get("/conversations.history", params={"channel": DM, "oldest": f"{since:.6f}", "limit": 100}).json().get("messages", [])
    return [{"ts": m["ts"], "text": m.get("text", "")[:600], "buttons": [e.get("action_id") for b in m.get("blocks", [])
             if b.get("type") == "actions" for e in b.get("elements", [])]} for m in reversed(msgs) if KEY in m.get("text", "") or m.get("blocks")]


def app_log(pattern=r"WARNING|ERROR|Job finished|fallback|Rate limited|daily|requeu|Retry|lease|already on"):
    out = docker("logs", APP_NAME)
    return [ln[:400] for ln in (out.stdout + out.stderr).splitlines() if re.search(pattern, ln) and "httpx" not in ln][-40:]


def checkpoint(stage, since):
    s = session() or {}
    i = JIRA.get(f"/rest/api/2/issue/{KEY}", params={"fields": "status,comment"}).json()["fields"]
    log(stage, "checkpoint", status=s.get("status"), round_no=s.get("round_no"), plan_version=s.get("plan_version"),
        rounds=[{k: r.get(k) for k in ("n", "outcome", "pr_url", "branch", "detail")} for r in s.get("rounds", [])],
        jira_status=i["status"]["name"], jira_comments=[c["body"][:300] for c in i["comment"]["comments"]][-4:],
        slack=bot_messages(since), app_log=app_log())


def run(stage):
    return STAGES.index(stage) <= LAST


TASKS = {
    "aider": ("E2E (aider): top districts script",
              "Create a new file `top_districts.py` in the repo root. Don't change any other file.\n"
              "It should load `crime/01_District_wise_crimes_committed_IPC_2001_2012.csv` with pandas (path relative to "
              "the script), drop rows where DISTRICT == 'TOTAL', keep YEAR == 2012, and print the 10 districts (with "
              "their STATE/UT) that have the most 'TOTAL IPC CRIMES'. Put the logic in functions and run them under "
              "if __name__ == '__main__'."),
    "opencode": ("E2E (opencode): crime trend script",
                 "Create a new file `crime_trend.py` in the repo root. Don't change any other file.\n"
                 "It should load `crime/01_District_wise_crimes_committed_IPC_2001_2012.csv` with pandas (path relative "
                 "to the script), keep only rows where DISTRICT == 'TOTAL', sum 'TOTAL IPC CRIMES' per YEAR, and print "
                 "each year with its total and the percentage change from the previous year. Put the logic in "
                 "functions and run them under if __name__ == '__main__'."),
}
FOLLOWUPS = {"aider": ["Also print the 5 districts with the fewest crimes in 2012, below the top 10.",
                       "Also print how many districts there are in 2012, at the end."],
             "opencode": ["Also print which year had the biggest increase, after the table.",
                          "Also print the average yearly change at the end."]}

# --------------------------------------------------------------------------------------------------------------------
R = infra()
R.flushall()
summary, description = TASKS[BACKEND]
KEY = JIRA.post("/rest/api/2/issue", json={"fields": {"project": {"key": "CA"}, "summary": summary, "description": description,
                                                   "issuetype": {"name": "Task"}, "labels": [LABEL]}}).json()["key"]
log("setup", f"created Jira ticket {KEY} (label {LABEL}); checkpoints in {OUT}")

# S1 + S2: planning with the main model missing; a duplicate webhook meanwhile
since = time.time()
missing = {"PLANNER_MODEL": MISSING} if BACKEND == "aider" else {"OPENCODE_PLANNER_MODEL": MISSING}
log("S1", "app up; the planner's main model doesn't exist", problems=app(missing).get("problems"), **missing)
code, body = webhook()
log("S1", "webhook sent", http=code, body=body)
code, body = webhook()
log("S2", "duplicate webhook sent", http=code, body=body)
s = wait(lambda s: s["status"] in ("awaiting_reply", "cancelled"), "the plan (or the planning failure)", 600)
log("S1", "plan ready" if s.get("plan") else "NO PLAN", plan=s.get("plan"), files=s.get("files"))
checkpoint("S1", since)
if not s.get("plan"):
    raise SystemExit(1)

if run("S3"):  # a question about the plan: answered, plan unchanged
    since = time.time()
    n_msgs, version = len(session()["messages"]), s["plan_version"]
    say("Which columns of the CSV will you use?")
    s = wait(lambda s: s["status"] == "awaiting_reply" and len(s["messages"]) > n_msgs + 1 and s["handled_upto"] == len(s["messages"]), "the answer", 300)
    log("S3", "answered", plan_version_before=version, plan_version_after=s["plan_version"])
    checkpoint("S3", since)

if run("S4"):  # coding with both models missing
    since = time.time()
    broken = ({"AIDER_MODEL": MISSING, "AIDER_FALLBACK_MODEL": "gemini/gemini-0-also-missing"} if BACKEND == "aider"
              else {"OPENCODE_MODEL": MISSING, "OPENCODE_FALLBACK_MODEL": "google/gemini-0-also-missing"})
    app(broken)
    log("S4", "app restarted: coding main and backup models both missing", **broken)
    approve()
    s = wait(lambda s: s["status"] == "delivered", "the failed result")
    log("S4", "coding finished", outcome=s["rounds"][-1]["outcome"], detail=s["rounds"][-1]["detail"])
    checkpoint("S4", since)

if run("S5"):  # fix the config, retry the same plan
    since = time.time()
    if BACKEND == "opencode":
        # S4 was a setup problem: no Retry button, the message says to reply "retry" once fixed
        failure = bot_messages(since - 3600)[-1]
        log("S5", "S4's failure message", retry_button="retry_round" in failure["buttons"], text=failure["text"][:300])
        app({"OPENCODE_MAX_STEPS": "1"})
        log("S5", "app restarted with the models fixed but OPENCODE_MAX_STEPS=1; replying 'retry'")
        say("retry")
        s = wait(lambda s: s["status"] == "delivered" and s["rounds"] and s["rounds"][-1].get("finished_at", 0) > since, "the step-limited retry")
        log("S5", "step-limited retry finished", outcome=s["rounds"][-1]["outcome"], detail=s["rounds"][-1]["detail"])
        checkpoint("S5a", since)
        since = time.time()
        app()
        step_failure = [m for m in bot_messages(since - 3600) if "retry_round" in m["buttons"]]
        log("S5", "app restarted with normal settings; pressing the step-limit failure's Retry button",
            retry_button=bool(step_failure))
        click("retry_round", step_failure[-1]["ts"])
        s = wait(lambda s: s["status"] == "delivered" and s["rounds"] and s["rounds"][-1].get("finished_at", 0) > since, "the retried result")
        log("S5", "retry finished", outcome=s["rounds"][-1]["outcome"], pr=s.get("pr_url"), branch=s.get("branch"), rounds=len(s["rounds"]))
        checkpoint("S5", since)
    else:
        app({"SLACK_BOT_TOKEN": "xoxb-invalid-e2e"})
        log("S5", "app restarted with the models fixed but Slack DOWN (invalid bot token); replying 'retry'")
        plan_before = session()["plan"]
        say("can u try again")
        s = wait(lambda s: s["status"] == "delivered" and s["rounds"] and s["rounds"][-1].get("finished_at", 0) > since, "the retried result")
        log("S5", "retry finished", outcome=s["rounds"][-1]["outcome"], pr=s.get("pr_url"), branch=s.get("branch"),
            same_plan=s["plan"] == plan_before, rounds=len(s["rounds"]))
        checkpoint("S5", since)
    time.sleep(10)  # (with Slack down the job may still be retrying posts)

if run("S6"):  # follow-up round, crash mid-coding
    since = time.time()
    app()
    say(FOLLOWUPS[BACKEND][0])
    s = wait(lambda s: s["status"] == "awaiting_reply" and s["round_no"] == 2, "the follow-up plan", 600)
    log("S6", "follow-up plan ready", plan=s["plan"])
    approve()
    wait(lambda s: s["status"] == "executing", "executing", 60)
    time.sleep(12 if BACKEND == "aider" else 20)
    app(kill=True)
    log("S6", "CRASH: app killed mid-coding and started again", status=session()["status"])
    # no /tick from here on: the app's watchdog (every 15 s here) should re-queue it once the dead job's lease lapses
    s = wait(lambda s: s["status"] == "delivered" and s["round_no"] == 2 and len(s["rounds"]) >= 2, "the recovered round 2")
    log("S6", "round 2 finished", outcome=s["rounds"][-1]["outcome"], pr=s["rounds"][-1].get("pr_url"),
        watchdog="Watchdog:" in " ".join(app_log(r"Watchdog")))
    checkpoint("S6", since)

if run("C1"):  # another round, crash the moment its commit reaches GitHub
    since = time.time()
    say(FOLLOWUPS[BACKEND][1])
    s = wait(lambda s: s["status"] == "awaiting_reply" and s["round_no"] == 3, "the round 3 plan", 600)
    approve()
    s = wait(lambda s: s["status"] == "executing" and s.get("delivery_id"), "executing", 60)
    did, branch = s["delivery_id"], s["branch"]
    end = time.time() + 600
    while time.time() < end and f"Agent-Delivery: {did}" not in GH.get(f"/commits/{branch}").json().get("commit", {}).get("message", ""):
        time.sleep(0.4)
    app(kill=True)
    log("C1", "CRASH right after the stamped commit reached GitHub", delivery_id=did, status=session()["status"])

    s = wait(lambda s: s["status"] == "delivered" and len(s["rounds"]) >= 3, "the recovered round 3", 300)
    commits = [c["commit"]["message"].split("\n")[0][:90] for c in GH.get(f"/pulls/{s['pr_number']}/commits").json()]
    log("C1", "recovered", outcome=s["rounds"][-1]["outcome"], pr=s.get("pr_url"),
        round3_commits=sum("(round 3)" in c for c in commits), redone="already on" in " ".join(app_log()))
    checkpoint("C1", since)

if s.get("pr_number"):
    p = GH.get(f"/pulls/{s['pr_number']}").json()
    log("end", "PR", pr=p["html_url"], commits=[c["commit"]["message"][:80] for c in GH.get(f"/pulls/{s['pr_number']}/commits").json()])
log("end", "costs", costs=[json.loads(x) for x in R.lrange(f"jiraagent:cost:{KEY}", 0, -1)])
if not args.keep:
    docker("rm", "-f", APP_NAME, REDIS_NAME)
print(f"Done. Clean up: Jira {KEY}" + (f", PR #{s['pr_number']}" if s.get("pr_number") else ""))
