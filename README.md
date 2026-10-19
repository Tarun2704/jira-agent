# Jira Coding Agent

[![Tests](https://github.com/Tarun2704/jira-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/Tarun2704/jira-agent/actions/workflows/tests.yml)

Create a Jira ticket with the label `ai-agent` and this service fixes it: it clones the repo, has an LLM coding tool make the change ([Aider](https://aider.chat) by default, or the [OpenCode](https://opencode.ai) agent: a switch in the environment), pushes a branch, opens a pull request with a written description, and comments the PR link back on the ticket. Everything runs on free tiers (Jira Free, GitHub Free, Render Free, Groq).

**Understand the design:** [docs/design-and-scenarios.md](docs/design-and-scenarios.md) explains the full flow, what happens in every scenario, the alternatives considered, and why Aider was used instead of building a custom AI coding agent.

For a step-by-step explanation of what happens during a run, see [docs/how-it-works.md](docs/how-it-works.md). To compare Aider with the OpenCode agent on the same ticket, see [docs/opencode.md](docs/opencode.md).
Setting up on another computer: [docs/setup-new-machine.md](docs/setup-new-machine.md).

```
Jira ticket (label ai-agent)
   │ webhook
   ▼
POST /jira-webhook ──queue──▶ CodingAgent
                              ├─ fetch ticket (Jira REST)              → comment "picked this up"
                              ├─ git clone, branch ai/KEY-title
                              ├─ coding tool edits the files (CODING_BACKEND):
                              │    aider    - edit the files named in the ticket (diff mode, no shell commands)
                              │    opencode - an agent explores the repo and edits (no shell by default)
                              │    (either way the tool only edits files: this app does every commit and push)
                              ├─ restore original line endings
                              ├─ syntax-check changed files (one automatic fix attempt on errors)
                              ├─ LLM writes PR description from the real diff
                              ├─ commit + push
                              ├─ open draft PR (GitHub REST)
                              └─ comment PR link + summary on the ticket
```

## Writing a ticket the agent can handle

- **Add the label `ai-agent`.** Without it the agent ignores the ticket.
- **Name the file(s)** to change, e.g. `` In `Crimes in India Dashboard.py`, ... ``. Named files are given to the agent directly.
- **Say what should happen**, not just what's wrong. Add "Don't change anything else" to keep the PR small.
- **Keep it small and additive** (a new function or file, a few changed lines). The free Groq tier allows ~8,000 tokens per request, so big rewrites get cut off.
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

While it works, the agent moves the ticket **To Do → In Progress → In Review** (or back to **To Do** if it failed or changed nothing), and marks queued tickets with the label `ai-agent-queued` so they're resumed after a restart. Details in [docs/how-it-works.md](docs/how-it-works.md).

**To rerun a ticket:** close or merge its PR, then remove and re-add the `ai-agent` label. Step-by-step instructions, including cleanup, are in [docs/operations.md](docs/operations.md).

## Layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: webhook signature check, trigger rules, dedupe, single-worker queue |
| `app/agent.py` | The job: clone → coding tool (Aider or OpenCode) → commit → push → PR → Jira comments |
| `app/opencode_runner.py` | The OpenCode backend: config, scrubbed environment, process control, event parsing |
| `app/checks.py` | Checks run before the PR (Python syntax), shown as a **Checks** section in the PR |
| `app/pr_writer.py` | LLM-written PR description (Summary / Changes / How to verify) with a plain fallback |
| `app/jira_client.py` | Get ticket, add comment |
| `app/github_client.py` | Default branch, find open PR for a ticket, create PR |
| `app/jobs.py` | Observability: job history for `/jobs`, step timings, token counts, job-tagged logs |
| `app/config.py` | Settings from environment variables |
| `scripts/check_setup.py` | Verify Jira, GitHub and Groq credentials and model (read-only, prints no secrets) |
| `scripts/check_opencode.py` | Smoke-test the OpenCode backend with your real model and key (a few cents, no Jira/GitHub) |
| `scripts/send_test_webhook.py` | Send a signed, Jira-shaped webhook for a ticket |
| `docs/how-it-works.md` | The flow step by step: what each step does and why |
| `docs/observability.md` | Logs and `/jobs`: where to look and what each line means |
| `docs/opencode.md` | The OpenCode backend: how a run works, safety, costs, Render sizing, troubleshooting |
| `docs/known-issues.md` | Limitations, past fixes, troubleshooting |
| `docs/planner-comparison.md` | Aider vs OpenCode as the planner, measured on a small and a big repo; which to pick |
| `docs/e2e-test-report.md` | End-to-end run of both backends with an injected fault at every stage: what happened, what was fixed |
| `docs/todo.md` | Open follow-ups: what to do before the next deploy, known gaps, next improvements |
| `docs/setup-new-machine.md` | Step-by-step setup on another laptop |
| `docs/design-and-scenarios.md` | Full flow, behaviour in every scenario, options considered, and why Aider |
| `docs/operations.md` | Runbook: run a ticket, run it again, fix a stuck ticket, clean up after tests or a demo |
| `docs/roadmap.md` | What it would take to make this a production tool, in priority order |

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
| `CODING_BACKEND` | `aider` (default) or `opencode`: who edits the files. Planning, PR text, git, Jira and Slack don't change. See [docs/opencode.md](docs/opencode.md) |
| `PLANNER_BACKEND` | `opencode` (default) or `aider`: who reads the code and writes the plan (independent of `CODING_BACKEND`). OpenCode searches file contents: right on big repos, ~3× the requests; Aider is cheaper on small repos. See [docs/planner-comparison.md](docs/planner-comparison.md) |
| `OPENCODE_MODEL` | Default `google/gemini-2.5-flash` (`provider/model`; uses your `GEMINI_API_KEY`). Only used with `CODING_BACKEND=opencode` |
| `OPENCODE_FALLBACK_MODEL`, `OPENCODE_MAX_STEPS`, `OPENCODE_TIMEOUT_SECONDS`, `OPENCODE_ALLOW_BASH`, `OPENCODE_VARIANT`, `OPENCODE_PASS_ENV`, `OPENCODE_EXTRA_CONFIG`, `OPENCODE_BIN` | OpenCode tuning: see [docs/opencode.md §3](docs/opencode.md) (shell access is **off** by default) |
| `TOOL_FALLBACK` | `true` (default): when OpenCode itself fails (crash, out of memory, stuck, timeout), Aider does that step instead |
| `JOB_STUCK_SECONDS` | Default 420: a running job with no sign of life this long is stopped by the watchdog, then falls back to Aider (OpenCode) or runs once more by itself before it's reported as failed. 0 = off |
| `WEBHOOK_SECRET` | Random string; must match the Secret in the Jira webhook |
| `TRIGGER_LABEL` | Default `ai-agent` |
| `QUEUE_LABEL` | Default `ai-agent-queued`; marks queued/running tickets so they survive restarts (empty = off) |
| `RECOVERY_DELAY_SECONDS` | Default `120`; wait after startup before resuming queued tickets |
| `JIRA_STATUS_IN_PROGRESS` / `JIRA_STATUS_IN_REVIEW` / `JIRA_STATUS_ON_FAILURE` / `JIRA_STATUS_DONE` | Defaults `In Progress` / `In Review` / `To Do` / `Done` (PR merged); empty = don't move |
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

With Slack and Redis configured, every ticket gets a plan first and waits for your approval in Slack: see [docs/plan-first.md](docs/plan-first.md) (includes running it locally with a tunnel).

Tests (no credentials needed): `.venv/bin/python -m pytest -q`. They also run on GitHub Actions on every push (`.github/workflows/tests.yml`); see the badge at the top.

## Deploy to Render (free)

1. Push this repo to GitHub.
2. Render → **New → Web Service** → pick the repo → Runtime **Docker** → Instance **Free**.
3. Environment → **Add from .env** → paste your `.env`.
4. Advanced → Health Check Path: `/health`.
5. Deploy. Every push to `main` redeploys automatically.

Current deployment: `https://jira-agent-r9w8.onrender.com`

> **OpenCode needs more memory than the Free plan has.** One run peaks near 550 MB; the Free instance has 512 MB. Keep `CODING_BACKEND=aider` on Free, or move to an instance with 1 GB+ (2 GB is comfortable) to use OpenCode. `/health` shows a warning under `problems` if the container is too small. To make Render deploy only after the tests pass, use its "deploy after CI checks pass" auto-deploy option; the tests run in GitHub Actions. Details: [docs/opencode.md §6](docs/opencode.md).

## Connect Jira

Jira ⚙ → **System → WebHooks → Create a WebHook**:

| Field | Value |
|---|---|
| URL | `https://jira-agent-r9w8.onrender.com/jira-webhook` (no Secret field? append `?token=<WEBHOOK_SECRET>`) |
| Secret | your `WEBHOOK_SECRET` |
| Events | Issue → **created** and **updated** |
| JQL | `project = CA AND labels = ai-agent` |

## Logs and job history

- **Logs:** Render → service → **Logs**. Every webhook is logged with its outcome and reason; every job line is tagged like `[CA-3#1a2b3c4d]` and ends with a `Job finished` summary (status, step timings, tokens, PR).
- **Job history:** `https://jira-agent-r9w8.onrender.com/jobs?token=<WEBHOOK_SECRET>` (add `&issue=CA-3` to filter). Resets on each deploy.

Details: [docs/observability.md](docs/observability.md).

## Limits and troubleshooting

See [docs/known-issues.md](docs/known-issues.md). In short: Render sleeps when idle (first ticket takes 30–60s longer), queued jobs are lost on restart, free LLM tiers are rate-limited and models get retired, and the GitHub token expires. Always review the PR before merging.
