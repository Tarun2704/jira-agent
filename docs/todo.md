# TODO

Open follow-ups, so they don't get lost. Done items move to [known-issues.md](known-issues.md) ("Problems already
fixed"); bigger themes live in [roadmap.md](roadmap.md). Newest first within each section.

## Before the next deploy

- [ ] **Commit the work on `feature/tick`**: OpenCode backend, `PLANNER_BACKEND`, retry fix, cost fixes, docs. (Not committed yet, by choice.)
- [ ] **Render environment** (Render deploys from git, so set these when the branch is deployed):
  - `OPENCODE_FALLBACK_MODEL` must be `google/...` (not `gemini/...`); `/health` → `problems` now says so.
  - `PLANNER_BACKEND` now defaults to `opencode`: the instance needs 1 GB+ even with `CODING_BACKEND=aider` (2 GB if planning and coding overlap), or set `PLANNER_BACKEND=aider` on Free.
  - OpenCode needs an instance with 1 GB+ memory (Free has 512 MB).
- [ ] **Decide what to do with the comparison PRs** #38 (CA-10, Aider) and #39 (CA-11, OpenCode) on Crimes-in-India: same task, both correct. Merge one, close the other.
- [ ] **Clean up the e2e test artifacts** ([e2e-test-report.md](e2e-test-report.md)): Jira CA-12 … CA-20 (label `ai-agent-e2e`) and PRs #40 … #45.

## Bugs and gaps found, not fixed yet

- [x] ~~Local and Render shared one Redis~~: the local app now has its own Redis and is the live one (see [plan-first.md](plan-first.md) "The current local setup"). When moving to a new host, give it its own Redis too.
- [x] ~~Host the app off the laptop~~: done 2026-10-14 on Oracle's free 1 GB AMD VM (see [plan-first.md](plan-first.md) "Running on a server"). One job at a time, with swap; slower than a laptop.
- [ ] **Move to the free ARM VM (2 OCPU / 12 GB) when Oracle has capacity**: the retry job (`~/.oci/jira-agent-vm-retry.sh`, every 10 min, up to 14 days) notifies on success. Then planning and coding can run in parallel again.
- [ ] **Upstash command budget** if Redis moves back to Upstash: free tier is 500K commands/month; with the watchdog every 60 s plus `/tick`, set `WATCHDOG_SECONDS=180` or more. (The server's own Redis has no limit.)
- [ ] **Pause the Render service and its `/tick` pinger** (cron-job.org/UptimeRobot) until it's the live host again: its Jira webhook is disabled and its tickets were moved to the local Redis, but a running Render app still uses Slack credentials and would answer `/tick`.
- [ ] **Render Free + OpenCode planner**: `PLANNER_BACKEND` now defaults to `opencode` (~550 MB per run); on the 512 MB Free instance set `PLANNER_BACKEND=aider` until the instance is upgraded.

- [x] ~~Structured error codes~~: done 2026-10-14, `app/errors.py` (one table: wording, backup model, Retry button, admin alert). Admin alerts are log lines (`ADMIN: ...`) until messages are routed (below).
- [ ] **Hung jobs**: the watchdog (done) restarts jobs whose process *died*. A job that is alive but stuck keeps refreshing its lease and is only ended by its own timeouts (`OPENCODE_TIMEOUT_SECONDS`, `PLANNER_TIMEOUT_SECONDS`, Aider's). Add an overall time limit per job (e.g. no progress for 3 minutes → stop and re-run once, then fail with a message), checked by the watchdog.
- [ ] **Post Slack messages outside the job lease**: failed posts are now queued (done), but the retries before queueing still run while the job holds its lease.
- [x] ~~Backup model for the PR description~~: done 2026-10-14 (`AIDER_FALLBACK_MODEL`, tried once; both calls are counted).
- [ ] **GitHub webhook instead of polling** for PR merged/closed, reviews and comments (`pull_request`, `pull_request_review`, `pull_request_review_comment`, `issue_comment`): instant instead of up to ~10 minutes, and less API use. Needs an endpoint with signature check (`X-Hub-Signature-256`) and the webhook on the repo; keep `/tick` polling as the safety net. Decide what an **approval** should do (today: nothing; merge completes the ticket).
- [x] ~~Don't offer Retry for setup problems~~: done 2026-10-14; the message says to reply `retry` once the admin has fixed it.

- [x] ~~Gemini 3 and `temperature`~~: done 2026-10-14; no temperature for Gemini 3 (direct calls and Aider, via a per-run model settings file).
- [x] ~~Cache prices for Gemini~~: done 2026-10-14 (from litellm's price table; matches OpenCode's own cost figure).
- [ ] **Old cost records keep old numbers**: entries saved before the cost fix (e.g. CA-11's first OpenCode run, $0.0487 instead of $0.0246) aren't recalculated.
- [x] ~~`.opencode/` appended to `.git/info/exclude` on every run~~ and ~~`.DS_Store` tracked~~: done 2026-10-14.
- [ ] **Rotate the Gemini API key** if you want to be safe: before 2026-10-14 a failed Gemini call logged its request URL with `?key=...` (local Docker logs; on Render, Render's log viewer). Logs now mask keys and tokens.

## Who gets which message (planned)

Today every Slack message goes to `SLACK_USER_ID`. Target:

- [ ] **Assignee** gets the ticket's plan, questions, results and *friendly* errors (DM, by mapping the Jira assignee's e-mail to a Slack user via `users.lookupByEmail`). Only the assignee can approve.
- [ ] **No assignee** on a ticket with the trigger label: DM the **reporter** that the ticket has no assignee (and nothing starts until it has one), plus a Jira comment.
- [ ] **Admin** (for now `SLACK_USER_ID`) gets *technical* problems only: configuration (bad key, unknown model, settings file), GitHub/Jira permission errors, crashes, a job given up on, `/health` problems, with the reference and the technical detail.
- [ ] Needs the **structured error codes** above: each code says whether it is for the assignee (what to do) or the admin (what broke).
- [ ] Session keeps `assignee_slack_id`; approvals and button clicks are checked against it instead of `SLACK_USER_ID`; reminders go to the assignee.

## Next improvements

- [ ] **Content search for the Aider planner**: rank candidate files by ripgrep hits for the ticket's words, not only by path. Cheap (no model calls) and fixes the "path doesn't match" misses. See [planner-comparison.md](planner-comparison.md).
- [ ] **`PLANNER_BACKEND=auto`**: Aider on small repos, OpenCode when the repo has more candidate files than the planner's 300-path shortlist. Needs a few more comparisons on big repos first.
- [ ] **Fall back to the other coding backend** when the *tool* fails (OpenCode missing, out of memory, step limit), not the model.
- [ ] **Route coding by ticket size** (files in the plan, tests needed): only after 10–20 Aider vs OpenCode comparisons, including multi-file tickets. So far: one single-file ticket, where both were correct and Aider used 2 requests vs 9.
- [ ] **Retry for "no changes"** outcomes, like the new Retry for failed runs.
- [x] ~~Test scripts in `scripts/`~~: `scripts/compare_planners.py` and `scripts/e2e_faults.py` (done 2026-10-14).
- [ ] **Aider planner: large files are hidden** (over 200 KB, e.g. FastAPI's `routing.py`), so it plans around code it can't see. Pass the relevant part (the functions that match the ticket) instead of hiding the whole file.
