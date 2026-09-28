# Jira Coding Agent

Create a Jira ticket with the label `ai-agent` and this service fixes it: it clones the repo, has an LLM (via [Aider](https://aider.chat)) make the change, pushes a branch, opens a pull request with a written description, and comments the PR link back on the ticket. Everything runs on free tiers (Jira Free, GitHub Free, Render Free, Groq).

```
Jira ticket (label ai-agent)
   │ webhook
   ▼
POST /jira-webhook ──queue──▶ CodingAgent
                              ├─ fetch ticket (Jira REST)              → comment "picked this up"
                              ├─ git clone, branch ai/KEY-title
                              ├─ aider: edit files named in the ticket (diff mode, no shell commands)
                              ├─ restore original line endings
                              ├─ LLM writes PR description from the real diff
                              ├─ commit + push
                              ├─ open draft PR (GitHub REST)
                              └─ comment PR link + summary on the ticket
```

## Writing a ticket the agent can handle

- **Add the label `ai-agent`.** Without it the agent ignores the ticket.
- **Name the file(s)** to change, e.g. `` In `Crimes in India Dashboard.py`, ... ``. Named files are given to the agent directly.
- **Say what should happen**, not just what's wrong. Add "Don't change anything else" to keep the PR small.
- Keep one change per ticket.

Example:
> **Summary:** Replace deprecated Dash imports
> **Description:** In `Crimes in India Dashboard.py`, replace `import dash_core_components as dcc` and `import dash_html_components as html` with `from dash import dcc, html`. Don't change anything else.

## When does it run?

| Jira action | Agent |
|---|---|
| Ticket created with `ai-agent` | Runs |
| `ai-agent` label added to an existing ticket | Runs |
| Any other edit or comment | Ignored (so its own comments can't re-trigger it) |
| Ticket already has an open agent PR | Skipped, with a Jira comment linking the PR |

**To rerun a ticket:** close or merge its PR, then remove and re-add the `ai-agent` label.

## Layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: webhook signature check, trigger rules, dedupe, single-worker queue |
| `app/agent.py` | The job: clone → Aider → commit → push → PR → Jira comments |
| `app/pr_writer.py` | LLM-written PR description (Summary / Changes / How to verify) with a plain fallback |
| `app/jira_client.py` | Get ticket, add comment |
| `app/github_client.py` | Default branch, find open PR for a ticket, create PR |
| `app/config.py` | Settings from environment variables |
| `scripts/check_setup.py` | Verify Jira, GitHub and Groq credentials and model (read-only, prints no secrets) |
| `scripts/send_test_webhook.py` | Send a signed, Jira-shaped webhook for a ticket |
| `docs/known-issues.md` | Limitations, past fixes, troubleshooting |

## Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored; never commit it or paste it anywhere.

| Variable | Meaning |
|---|---|
| `JIRA_BASE_URL` | e.g. `https://yourname.atlassian.net` |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | Atlassian login email + API token (id.atlassian.com → Security → API tokens) |
| `GITHUB_TOKEN` | Fine-grained token, target repo only: Contents + Pull requests = Read and write |
| `GITHUB_REPO` | Target repo as `owner/repo` |
| `GROQ_API_KEY` | Groq API key (or another provider's key, matching `AIDER_MODEL`) |
| `AIDER_MODEL` | e.g. `groq/openai/gpt-oss-120b`. Also used to write PR descriptions. |
| `AIDER_EDIT_FORMAT` | `diff` (default, edits only needed lines) or `whole` |
| `WEBHOOK_SECRET` | Random string; must match the Secret in the Jira webhook |
| `TRIGGER_LABEL` | Default `ai-agent` |
| `OPEN_DRAFT_PR` | Default `true` |

Check everything with:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/check_setup.py
```

## Run locally

Easiest with Docker (includes Aider and git):

```bash
docker build -t jira-agent .
docker run --env-file .env -p 8000:8000 jira-agent
# in another terminal, for an existing ticket with the ai-agent label:
.venv/bin/python scripts/send_test_webhook.py CA-1
```

Jira can't reach your laptop, so locally you trigger tickets with `send_test_webhook.py`.

Tests (no credentials needed): `.venv/bin/python -m pytest -q`

## Deploy to Render (free)

1. Push this repo to GitHub.
2. Render → **New → Web Service** → pick the repo → Runtime **Docker** → Instance **Free**.
3. Environment → **Add from .env** → paste your `.env`.
4. Advanced → Health Check Path: `/health`.
5. Deploy. Every push to `main` redeploys automatically.

Current deployment: `https://jira-agent-r9w8.onrender.com`

## Connect Jira

Jira ⚙ → **System → WebHooks → Create a WebHook**:

| Field | Value |
|---|---|
| URL | `https://jira-agent-r9w8.onrender.com/jira-webhook` (no Secret field? append `?token=<WEBHOOK_SECRET>`) |
| Secret | your `WEBHOOK_SECRET` |
| Events | Issue → **created** and **updated** |
| JQL | `project = CA AND labels = ai-agent` |

## Limits and troubleshooting

See [docs/known-issues.md](docs/known-issues.md). In short: Render sleeps when idle (first ticket takes 30–60s longer), queued jobs are lost on restart, free LLM tiers are rate-limited and models get retired, and the GitHub token expires. Always review the PR before merging.
