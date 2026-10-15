# Known issues and limits

Things to know when running the Jira coding agent: current limitations, problems already fixed (and why), and what to do when something goes wrong.

## Current limitations

| Issue | Impact | Workaround |
|---|---|---|
| **Render free tier sleeps** after ~15 min idle | First ticket after a quiet spell waits 30–60s while the service wakes up. Jira retries if the first delivery times out. | None needed. For always-on, move to an Oracle Cloud Always Free VM or a paid Render instance. |
| **Recovered jobs restart from scratch** | After a restart, a ticket cut off mid-run is re-queued ~2 minutes later and repeated from the beginning (a second "picked this up" comment). | Expected. If the label `ai-agent-queued` is stuck on a ticket that isn't running, remove it by hand. |
| **Status moves need matching statuses** | The agent moves tickets to "In Progress", "In Review" and back to "To Do". A status that doesn't exist in the project's workflow is skipped with a warning in the logs. | Add the missing column on the board, or change `JIRA_STATUS_*` on Render. Set one to empty to disable that move. |
| **One job at a time** | Several tickets created together are handled one after another. | Expected on the 512 MB free instance; don't raise the worker count there. |
| **Vague tickets fail** | "Improve the code" gives poor or no edits. | Name the file(s) and describe the expected behaviour. Tickets that name a file get that file handed to Aider directly. |
| **Groq free tier: ~8,000 tokens per request** | Groq's free tier allows about 8,000 tokens per minute for `gpt-oss-120b`, counting the prompt **and** the reply. Tickets that need a large rewrite (e.g. replacing a 100-line function) get cut off; the agent reports "made no changes" with `hit a token limit` in the output. | Keep tickets small and additive (new functions or files, a few lines changed). For bigger tasks, switch `AIDER_MODEL` to a provider with higher free limits (e.g. Google Gemini's free tier) or a paid tier. |
| **Free LLM limits** | Groq's free tier is rate-limited. Free tiers may log or train on prompts. | Only point the agent at code you're fine sending to the provider. Switch provider with `AIDER_MODEL` + its API key. |
| **Groq retires models** | Jobs fail with a model-not-found error. | Run `.venv/bin/python scripts/check_setup.py`; it lists the models available to your key. Update `AIDER_MODEL` on Render. |
| **GitHub token expires** | Clone/push/PR fails after the expiry date chosen at creation. | Create a new fine-grained token (Contents + Pull requests: Read and write, target repo only) and update `GITHUB_TOKEN` on Render. |
| **Draft PRs need a public repo** on GitHub Free | For private repos, draft creation is rejected. | Handled automatically: the agent retries as a normal PR. |
| **Agent PRs can conflict** | Two tickets touching nearby lines of the same file produce PRs that conflict with each other. | Merge one, then resolve the other on GitHub, keeping both changes. |
| **Single target repo** | Every ticket goes to `GITHUB_REPO`. | Multi-repo support (e.g. map by Jira component) is not built yet. |
| **Checks are syntax-only** | The agent verifies that changed Python files compile (and fixes syntax errors once), but doesn't run the repo's tests, so logic bugs aren't caught. | Review every PR. Running the repo's tests (e.g. Aider's `--test-cmd`) is a possible next step once the target repo has tests. |
| **`/jobs` history resets** on every deploy/restart | Older runs aren't in `/jobs`. | Use Render's Logs (limited retention on the free plan). |
| **No alerts** | Nobody is notified when a job fails. | Check Jira comments / `/jobs`. Sentry or UptimeRobot (free) could add alerts. |
| **Webhooks can be lost during a deploy** | A ticket created while Render is redeploying may never reach the agent (seen with CA-3). | Replay with `send_test_webhook.py`, or re-add the label. Render **Build Filters** can skip deploys for docs-only changes. |
| **OpenCode needs ~550 MB per run** | On Render's Free 512 MB instance a coding job with `CODING_BACKEND=opencode` is killed mid-run. `/health` → `problems` warns about it. | Use Aider on Free, or an instance with 1 GB+ (2 GB comfortable). |
| **OpenCode uses many more model requests than Aider** | Up to `OPENCODE_MAX_STEPS` (default 40) requests per coding step, each carrying the growing conversation: higher cost, and free-tier request limits are hit sooner. | Cap with `OPENCODE_MAX_STEPS`, use a model with higher limits, compare in `/usage/<KEY>`. |
| **OpenCode isn't a full sandbox** | Shell access is off by default. If you turn it on, ticket text could try to make the agent run commands; it can read other files in the container and use the network (its environment is scrubbed of app secrets and git is repaired afterwards, but that is not isolation). | Leave `OPENCODE_ALLOW_BASH=false` unless you trust the tickets; for production run the agent in a separate secret-free sandbox. See [opencode.md §4](opencode.md). |
| **The agent can't run the target repo's tests on Render** | The container has the app's dependencies, not the target repo's, and the agent is told not to install any. | Let the target repo's CI run on the draft PR. See [opencode.md §6](opencode.md). |
| **The Docker image with OpenCode is untested until your first deploy** | It was written without a Docker daemon available, so the first Render build is its first real build. | If it fails, read the Build log (stage 1: `npm install` / `cp` lines). Set `CODING_BACKEND=aider` meanwhile (the old image still works). |
| **The Aider planner can miss code on big repos** | With `PLANNER_BACKEND=aider`, files the ticket doesn't name are found by matching file *paths* against the ticket's words (top 300 paths, at most 8 files), plus Aider's ~4k-token repo map. Code whose path doesn't share the ticket's words can be missed and the plan written against the wrong files. The default `PLANNER_BACKEND=opencode` searches file contents (more model requests). Name the files in the ticket. See [planner-comparison.md](planner-comparison.md). |
| **Free-tier daily quotas run out fast with OpenCode** | Google's free keys allow about 20 requests per day per model; one OpenCode run used 8–9 on a one-file change (Aider: 1–2). | Set a fallback model on a *different* model (it has its own quota), keep `OPENCODE_MAX_STEPS` low, or use a key with billing. |
| **PR description may be missing** | If the extra LLM call fails (rate limit, timeout), the PR gets a basic description instead of the AI-written one. | The PR is still opened; the diff and agent log are in it. |

## When does the agent run?

- **Runs:** a new ticket is created with the `ai-agent` label, or the label is added to an existing ticket.
- **Ignored:** any other edit or comment, including the agent's own comments.
- **Skipped with a comment:** the ticket already has an open agent PR (matched by ticket key, so renaming the ticket doesn't create a duplicate).
- **To rerun a ticket:** close or merge its PR, then remove and re-add the `ai-agent` label.

## Problems already fixed

Kept here so they aren't reintroduced.

| Problem | Cause | Fix |
|---|---|---|
| Agent made no changes on a clear ticket | Aider only edits files "added to the chat" and didn't pick up a file name containing spaces from the model's reply. | Files named in the ticket are passed to Aider explicitly (`mentioned_files` in `app/agent.py`). |
| PR showed every line of the file as changed | The model rewrote the whole file with Unix line endings; the original used Windows (CRLF). | Original line endings are restored after Aider runs (`restore_crlf`). |
| Unrequested reformatting in PRs | Aider used "whole file" edit mode for a model it didn't recognise. | Aider is forced into `diff` (search/replace) mode via `AIDER_EDIT_FORMAT=diff`. |
| Aider auto-ran model-suggested shell commands | `--yes-always` approves everything, including commands such as starting the dashboard server. | `--no-suggest-shell-commands`. |
| Risk of an infinite comment loop | Jira sends `issue_updated` for comments, so the agent's own comments could re-trigger it. | Only ticket creation or newly added `ai-agent` label triggers a run. |
| Renamed ticket could get a duplicate PR | The open-PR check matched the full branch name, which includes the ticket title. | Open PRs are matched by ticket key. |
| Rerun failed if the old PR's branch still existed | A fresh branch from `main` can't be pushed over the leftover `ai/KEY-...` branch without force. | The agent force-pushes its own `ai/` branches (safe: only reached when no PR for the ticket is open). |
| No way to tell why a ticket was ignored | Ignored webhooks weren't logged, and there was no job history. | Every webhook is logged with its outcome and reason; `/jobs` shows recent jobs and deliveries ([observability.md](observability.md)). |
| Restart lost queued/running tickets | The queue was only in memory. | The queue is also kept in Jira as the `ai-agent-queued` label; labelled tickets are re-queued after startup. |
| PRs could contain code that doesn't even compile | Nothing checked the model's edits. | Changed Python files are compiled before the PR; a syntax error gets one automatic fix attempt, and the result is shown in the PR's **Checks** section. |
| Tickets naming a data file failed with a huge token count | Aider adds any file mentioned in the ticket to the model's context; one 1 MB CSV is ~250k tokens. | Files over 200 KB are hidden from Aider via a generated `.aiderignore`; the repo map is skipped when the ticket names its files. |
| Weak PR descriptions | The PR body was just a diff stat and log tail. | An extra LLM call writes Summary / Changes / How to verify from the real diff (`app/pr_writer.py`). |
| OpenCode never switched to the fallback model on a daily quota (CA-11, 2026-10-14) | OpenCode's quota error spans several lines and only the first line was read, so "retry in 19h" was missed: the app waited 45 s and asked the same exhausted model again. (The container that ran it also had no `OPENCODE_FALLBACK_MODEL`.) | Error text is kept on one line, and "retry in Nh" counts as a daily quota: straight to the fallback, no pointless wait. |
| OpenCode fallback model could never work | `OPENCODE_FALLBACK_MODEL=gemini/...` is Aider's spelling; OpenCode answers "Provider not found: gemini". | `/health` → `problems` flags `gemini/...` in the OpenCode model settings and names the `google/...` spelling. |
| "Retry" after a failed run planned again, or did nothing | A reply to a failed run went to the planner as chat: sometimes it only answered ("I'll try again") and nothing ran; making it always re-plan meant an already-approved plan needed approving again. | The failure message has a **Retry with the same plan** button, and a reply like `retry` / `try again` re-runs the coding step with the approved plan. Any other reply still goes to the planner. |
| Costs showed as "unknown", or doubled for OpenCode | Reported costs were saved under three different field names and never read; OpenCode counted cache hits inside `sent` and they were billed at the full input price. | One set of names (`reported_cost`, `cache_hit`, `cache_write`); Aider's `Cost:` line is parsed; cached calls without cache prices use the provider's reported cost. Prices for the usual models are built in (`DEFAULT_PRICES` in `app/costs.py`). |
| "Taken into account in the plan." on every new ticket | The answer slot of the plan post was filled with a stock phrase even when you hadn't written anything. | Only posted when the plan actually answers a message of yours. |
| Every Aider call to `gemini-2.5-flash` rejected (2026-10-14, caught by the e2e test) | A comment was glued onto a value in `aider-model-settings.yml` (`BLOCK_NONE# …`), which Google rejects with a 400; the 400 was shown as "couldn't put together a clear plan". | File fixed and covered by a test; 400 "invalid request" errors are named as a setup problem and may use the backup model. |
| OpenCode with an unknown model: "something went wrong", no backup | OpenCode reports it only as "UnknownError". | The model is checked against `opencode models` first; an opaque error before the first request counts as a model problem. |
| Ticket stuck on "Looking into it…" after a restart | A job that found the ticket's lease held (by a process that had died) was skipped and never retried. | It reschedules itself for when the lease lapses. |
| After a crash, a finished round was redone and reported as failed | The job died after pushing but before marking the ticket delivered; the recovered job coded the same change again. | Each run stamps its commit (`Agent-Delivery: <id>`); a restarted run that finds its stamp records the delivery instead. |
| Slack messages lost during a Slack outage | Posts were retried a few times, then dropped. | They are kept in Redis and sent (marked "Delayed") once Slack answers; the watchdog checks every minute. |
| Crashed jobs waited for an outside `/tick` (up to ~10 min, never without a pinger) | Only `/tick` re-queued sessions whose job had died. | The app's watchdog does it every `WATCHDOG_SECONDS` (60). |
| Jira ticket stayed "In Review" after the PR was merged | The merge completed the session but didn't touch Jira's status. | Moved to `JIRA_STATUS_DONE` (default `Done`). |
| The planner's file scan had no backup model | Only the main planner model was asked. | `PLANNER_FALLBACK_MODEL` is tried once, like for the plan itself. |
| Gemini API key shown in the logs | litellm puts Google's request URL (with `?key=...`) into its error messages, which were logged with their tracebacks. | Every log line is masked for keys, tokens and the app's secret values (`RedactingFormatter`). |
| Error wording, backup-model and Retry decisions matched English phrases | A reworded or new error became "something went wrong", and Retry was offered for setup problems it can't fix. | One table, `app/errors.py`: each kind of failure has a code that decides the wording, the backup model, the Retry button and an `ADMIN:` log line. |
| Tests failed on a developer machine only | Tests read the developer's `.env` (fallback models, backend), which changed the retry path a test exercised. | `tests/conftest.py` blanks those settings. |

## Troubleshooting

1. **Check credentials and model:** `.venv/bin/python scripts/check_setup.py` (read-only calls; never prints secrets).
2. **Check the service is up:** open `https://<your-render-url>/health`. `in_flight` lists tickets being worked on.
3. **See what happened to a ticket:** open `https://<your-render-url>/jobs?token=<WEBHOOK_SECRET>&issue=CA-3`: status, step timings, error, and whether the webhook arrived.
4. **Read the logs:** Render dashboard → the service → **Logs**; search for the ticket key or `ERROR`. See [observability.md](observability.md) for what each line means.
5. **Check Jira delivered the webhook:** Jira ⚙ → System → WebHooks. A 401 means the Secret in Jira doesn't match `WEBHOOK_SECRET` on Render.
6. **Replay a ticket without Jira:** `.venv/bin/python scripts/send_test_webhook.py CA-1 --url https://<your-render-url>`.
