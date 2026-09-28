"""Send a signed, Jira-shaped webhook to the agent (local or deployed).

Usage:
    python scripts/send_test_webhook.py DEMO-1
    python scripts/send_test_webhook.py DEMO-1 --url https://jira-agent.onrender.com
"""
import argparse
import hashlib
import hmac
import json
import os

import httpx
from dotenv import load_dotenv  # installed with uvicorn[standard]

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("issue_key")
parser.add_argument("--url", default="http://localhost:8000")
parser.add_argument("--label", default=os.getenv("TRIGGER_LABEL", "ai-agent"))
args = parser.parse_args()

payload = {
    "webhookEvent": "jira:issue_created",
    "issue": {"key": args.issue_key, "fields": {"labels": [args.label]}},
}
body = json.dumps(payload).encode()
sig = hmac.new(os.environ["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()

r = httpx.post(
    f"{args.url.rstrip('/')}/jira-webhook",
    content=body,
    headers={"Content-Type": "application/json", "X-Hub-Signature": f"sha256={sig}"},
    timeout=90,  # Render free tier cold start
)
print(r.status_code, r.text)
