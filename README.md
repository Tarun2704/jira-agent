# Jira Coding Agent

Jira ticket (label `ai-agent`) → webhook → this service → Aider + free LLM edits the repo → branch pushed → PR opened → PR link commented back on the ticket.

```
Jira ──webhook──▶ POST /jira-webhook ──queue──▶ CodingAgent
                                               ├─ fetch issue (Jira REST)
                                               ├─ git clone / branch ai/KEY-slug
                                               ├─ aider --message "<ticket>"
                                               ├─ commit + push
                                               ├─ open PR (GitHub REST)
                                               └─ comment PR link on ticket
```

## Layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: webhook auth, label filter, dedupe, single-worker queue |
| `app/agent.py` | The job: clone → Aider → commit → push → PR → Jira comment |
| `app/jira_client.py` | Get issue, add comment |
| `app/github_client.py` | Default branch, find/create PR |
| `app/config.py` | Env-var settings |
| `scripts/send_test_webhook.py` | Send a signed fake Jira webhook |

## Local run

```bash
cp .env.example .env          # fill in creds
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
# create a ticket DEMO-1 with label ai-agent in Jira, then:
.venv/bin/python scripts/send_test_webhook.py DEMO-1
```

Or with Docker: `docker build -t jira-agent . && docker run --env-file .env -p 8000:8000 jira-agent`

Tests (no creds needed): `pip install -r requirements-dev.txt && pytest -q`

## Deploy to Render (free)

1. Push this folder to its own GitHub repo.
2. Render → New → Web Service → pick the repo → Runtime: Docker → Instance: Free.
3. Add every variable from `.env.example` under Environment.
4. Health check path: `/health`. Note the URL, e.g. `https://jira-agent.onrender.com`.

## Connect Jira

Jira ⚙ → System → WebHooks → Create a WebHook:

- **URL**: `https://jira-agent.onrender.com/jira-webhook` (if there's no Secret field, append `?token=<WEBHOOK_SECRET>`)
- **Secret**: your `WEBHOOK_SECRET`
- **Events**: Issue → created and updated
- **JQL**: `project = CA AND labels = ai-agent`

The agent runs on a new labelled ticket, or when the `ai-agent` label is added to an existing one.
Other updates (edits, comments — including the agent's own) are ignored.

## Notes / limits

- Jobs run one at a time; the queue and dedupe live in memory, so a restart drops queued jobs.
- Render free sleeps after ~15 min idle. The first webhook wakes it up (~30-60s).
- Draft PRs need a public repo on GitHub Free; for private repos the agent falls back to a normal PR.
- Free LLM tiers are rate-limited; switch providers with `AIDER_MODEL` + the matching API key.
- Always review the PR — the agent is not a substitute for code review.
