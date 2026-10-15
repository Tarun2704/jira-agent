# End-to-end test with injected faults (2026-10-14)

The whole flow (Jira webhook → plan in Slack → chat → approve → code → PR → follow-up round) was run against the real
app in its Docker image, once with **Aider** (`PLANNER_BACKEND=aider`, `CODING_BACKEND=aider`) and once with
**OpenCode** (both `opencode`), breaking one thing at each stage. Real Jira (project CA), real GitHub
(Crimes-in-India), real Slack bot; "my" Slack messages and button clicks were signed requests sent to the app exactly as
Slack sends them.

**Isolation:** a throwaway local Redis (Render's deployment reads the shared Upstash Redis, and its `/tick` would have
picked up test sessions) and the trigger label `ai-agent-e2e` (Render's Jira webhook filter is `labels = ai-agent`).

Test tickets: CA-12 … CA-17. Test PRs: #40, #41, #42, #43 (see [todo.md](todo.md) for cleanup).

## Stages and faults

| # | Stage | Fault injected | Expected |
|---|---|---|---|
| 1 | Planning | Planner's main model doesn't exist | Backup model writes the plan; Slack says so |
| 2 | Webhook | Same Jira webhook twice | Second one ignored |
| 3 | Chat | A question about the plan | Answered; plan version unchanged |
| 4 | Coding | Main **and** fallback coding model don't exist | Clean failure, clear reason, Retry button, Jira back to To Do |
| 5 | Retry | Config fixed, then "can u try again" (Aider: with Slack **down**; OpenCode: first via the Retry button with `OPENCODE_MAX_STEPS=1`) | Same approved plan runs again (no new plan, no re-approval), first-round branch, PR opened |
| 6 | Follow-up + crash | Follow-up round approved, container **killed mid-coding**, restarted | `/tick` re-queues it after the lease lapses; round 2 lands on the same PR |
| 6b | Crash after push | Container killed the moment the round's commit reached GitHub | Recovery records the round; nothing is coded twice |

## Results

| Stage | Aider | OpenCode |
|---|---|---|
| 1 Planning, main model missing | ❌ first run (CA-12): **every Aider call to `gemini-2.5-flash` failed** (bug 1). ✅ after the fix (CA-14): backup model planned in 12 s. ⚠️ the file-finding step before it has no backup model ("couldn't scan the project") | ❌ first run (CA-15): OpenCode said only "UnknownError", no backup tried (bug 2). ✅ after the fix (CA-16, CA-17): backup model planned in 15–39 s |
| 2 Duplicate webhook | ✅ "a plan for this ticket is already in progress" | ✅ same |
| 3 Question | ✅ answered in ~15 s, plan stays v1 | ✅ answered in 15–80 s, plan stays v1 |
| 4 Both coding models missing | ✅ backup tried, clean failure with Retry button, Jira → To Do | ✅ same; ⚠️ message named `AIDER_MODEL` (fixed) |
| 5 Retry | ✅ same plan, PR #41 on `ai/CA-14-…`; with Slack down Jira still got every update. ❌ left a stale lease behind (bug 3) | ✅ Retry button → step limit → clear failure (⚠️ wording, fixed); "can u try again" → PR #42, same plan |
| 6 Crash mid-coding | ❌ the job had **already pushed** before the kill; recovery coded it again and reported a false "I couldn't make that change" (bug 4) | ✅ killed before the push; re-queued by `/tick`, round 2 on PR #42 (2 commits, no duplicate) |
| 6b Crash right after push | | ✅ after the fix: recorded in 1.3 s, 0 model requests, PR #42 has exactly one round-3 commit |

(A second, unplanned full OpenCode run on CA-17 passed every stage again.)

## Bugs found, and what was changed

1. **Broken `aider-model-settings.yml` made Google reject every Aider call to `gemini-2.5-flash`** (introduced earlier
   the same day when a Claude entry was added to a file without a final newline: `threshold: BLOCK_NONE# Claude…`).
   The 400 wasn't recognised, so people saw "I couldn't put together a clear plan", and no backup model was tried.
   *Fixed:* the file; a test that the file parses with valid values; 400 "invalid request" errors are now named
   ("setup problem") and, like "model not found", may use the backup model once (another model may accept it).
2. **OpenCode reports an unknown model only as "UnknownError: Unexpected server error"** (nothing on stderr), so the
   backup model was never tried and people saw "something went wrong". *Fixed:* before a run the app checks the model
   against `opencode models <provider>` (cached) and reports "model not found"; and an opaque OpenCode error before
   its first model request counts as a model problem (backup tried).
3. **A job skipped because another process held the ticket's lease was never retried.** Seen when the container
   restarted while a job was still trying to post to (a down) Slack: the owner's next message started a plan job that
   was skipped, and the ticket sat on "Looking into it…" until a `/tick` (up to ~10 min; never without a pinger).
   *Fixed:* a skipped job schedules itself again for when the lease lapses, as long as the ticket still needs it.
4. **A crash after the push but before the ticket was marked delivered made the recovered job redo the work**, which
   then failed ("couldn't apply the edit", because the change was already there) and told the owner the opposite of
   what happened. *Fixed:* each approved run gets a delivery id, stamped into its commit (`Agent-Delivery: <id>`); a
   (re)started run that finds its own stamp on the branch with an open PR records the delivery instead of coding again.
5. **Messages:** step-limit failures started with "The LLM call failed" and showed as "something went wrong" in Slack;
   "model not found" pointed OpenCode users at `AIDER_MODEL`. *Fixed* (wording and friendly texts).

All fixes have tests (286 passing).

## Still open (in [todo.md](todo.md))

- **Recovery after a crash waits for `/tick`.** A ticket whose job died is re-queued only by the next tick after its
  lease lapses: up to ~10 minutes with the pinger, never without it. A single-instance app could re-check on its own
  shortly after startup.
- **Slack outages lose messages.** Posts are retried a few times, while the job holds its lease, then dropped (only
  results and failures fall back to a Jira comment). An outbox in Redis, like the one for Jira comments, would keep
  them, and posting outside the lease would avoid bug 3's trigger.
- **No backup model for two smaller calls:** the planner's file-finding step (Aider planner only) and the PR
  description (falls back to a basic description).
- **Error handling is string matching.** `friendly.py` and the retry/fallback decisions recognise English phrases;
  a reworded or new error silently becomes "something went wrong". Proposal: errors carry a code (`model_not_found`,
  `quota_daily`, `step_limit`, …) that decides the message, the backup-model attempt and whether Retry is offered
  (Retry is pointless for a setup problem until it's fixed).
- A crash between the push and creating the PR (first round only) still redoes the work. The result is correct (the
  branch is force-pushed), it just costs a second run.

## How to run it

```
docker build -t jira-agent .
python scripts/e2e_faults.py aider          # or: opencode;  --until S3 stops before any code or PR
```

`scripts/e2e_faults.py` runs the test app on port 8001 with its own Docker network and Redis (the live app on 8000 is
not touched), creates the Jira ticket, sends the webhook, signs Slack messages and clicks, kills/restarts the container,
and records a checkpoint after each stage: session state, Jira status and comments, the bot's Slack messages and the
app log. Crash recovery is left to the app's watchdog (no `/tick`). It prints the ticket and PR to clean up.

**Re-run on 2026-10-14 after the fixes** (CA-19, Aider): every stage passed; the watchdog re-queued the crashed round 30 s
after the restart, and the crash right after the push was recorded instead of redone.
