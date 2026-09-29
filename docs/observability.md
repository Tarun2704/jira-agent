# Observability: logs and job history

How to find out what the agent did with a ticket, or why it didn't do anything.

## Where to look

| Question | Where |
|---|---|
| Did Jira's webhook arrive? Why was it ignored? | `/jobs` → `webhooks`, or Render **Logs** (search `Webhook`) |
| What happened to ticket CA-3? | `/jobs?issue=CA-3`, or Render **Logs** (search `CA-3#`) |
| Which step failed, and why? | `/jobs` → `status`, `detail`, `steps`; or Render **Logs** (search `ERROR`) |
| How long did it take, and how many LLM tokens? | `/jobs` → `duration_s`, `steps`, `tokens`; or the `Job finished` log line |
| What did the model actually do? | The **Agent log** section at the bottom of the PR |
| Is the service up? | `/health` |
| Was a deploy running at that time? | Render → service → **Events** |

## The `/jobs` page

Open in a browser:

```
https://jira-agent-r9w8.onrender.com/jobs?token=<WEBHOOK_SECRET>
https://jira-agent-r9w8.onrender.com/jobs?token=<WEBHOOK_SECRET>&issue=CA-3
```

It needs the `WEBHOOK_SECRET` (the token is hidden as `token=***` in the logs). It returns, newest first:

- **`in_flight`**: tickets queued or running right now
- **`jobs`**: the last 50 jobs:

  | Field | Meaning |
  |---|---|
  | `issue_key`, `id` | Ticket and a short job ID (logs show it as `CA-3#1a2b3c4d`) |
  | `status` | `queued` → `running` → `succeeded` / `no_changes` / `skipped` / `failed` |
  | `queued_at`, `started_at`, `finished_at`, `duration_s` | Timing (UTC) |
  | `steps` | Seconds per step: `fetch_ticket`, `check_open_pr`, `clone`, `aider`, `checks`, `aider_fix` (only if a syntax error had to be fixed), `describe`, `commit_push`, `create_pr` |
  | `checks` | Result of each check, e.g. `{"Python syntax": "passed"}` |
  | `files` | Files named in the ticket and handed to Aider |
  | `tokens` | LLM tokens used (Aider + PR description) |
  | `event` | `jira:issue_created`, `jira:issue_updated`, or `recovered_after_restart` |
  | `pr_url` | The PR opened (or the already-open PR if skipped) |
  | `jira_status` | Last status the agent moved the ticket to |
  | `detail` | Why it was skipped, or the error if it failed |

- **`webhooks`**: the last 100 webhook deliveries, each with `outcome` (`accepted`, `ignored`, `rejected`) and `reason`

**`/jobs` is in memory:** it starts empty after every restart or deploy. For older history, use Render's Logs.

## Reading the logs

Render → service **jira-agent** → **Logs**. Every line from a job carries its tag, e.g. `[CA-3#1a2b3c4d]`; lines outside a job show `[-]`.

A successful run:

```
INFO [-] jira-agent: Webhook accepted: queued CA-3#1a2b3c4d (event=jira:issue_created)
INFO [CA-3#1a2b3c4d] jira-agent: Job started (event=jira:issue_created)
INFO [CA-3#1a2b3c4d] app.agent: Ticket: 'Dashboard crashes on startup...' (Task)
INFO [CA-3#1a2b3c4d] app.agent: Cloned Tarun2704/Crimes-in-India@main into branch ai/CA-3-...
INFO [CA-3#1a2b3c4d] app.agent: Files named in ticket: ['Crimes in India Dashboard.py']
INFO [CA-3#1a2b3c4d] app.agent: Running aider with model groq/openai/gpt-oss-120b
INFO [CA-3#1a2b3c4d] app.agent: Changed files: Crimes in India Dashboard.py | 2 +- | 1 file changed...
INFO [CA-3#1a2b3c4d] app.agent: Opened https://github.com/.../pull/4
INFO [CA-3#1a2b3c4d] jira-agent: Job finished: status=succeeded | total=55.2s | steps: fetch_ticket=0.4s ... aider=41.0s ... | tokens: 7400 sent / 1300 received | pr=...
```

What problems look like:

| Log line | Meaning / fix |
|---|---|
| *(no `Webhook` line at all)* | Jira never delivered it. Check Render **Events** for a deploy at that time; replay with `scripts/send_test_webhook.py`. |
| `WARNING ... Webhook rejected: invalid signature/token` | The Secret in the Jira webhook doesn't match `WEBHOOK_SECRET` on Render. |
| `Webhook ignored: ... reason=missing label 'ai-agent'` | Ticket lacks the label. |
| `Webhook ignored: ... reason=ignored event 'jira:issue_updated' ...` | Normal: an edit or comment, not a trigger. |
| `Webhook ignored: ... reason=already queued` | Duplicate delivery while the ticket was already being worked on. |
| `Startup recovery: N ticket(s) re-queued from Jira label 'ai-agent-queued': [...]` | Logged ~2 min after every start; lists tickets resumed after a restart (usually 0). |
| `WARNING ... Jira status 'In Review' not available for CA-3 (available: [...])` | That status isn't in the workflow; add it on the board or change `JIRA_STATUS_IN_REVIEW`. |
| `WARNING ... Could not add label 'ai-agent-queued'` | Jira call failed; the job still runs but won't survive a restart. |
| `Check Python syntax: passed - ...` | The changed files compile. |
| `WARNING ... Syntax errors, asking Aider to fix them: ...` | The first attempt broke the syntax; one fix attempt follows. Look for `after fix attempt: passed/failed`. |
| `Skipped: PR already open` | Close/merge that PR, then re-add the label. |
| `Aider made no changes. Output tail: ...` | Ticket too vague; the tail shows what the model said. |
| `WARNING ... PR description generation failed` | LLM call failed (often rate limits); PR still opened with a basic description. |
| `ERROR ... Job failed at step 'clone'` + traceback | The named step broke; the traceback says why (e.g. expired `GITHUB_TOKEN`). |

## Limits

- Render's free plan keeps logs for a limited time; check soon after a problem.
- `/jobs` resets on every deploy/restart.
- No alerting yet: nobody is notified when a job fails. Free options: Sentry (error emails) and UptimeRobot (pings `/health`, emails if it's down, and keeps the free instance awake).
