"""Plan the same tickets with PLANNER_BACKEND=aider and =opencode (same model) and save everything for comparison.

Only the planner runs: nothing is pushed and nothing is written to Jira or Slack (Jira tickets are only read). Run it
inside the app's image, so OpenCode is the same version as in production:

    docker build -t jira-agent .
    docker run --rm --env-file .env -e REDIS_URL= -e PYTHONPATH=/app \
        -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/tmp:/work" jira-agent \
        python /app/scripts/compare_planners.py --out /work/plans CA-11 CA-7 --fastapi

(scripts/ isn't part of the image, hence the first -v.)

Each plan is saved as <out>/<ticket>-<backend>.json (plan, files, requests, tokens, cost, the tools OpenCode used).
See docs/planner-comparison.md for what came out of it on 2026-10-14.
"""
import argparse
import json
import time
import traceback
from pathlib import Path

from app.config import Settings
from app.costs import make_entry, model_prices
from app.jira_client import JiraClient, JiraIssue
from app.jobs import Job
from app.planner import Planner
from app.store import Session

# Two tickets on a big public repo (3,000+ files) that don't name their files: the hard case for the Aider planner.
FASTAPI = [
    JiraIssue("FA-1", "Malformed JSON bodies: say where the JSON is broken",
              "When a client sends a request body that is not valid JSON, the 422 response says the JSON is invalid but "
              "not where. Include the line and column number of the JSON syntax error in the error's `ctx`, so clients "
              "can point users at the problem. Keep the error type the same.", ["ai-agent"], "Task"),
    JiraIssue("FA-2", "Encode timedeltas as ISO 8601 durations",
              "`jsonable_encoder` turns a `datetime.timedelta` into a float number of seconds. Encode it as an ISO 8601 "
              "duration string instead (for example `P1DT2H` or `PT30S`), which is what OpenAPI's `duration` format "
              "expects. Add a test.", ["ai-agent"], "Task"),
]

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("tickets", nargs="*", help="Jira keys to plan, on GITHUB_REPO")
parser.add_argument("--fastapi", action="store_true", help="also plan FA-1 and FA-2 on fastapi/fastapi")
parser.add_argument("--backends", default="aider,opencode")
parser.add_argument("--aider-model", default="gemini/gemini-3.1-flash-lite")
parser.add_argument("--opencode-model", default="google/gemini-3.1-flash-lite")
parser.add_argument("--out", default="plans")
args = parser.parse_args()

out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
base = Settings()
jobs = [(base.github_repo, key) for key in args.tickets] + ([("fastapi/fastapi", i) for i in FASTAPI] if args.fastapi else [])
if not jobs:
    parser.error("name at least one ticket, or --fastapi")

for repo, ticket in jobs:
    for backend in args.backends.split(","):
        settings = Settings(github_repo=repo, planner_backend=backend, workdir="/tmp/compare-planners",
                            planner_model=args.aider_model, opencode_planner_model=args.opencode_model)
        issue = ticket if isinstance(ticket, JiraIssue) else JiraClient(settings).get_issue(ticket)
        job = Job(issue.key, "compare", kind="plan")
        job.start()
        rec = {"ticket": issue.key, "repo": repo, "backend": backend, "title": issue.summary}
        t0 = time.time()
        try:
            result = Planner(settings, job).run(issue, Session(issue.key))
            rec.update(ok=True, plan=result.body, files=result.files, questions=result.questions, warnings=result.warnings)
        except Exception as e:
            rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:1500], trace=traceback.format_exc()[-1500:])
        rec["seconds"] = round(time.time() - t0, 1)
        entry = make_entry(job.id, "plan", 1, job.calls, model_prices(settings.model_prices))
        rec.update(requests=entry["requests"], sent=entry["sent"], received=entry["received"], cost_usd=entry["cost_usd"],
                   calls=entry["calls"], steps=job.steps,
                   tools=[ln[7:] for r in job.runs for ln in r["output"].splitlines() if ln.startswith("[tool] ")])
        (out / f"{issue.key}-{backend}.json").write_text(json.dumps(rec, indent=1, default=str))
        cost = f"${rec['cost_usd']:.4f}" if rec["cost_usd"] is not None else "unknown"
        print(f"{issue.key:6s} {backend:8s} ok={rec['ok']} {rec['seconds']:5.1f}s requests={rec['requests']:2d} cost={cost} "
              f"files={rec.get('files')} {rec.get('error', '')[:200]}", flush=True)
