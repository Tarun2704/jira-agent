# Plan-first workflow (Slack + Redis)

Every ticket with the `ai-agent` label now gets a **plan first**. Nothing is coded until you approve it in Slack.

```
Jira ticket (ai-agent label)
  → planner reads the repo (Aider, read-only "ask" mode) → plan + any questions
  → Slack DM to you, with a [Proceed with this plan] button
  → you chat: questions get an answer under your message; only a real change makes a new plan version
  → [Proceed] → "Are you sure? It can't be stopped" → [Yes, start now]
  → the existing flow runs (Aider → checks → PR), with the approved plan added to its instructions
  → Slack DM with the PR link (or the failure)
```

If `SLACK_BOT_TOKEN`, `SLACK_USER_ID` or `REDIS_URL` is empty, the old straight-to-PR flow is used unchanged.

## Settings

| Variable | Meaning |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…`, from the Slack app's OAuth & Permissions page |
| `SLACK_SIGNING_SECRET` | Slack app → Basic Information → Signing Secret |
| `SLACK_USER_ID` | Your member ID in that workspace. Only you can chat and approve |
| `REDIS_URL` | `rediss://default:<password>@<host>:6379` (Upstash free tier works) |
| `TICK_TOKEN` | Secret for `GET /tick?token=…` |
| `PLANNER_MODEL` | Model for plans and revisions. Empty = same as `AIDER_MODEL` |
| `PLAN_REMINDER_HOURS` | No reply after this long → Jira comment tagging the reporter (default 48) |
| `PLAN_WORKERS` | How many plans/revisions run at once (1 to 4, default 1). Each is a clone plus an Aider process (roughly 200-300 MB), so keep 1 on a 512 MB instance |
| `SEPARATE_PLAN_QUEUE` | `true` (default): plans/revisions have their own worker. `false`: share the single coding worker |

Settings are read from the environment (and `.env` locally) by `app/config.py`. The names match the variables (`SLACK_BOT_TOKEN` ↔ `slack_bot_token`). Never put real values in `config.py`; the defaults there are only for non-secret options.

## What lives in Redis

One JSON session per ticket (status, plan, chat history, Slack thread), a short-lived **lease** per running job, the set of active tickets, and Slack event ids (Slack redelivers events). Finished sessions expire after 30 days. Nothing polls Redis, so Upstash's 500k commands/month is plenty.

## Crash recovery: `/tick`

A running job refreshes its lease every ~100 s. If the process dies, the lease lapses (5 min) and the next `/tick` sees a session that should be running but has no lease, and re-queues it. A re-run starts from scratch, which is safe for the same reasons as before (fresh clone, force-pushed `ai/` branch, open-PR check).

`/tick` also sends the "no reply" reminder once per wait. A reply resets the clock.

**Something outside Render must call `/tick`**, because a sleeping free instance runs nothing. Use UptimeRobot, cron-job.org or similar, every 10 minutes (5 if allowed):

```
https://<your-service>.onrender.com/tick?token=<TICK_TOKEN>
```

That call also keeps the free instance awake.

## Run it locally

1. Fill in `.env` (the five values above plus the existing Jira/GitHub/LLM ones).
2. Start the app: `uvicorn app.main:app --port 8000` (or the Docker command in the README).
3. Slack can't reach `localhost`, so open a tunnel: `ngrok http 8000` (or `cloudflared tunnel --url http://localhost:8000`).
4. In the Slack app, set both URLs to the tunnel address:
   - Event Subscriptions → Request URL: `https://<tunnel>/slack/events` (it should say *Verified*)
   - Interactivity & Shortcuts → Request URL: `https://<tunnel>/slack/interactive`
5. Trigger a real ticket that has the label: `python scripts/send_test_webhook.py CA-1`.
6. A DM appears in the app's **Messages** tab. Reply in the thread, then press **Proceed**.
7. Optional, to see crash recovery and reminders: `curl "http://localhost:8000/tick?token=<TICK_TOKEN>"`.

For production, put the Render URL in the two Slack fields instead of the tunnel, and add the pinger.

## The queue

- **Coding jobs**: one at a time, as before (small instance, rate-limited LLM).
- **Planning jobs** (first plan and every revision): up to `PLAN_WORKERS` at a time (default 1), on their own workers, so a 15-minute coding run doesn't delay your reply. This means up to two Aider processes at once. If memory is tight, set `SEPARATE_PLAN_QUEUE=false` and everything shares one worker (replies then wait behind a running coding job).
- **One job per ticket** at any moment, whichever queue it is in.
- The queue itself is in memory. What survives a restart is the *session* in Redis plus the lease; `/tick` re-queues anything that should be running but isn't.

## What is recorded (observability)

Every Aider run (the plan, each chat round, the coding run and the syntax-fix attempt) is saved with the exact prompt sent, the model's output, the model name, and whether it succeeded. Secrets are scrubbed. It lives in Redis for 30 days, newest 40 runs per ticket, and is read with:

```
curl "https://<your-service>/runs/CA-5?token=<WEBHOOK_SECRET>"
```

`/jobs` still shows the timings, tokens, files and PR for recent jobs (in memory, reset on restart); each job there now carries a `runs` count. The chat itself (your messages and the bot's answers) is in the ticket's session in Redis.

## Which files the plan looks at

1. Files named in the ticket text or in your chat replies (path or file name).
2. Files found in earlier rounds, remembered in the session.
3. If there are none: a small direct LLM call (like the PR-description writer) is given the repo's file list (up to 300 text files, ranked by words shared with the ticket) and returns up to 8 relevant paths. Invented paths are dropped.

Aider (read-only) then reads those files in full to write the plan. The files the plan ends up listing are saved and handed to the coding run, so Aider edits them directly instead of hunting for them.

## If your reply does nothing

The plan DM only needs the bot token (app → Slack). Replies and buttons go the other way (Slack → your app), so they need more:

1. **Check the app's startup log** for `CONFIG:` lines (or open `/health` and read `problems`). A missing `SLACK_SIGNING_SECRET` is the classic: plans arrive, replies are rejected.
2. **Test the app without Slack:** `python scripts/send_test_slack_reply.py "yes, mobile too"`. If this works (log shows `Slack message handled: revise:awaiting_reply`), the app is fine and the problem is between Slack and your app (steps 3 to 5).
3. **Is Slack reaching you?** Run the tunnel, then in the Slack app → Event Subscriptions, Request URL must be `https://<tunnel>/slack/events` and show **Verified**. Free tunnel addresses change on every restart, so re-paste after restarting it. The request URL must not still point at Render.
4. **Subscribed events:** `message.im` under *Subscribe to bot events*. After changing scopes or events Slack asks you to **reinstall the app**; do it.
5. **Look for `Slack event received` in the app's log** when you reply. Nothing there means Slack isn't delivering (steps 3 and 4). `Slack event ignored` or `Ignored a Slack message from user ...` means it arrived but was filtered; the latter means `SLACK_USER_ID` isn't your ID in this workspace.
6. Replies must be a normal message from you in the DM (ideally inside the plan's thread), not an edited one.

## How the chat behaves

- Every message you send gets a "Looking into it..." reply directly under it (in the main DM if you wrote there, in the thread if you wrote in a thread). That reply is then edited into the answer.
- The planner decides per message: a **question or comment** is just answered; the plan and its version stay as they are. Only a **change request**, or an answer that alters steps or files, produces a new plan version, posted as a new message right below yours. "Show me the plan again" re-posts the current plan the same way.
- Old plan versions stay readable: they lose the Proceed button and get a "Superseded by vN" note.
- Messages in the main DM go to your only waiting plan; if several plans are waiting, reply inside the plan's thread.

## When the LLM fails

Aider prints provider errors into its output instead of failing loudly, so the app now looks for them when a run produced nothing useful (no plan, no code changes) and names the cause in Slack and Jira: token or context limit, rate limit, content filter or blocked reply, rejected API key, unknown model, provider down or timed out, or an empty reply. The provider's own error line is quoted. A failed chat round keeps your old plan and says so; reply to retry. A failed file search doesn't stop planning; it posts a :warning: that the file list may be incomplete.

## Several tickets at once

- **Buttons** always know their ticket. So does any reply inside a thread under a plan, an answer or your own message.
- **A plain message in the main chat** goes to your only waiting plan. If several are waiting, the bot asks "Which ticket is this about?" with one button per ticket and sends your message to the one you press. Nothing is applied until you press.
- **`CA-7: also add tests`** goes straight to CA-7 and skips the question.
- **`tickets`** (also `open`, `list`, `status`, `help`) lists open tickets: ticket link, title, what it's doing or waiting for, how long ago, and a **Show plan** button that re-posts its current plan below.
- With more than one ticket open, the bot starts its messages with the ticket key (`*CA-7* · ...`). With one, it doesn't.

## Things to know

- **Questions come from the planner, not from Aider during coding.** Aider runs unattended (`--yes-always`) and can't ask anything, so all doubts must be cleared in the plan. If coding still ends with no changes, you get a DM saying so.
- **Each revision re-reads the repo** (a fresh shallow clone and an Aider run). Each costs LLM tokens. On Groq's free tier a large repo map plus a long chat can exceed the per-request limit; the history sent is capped at the last 12 messages.
- **Once you press "Yes, start now" it can't be stopped yet.** The cancel hook comes later; the lease and the status already model "a job is running".
- **One plan per ticket at a time.** Re-adding the label while a plan is active is ignored; after the run ends (done, failed, no changes) it starts a fresh plan.
- **Replies outside a thread** go to your only waiting plan; if several are waiting, the bot asks you to reply inside the right thread.

## Follow-ups after a PR is raised or merged

When coding finishes the ticket becomes **delivered**, not finished. It stays open for `FOLLOWUP_DAYS` (default 14) and every finished piece of work is a **round** (round 1 is the ticket itself).

- **Ask for more in Slack.** Any message about a delivered ticket (reply under its result, or `CA-7: ...`) is a follow-up. The planner decides: a question gets an answer only; a change request becomes a new plan ("Follow-up plan for CA-7, round 2, v1") that goes through the normal Proceed / confirm steps. Pure thanks (`thanks`, `ok`, `lgtm`) just get a 👍.
- **Re-trigger from Jira.** Re-adding `ai-agent` (or editing the description, then pressing **Plan a follow-up** in Slack) starts a new round. The label is only needed for rounds started from Jira, so Slack follow-ups work even if the label was removed. The label is never removed automatically.
- **GitHub review comments.** `/tick` polls the PR. New human comments are offered once in Slack with **Plan changes from these** / **Ignore**; Plan changes starts a round with the comments as the request.
- **Which branch?** Decided when you confirm (the confirm message says which): an open or draft PR gets a new commit on the same branch and a comment on the PR; a closed or merged PR (or none) starts fresh from the main branch as `ai/KEY-r<n>` with a new PR that links the previous one. If GitHub can't be reached the round fails with a message and the ticket stays delivered.
- **Context the planner and coder get:** the ticket, every earlier round (request, plan, files, summary, outcome, PR, last diff), new Jira comments by people since the last round, the review comments and the Slack conversation.
- **Merged = completed.** `/tick` notices a merged PR, marks the ticket completed, and tells you in Slack and Jira. Later Slack messages about it are answered with "ticket is completed; create a new Jira ticket (or update the description and re-add the label)". A Jira re-trigger of a completed ticket starts a new session on a new branch with the earlier rounds as history. After the window passes with no activity the ticket is completed too. A closed, unmerged PR is announced once; the ticket stays delivered.

## Errors and fallbacks

- Slack posts are retried 3 times, then sent as plain text; important ones also go to Jira as a comment if Slack stays down.
- Jira comments that fail are queued, you are told once in Slack, and `/tick` retries them.
- A trigger that can't be stored (Redis down) is held in memory and retried on `/tick`; the Jira ticket gets a comment.
- Failures in message/button handling are reported to you in Slack.
- "Looking into it" messages that never got an answer are closed on `/tick` (`ACK_STALE_SECONDS`).
- Optional fallback models (`PLANNER_FALLBACK_MODEL`, `AIDER_FALLBACK_MODEL`) are tried once when the first model fails with a limit/rate/provider error; the Slack message says so.
- If a Jira status move is refused, the result message includes a warning. If the PR can't be created, you get a compare link to open it yourself.

## Retries

- **GitHub, Jira and Slack API calls** retry up to 3 times with a short backoff on connection errors and HTTP 429 (waiting as long as `Retry-After` says, at most 30s). Safe calls (GET, PUT, DELETE) also retry on timeouts and 5xx. A POST is never repeated after a timeout or 5xx, so a pull request or comment can't be created twice.
- **git clone / push / fetch** retry 3 times on network errors (DNS, timeout, connection reset, 5xx), not on real errors such as a missing branch.
- **LLM rate limits:** with no fallback model set, the run waits `RATE_LIMIT_WAIT_SECONDS` (default 45, 0 = off) and tries once more, and the result mentions it.

## Deleting files

Aider can only edit files; it cannot delete them. So deletions are a separate, checked part of the plan, not something the model does:

1. The planner lists files to delete in an optional `## Delete` section (one path per line, or `None`).
2. When the plan is made, the code keeps only paths that are tracked files in the repo (never under `.git`, at most 10). Anything dropped is reported in a warning. The plan shown in Slack has a **Delete** section with exactly the checked paths, and the "are you sure" step repeats them.
3. On Proceed, the agent runs `git rm` for those files **before** anything else, then tells the model in its prompt that they are already deleted and to carry on with the rest of the plan (or change nothing if nothing else is left).
4. The result message lists what was deleted, and the round remembers it for later follow-ups. If the model call fails after deleting, you are told only the deletions were made.

Plans without a `## Delete` section (including older ones) delete nothing.

## What people see when something fails

Slack and Jira only get plain-language messages: what happened and what to do next (for example "The AI service is busy or at its usage limit right now. Please try again in a few minutes."). No model names, provider text, exception names or git output. Each failure ends with a short **Reference** code (the job id).

The technical details stay on the server: the Render logs (every line is tagged `[CA-7#<reference>]`) and `/runs/<ticket>?token=<WEBHOOK_SECRET>` for the full prompts and replies. The wording lives in `app/friendly.py`; unknown errors get a generic "something went wrong on my side" message.

## Cost per ticket

Every model call (file search, planning, coding, syntax fix, PR description, and any retry or fallback call) is recorded with its model and token counts. When a planning or coding job ends, one record per job is saved in Redis under the ticket and its round (`jiraagent:cost:<KEY>`, no expiry, newest 200 kept).

- **See it:** `GET /usage/CA-7?token=<WEBHOOK_SECRET>` returns each round (requests, tokens, cost, and the calls behind them) plus the ticket total.
- **Prices:** set `MODEL_PRICES` to JSON of USD per 1M tokens, `{"model name": [input, output]}`, using the exact model names. Prices are not built in because they change. A model that isn't listed shows its cost as unknown (null), and so does any round that used one; tokens and request counts are always kept. List free-tier models as `[0, 0]`.
- **Slack:** the delivery message ends with `Cost this round: about $0.12 (ticket total $0.30)`, or `AI use this round: 5 requests, 42k tokens` when a price is missing. `SHOW_COST_IN_SLACK=false` hides it.
- **Estimates:** Aider's token counts come from the `Tokens:` lines it prints, so costs are estimates. A call that times out before printing is not counted, and Aider's own internal retries are not visible.

## Self-ping (keeps /tick running)

`/tick` does the background work: recovering crashed jobs, the 48-hour reminder, merge and PR-comment checks. Something has to call it. Besides an outside pinger, the app now calls its **own public** `/tick` every `SELF_PING_INTERVAL_SECONDS` (default 600).

- **Why the public URL:** Render's free tier sleeps a service after about 15 minutes without *incoming* requests. A call to our own `https://...onrender.com/tick` comes in through Render's front door, so it counts as traffic and resets that timer, and it runs the tick.
- **When it runs:** only if plan-first mode is on, `TICK_TOKEN` is set, and a public URL is known (`RENDER_EXTERNAL_URL`, which Render sets itself, or `SELF_PING_URL`). On your own machine none of these is set, so it stays off. `SELF_PING_ENABLED=false` turns it off.
- **Limit:** it can't wake a service that is already asleep (the timer sleeps too). After a deploy or restart it starts again by itself. An outside pinger on `/tick` covers that gap; a paid always-on instance removes it.
- **Logs:** each ping logs `Self-ping: /tick answered 200`. A 401 means `TICK_TOKEN` is wrong; "could not reach our own /tick" means the call failed and will be retried. The token is never logged. The startup log says whether self-ping is ON or OFF.
- **Check it:** `GET /health` now shows `tick`: when the last tick ran, how long ago, who triggered it (`startup` or `request`), whether self-ping is on, and `overdue: true` if there was no tick for more than two intervals.

### Overlapping ticks, the token, and alerts

- **One tick at a time.** A tick takes a Redis lock (`jiraagent:ticklock`, at most `TICK_LOCK_SECONDS`, default 300) and an in-process lock. A second call (self-ping plus an outside pinger, a slow tick, two instances during a deploy, your local container on the same Redis) gets `{"skipped": "a tick is already running"}` and does nothing, so a reminder or PR-comment prompt can't go out twice. A tick that crashes releases the lock, and if it can't, the lock expires by itself. If Redis is down the tick fails (503) as before.
- **The token is sent as `Authorization: Bearer ...`**, not in the URL, so it can't appear in request logs. An outside pinger can keep using `?token=` or send the header.
- **Slack alert.** After `SELF_PING_ALERT_AFTER` (3) failed self-pings in a row you get one message saying why in plain words (wrong token, wrong address, can't reach itself, or the tick failed), and one more when it recovers. A ping that is only slow (the tick still running) is not a failure. This cannot fire while the whole service is asleep, since nothing is running to send it; an outside pinger or a paid always-on instance covers that.

