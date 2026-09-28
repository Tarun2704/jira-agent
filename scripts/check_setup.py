"""Verify .env credentials with read-only API calls. Never prints secrets.

Usage: python scripts/check_setup.py
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
ok = True


def report(name: str, passed: bool, detail: str) -> None:
    global ok
    ok &= passed
    print(f"{'✅' if passed else '❌'} {name}: {detail}")


required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN", "GITHUB_REPO", "WEBHOOK_SECRET", "AIDER_MODEL"]
missing = [k for k in required if not os.getenv(k, "").strip()]
placeholders = [k for k in ("JIRA_BASE_URL", "GITHUB_REPO") if "yourname" in os.getenv(k, "") or "your-" in os.getenv(k, "")]
report("env vars", not missing and not placeholders, f"missing={missing} placeholders={placeholders}" if missing or placeholders else "all set")

# Jira
try:
    r = httpx.get(
        f"{os.environ['JIRA_BASE_URL'].rstrip('/')}/rest/api/2/myself",
        auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
        timeout=20,
    )
    report("Jira auth", r.status_code == 200,
           f"logged in as {r.json().get('displayName')}" if r.status_code == 200 else f"HTTP {r.status_code}")
    if r.status_code == 200:
        p = httpx.get(
            f"{os.environ['JIRA_BASE_URL'].rstrip('/')}/rest/api/2/project",
            auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
            timeout=20,
        )
        keys = [x["key"] for x in p.json()] if p.status_code == 200 else []
        report("Jira projects", bool(keys), f"visible projects: {keys}")
except Exception as e:
    report("Jira auth", False, type(e).__name__ + ": " + str(e)[:200])

# GitHub
try:
    gh = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    r = gh.get(f"/repos/{os.environ['GITHUB_REPO']}")
    if r.status_code == 200:
        repo = r.json()
        perms = repo.get("permissions", {})
        report("GitHub repo", perms.get("push", False),
               f"{repo['full_name']} ({'private' if repo['private'] else 'public'}), default branch "
               f"'{repo['default_branch']}', push={perms.get('push')}")
        c = gh.get(f"/repos/{os.environ['GITHUB_REPO']}/commits", params={"per_page": 1})
        report("GitHub repo has commits", c.status_code == 200, "yes" if c.status_code == 200 else "repo looks empty - add a README/code")
        pr = gh.get(f"/repos/{os.environ['GITHUB_REPO']}/pulls", params={"per_page": 1})
        report("GitHub PR access", pr.status_code == 200, "ok" if pr.status_code == 200 else f"HTTP {pr.status_code} - add 'Pull requests' permission")
    else:
        report("GitHub repo", False, f"HTTP {r.status_code} - check token repo access and GITHUB_REPO=owner/repo")
except Exception as e:
    report("GitHub", False, type(e).__name__ + ": " + str(e)[:200])

# LLM (Groq)
model = os.getenv("AIDER_MODEL", "")
if model.startswith("groq/"):
    try:
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}"},
            timeout=20,
        )
        if r.status_code == 200:
            ids = sorted(m["id"] for m in r.json()["data"])
            name = model.removeprefix("groq/")
            report("Groq key", True, "valid")
            report("Groq model", name in ids, f"'{name}' available" if name in ids else f"'{name}' not found; available: {ids}")
        else:
            report("Groq key", False, f"HTTP {r.status_code}")
    except Exception as e:
        report("Groq", False, type(e).__name__ + ": " + str(e)[:200])
else:
    print(f"ℹ️  LLM check skipped for model '{model}'")

sys.exit(0 if ok else 1)
