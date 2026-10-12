"""Pretend you replied to the plan in Slack: sends a correctly signed Slack event to the local app.

It reads the waiting plan from Redis, so no Slack, tunnel or copy-pasting of ids is needed.
If this works but a real reply doesn't, the problem is between Slack and your app (tunnel, Request URL,
event subscription), not the app itself.

Usage (from the project folder):
    python scripts/send_test_slack_reply.py "yes, mobile too"
    python scripts/send_test_slack_reply.py "skip the tests" --key CA-1 --url http://localhost:8000
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from app.store import WAITING, SessionStore  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("text")
parser.add_argument("--url", default="http://localhost:8000")
parser.add_argument("--key", default="", help="ticket key (default: the one plan that is waiting)")
args = parser.parse_args()

for name in ("REDIS_URL", "SLACK_SIGNING_SECRET", "SLACK_USER_ID"):
    if not os.getenv(name):
        sys.exit(f"{name} is empty in .env")

store = SessionStore.from_url(os.environ["REDIS_URL"])
sessions = [store.get(k) for k in store.active_keys()]
print("Active sessions:", [(s.issue_key, s.status) for s in sessions if s] or "none")
waiting = [s for s in sessions if s and s.slack_thread_ts and (s.issue_key == args.key or (not args.key and s.status in WAITING))]
if len(waiting) != 1:
    sys.exit("Need exactly one waiting plan with a Slack thread; pass --key to choose.")
s = waiting[0]

payload = {
    "type": "event_callback",
    "event_id": "EvTEST" + uuid.uuid4().hex[:8],
    "event": {
        "type": "message", "channel_type": "im", "channel": s.slack_channel, "thread_ts": s.slack_thread_ts,
        "user": os.environ["SLACK_USER_ID"], "text": args.text, "ts": f"{time.time():.6f}",
    },
}
body = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = "v0=" + hmac.new(os.environ["SLACK_SIGNING_SECRET"].encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
r = httpx.post(
    f"{args.url.rstrip('/')}/slack/events", content=body, timeout=30,
    headers={"Content-Type": "application/json", "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
)
print(r.status_code, r.text)
if r.status_code == 401:
    print("-> The app rejected the signature: SLACK_SIGNING_SECRET in the app's environment differs from this .env.")
elif r.status_code == 503:
    print("-> Plan mode isn't fully configured in the running app. Check the app's startup log for 'CONFIG:' lines.")
else:
    time.sleep(2)
    print("Session is now:", store.get(s.issue_key).status)
    print("The app's log should show: 'Slack event received', then 'Slack message handled: revise:awaiting_reply'.")
    print("A new plan DM follows in Slack once the planner finishes (the status may already be back to 'awaiting_reply').")
