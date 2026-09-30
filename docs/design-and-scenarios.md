# Design, scenarios and decisions

This document explains the Jira coding agent from the outside in: the overall flow, what happens in every situation it can meet, and why it was built the way it was, including the alternatives that were considered and rejected.

- For a step-by-step walkthrough of a single run and the code behind it, see [how-it-works.md](how-it-works.md).
- For limits and troubleshooting, see [known-issues.md](known-issues.md).
- For logs and the `/jobs` page, see [observability.md](observability.md).

---

## 1. The goal and the constraints

**Goal:** when someone creates a Jira ticket describing a bug or a small task, a coding agent deployed in the cloud picks it up, gets access to the repository, makes the change, and raises a pull request, without a human writing the code.

**Constraints:**
- **Everything free:** ticketing, hosting, the AI model and the code hosting.
- **Safe by default:** the agent must never merge code itself, never trigger itself in a loop, and never leak secrets.
- **Understandable:** a reviewer must be able to see what the agent did and why.

---

## 2. The flow in one picture

```
 You                Jira Cloud (Free)          Agent on Render (Free)                 Groq (Free)        GitHub
  │ create ticket       │                              │                                  │                │
  │ + label ai-agent ──▶│── webhook (signed) ─────────▶│ 1. verify signature               │                │
  │                     │                              │ 2. is it a trigger? queue it       │                │
  │                     │◀── label ai-agent-queued ────│    (queue saved in Jira)           │                │
  │                     │◀── comment + In Progress ────│ 3. fetch ticket, check open PRs ───────────────────▶│
  │                     │                              │ 4. clone repo, new branch  ◀───────────────────────│
  │                     │                              │ 5. Aider: ticket + code ──────────▶│ edits          │
  │                     │                              │    apply edits  ◀─────────────────│                │
  │                     │                              │ 6. syntax check (1 auto-fix)       │                │
  │                     │                              │ 7. PR description ────────────────▶│                │
  │                     │                              │ 8. commit, push, open draft PR ───────────────────▶│
  │                     │◀── comment + In Review ──────│ 9. report back, remove queue label │                │
  │ review & merge the PR ───────────────────────────────────────────────────────────────────────────────▶│
```

Typical run: **about a minute** (plus up to a minute if the free Render service was asleep).

The three moving parts:

| Part | What it is | Its job |
|---|---|---|
| **Jira + webhook** | Jira Cloud Free, a webhook filtered to `project = CA AND labels = ai-agent` | Tells the agent "there's work" |
| **The agent** | A Python FastAPI service in Docker on Render Free (`app/`) | Orchestrates everything: security, queue, Git, GitHub, Jira, checks, logs |
| **Aider + LLM** | Aider (open-source CLI) calling `openai/gpt-oss-120b` on Groq's free tier | Decides what to change in the code and applies the edit |

The key idea: **the agent is the orchestrator, and Aider is only the "hands on the keyboard"**. Everything around the code edit (who may trigger it, where it runs, how it's checked, how it's reported) is our own code.

---

## 3. What happens in each scenario

### 3.1 Normal cases

| Scenario | What the agent does | What you see |
|---|---|---|
| **New ticket with the `ai-agent` label** | Full run: queue, clone, Aider, check, PR, report. | Ticket: To Do → In Progress → In Review. Comments: "picked this up", then the PR link and "What changed". A draft PR with Summary / Changes / How to verify / Checks. |
| **Label added to an existing ticket** | Same as a new ticket. The webhook's changelog shows `ai-agent` was *just added*. | Same as above. |
| **Several tickets at once** | Queued and processed **one at a time** (free instance: 512 MB RAM, rate-limited LLM). | Later tickets wait in To Do with the `ai-agent-queued` label, then run in order. |
| **Ticket names a file** (e.g. `` `Crimes in India Dashboard.py` ``) | That file is handed to Aider directly, and Aider's repo map is skipped. | Faster, smaller request, more accurate edits. |
| **Ticket names no file** | Aider uses its **repo map** (a compact summary of the code) to find where to work. | Works for small repos; less precise on big ones. |

### 3.2 Cases where the agent deliberately does nothing

| Scenario | Why it's ignored | Where it's recorded |
|---|---|---|
| **Ticket without the `ai-agent` label** | Only labelled tickets are for the agent (and the Jira webhook filter doesn't even send most of them). | `/jobs` → webhooks: `ignored`, "missing label" |
| **Edits, comments, status changes on a ticket** | Only *creation* or *newly added label* are triggers. Otherwise the agent's own comments and status moves would re-trigger it forever. | `/jobs`: `ignored event 'jira:issue_updated'` |
| **Same webhook delivered twice** | The ticket is already queued or running (`_in_flight`). | `/jobs`: `already queued` |
| **Ticket already has an open agent PR** | Avoids duplicate PRs. Matched by ticket key, so renaming the ticket doesn't fool it. | Jira comment "Skipped: a pull request for this ticket is already open", with the link |
| **Label removed before the job starts** | The label is re-checked when the job starts. | `/jobs`: `skipped` |
| **Request with a wrong or missing secret** | Only your Jira knows `WEBHOOK_SECRET`; anyone else gets **401**. | Log warning "Webhook rejected"; `/jobs`: `rejected` |

### 3.3 Cases where the agent tries but the result isn't a clean PR

| Scenario | What the agent does | What you see / do |
|---|---|---|
| **Vague ticket** ("improve the code") | The model makes no edits. | Comment "finished but made no changes" with the model's own words; ticket back to **To Do**. Make the ticket specific (file, expected behaviour) and re-add the label. |
| **The model's edit breaks Python syntax** | The syntax check catches it and gives Aider **one** fix attempt with the exact error. | Usually: PR with "✅ Python syntax … (the agent fixed a syntax error in its first attempt)". |
| **Syntax still broken after the fix attempt** | The PR is still opened (so the attempt is visible), but clearly flagged. | PR Checks: ❌ with the errors and "Do not merge"; Jira comment "Checks failed". |
| **Ticket needs a big rewrite** (e.g. replace a 100-line function) | Groq's free tier allows ~8,000 tokens per request (prompt + reply). A long reply gets cut off, so no edit is applied. | Comment "made no changes" with `hit a token limit`. Split the work into smaller, additive tickets, or use a model with higher limits. |
| **Ticket mentions a big data file** (e.g. a 1 MB CSV) | Files over 200 KB are hidden from Aider via a generated `.aiderignore`, so they aren't sent to the model. | Works normally; the ticket should describe the data (column names), since the model can't read the file. |
| **PR description can't be generated** (rate limit, timeout) | Falls back to a plain description. | PR still opens, with the diff and agent log. |
| **Any error** (expired token, network failure, Git error, Aider timeout after 15 min) | Caught; the job is marked failed. | Comment "Coding agent failed" with the (secret-free) error; ticket back to **To Do**; details in Render logs. |

### 3.4 Infrastructure cases

| Scenario | What happens |
|---|---|
| **Render service asleep** (free tier sleeps after ~15 min idle) | The webhook wakes it; the first run takes 30–60 s longer. |
| **Render restarts mid-queue or mid-job** (deploy, crash) | The `ai-agent-queued` label stays on the ticket. **2 minutes** after startup, the agent searches Jira for that label and re-queues those tickets from scratch. |
| **Webhook arrives while the service is fully down or mid-deploy** | It can be lost: the agent never received it, so nothing is saved. Re-add the label, or replay with `scripts/send_test_webhook.py`. |
| **Rerunning a ticket** | Close its PR, then remove and re-add the label. The agent force-pushes over its own old `ai/…` branch. |
| **Two agent PRs touch the same lines** | Each PR is fine alone, but whichever you merge second may need a small conflict fix. |
| **Jira status missing** (e.g. no "In Review" column) | The move is skipped with a warning listing the available statuses; the job still succeeds. |
| **Groq retires the model** | Runs fail. `scripts/check_setup.py` lists the models available; change `AIDER_MODEL` on Render. |
| **GitHub token expires** | Clone/push fails with an error comment. Create a new token and update `GITHUB_TOKEN` on Render. |
| **Private target repo on GitHub Free** | Draft PRs aren't allowed there; the agent retries as a normal PR automatically. |

---

## 4. The options we had, layer by layer

### 4.1 How Jira tells the agent about a ticket

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Jira webhook** (system setting) | Instant, free, unlimited, can be signed with a secret, filterable by JQL | Needs a public URL | ✅ **Chosen** |
| Jira Automation "send web request" | Point-and-click rules | The free plan limits monthly rule runs | ❌ |
| Agent polls Jira every N minutes | No public URL needed | Delay; wastes requests; a sleeping free service can't poll | ❌ |

### 4.2 Where the agent runs

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Render free web service (Docker)** | Deploys from GitHub on every push, HTTPS URL, logs UI, zero server admin | Sleeps when idle; 512 MB RAM; disk wiped on restart | ✅ **Chosen**: simplest "deployed service" |
| Oracle Cloud Always Free VM | Always on, much more RAM | You manage the server (OS updates, HTTPS, restarts) | Upgrade path if Render gets too small |
| Cloudflare Worker + GitHub Actions | No sleeping; Actions runners are powerful | Two systems to wire up; less of a "deployed agent" | Good alternative for heavier jobs |
| Hugging Face Spaces / Fly.io / Railway | Similar PaaS experience | Free tiers are smaller or have changed often | ❌ |

### 4.3 Which LLM

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Groq free tier, `gpt-oss-120b`** | Very fast, OpenAI-compatible, strong open model | ~8k tokens per request on the free tier; models get retired | ✅ **Chosen** |
| Google Gemini free tier | Much larger free limits | Free-tier prompts may be used for training | Best switch if bigger tickets are needed (just change `AIDER_MODEL`) |
| OpenRouter `:free` models | Many models behind one key | Low daily request caps; quality varies | Backup |
| Local model (Ollama) | Fully private, no limits | Needs a machine with a GPU running 24/7, not free to host | ❌ for a cloud agent |

Because Aider talks to models through **litellm**, switching provider is a configuration change (`AIDER_MODEL` plus that provider's API key), not a code change.

### 4.4 The coding engine: the most important decision

This is the part that turns "a ticket" into "an edited file". The options:

| Option | What it is | Why not / why |
|---|---|---|
| **Aider** | Open-source AI pair-programming CLI | ✅ **Chosen**: see below |
| Build our own | Our code sends files to the LLM, parses the reply, applies edits | Weeks of work to reach Aider's reliability (see 4.5) |
| OpenHands | Open-source autonomous agent that runs commands and tests in a sandbox | Much heavier (sandbox containers, more RAM than the free tier offers); more autonomy than a "small ticket → PR" flow needs |
| SWE-agent | Research agent built for fixing GitHub issues | Built for benchmarks and research; heavier setup; best with strong paid models |
| Claude Code, GitHub Copilot coding agent, OpenAI Codex | Commercial coding agents | Very capable, but **paid**, which breaks the "everything free" constraint |

### 4.5 Why Aider instead of building our own AI coding agent

At first glance, "send the code to the LLM and write back what it says" sounds like 50 lines. In practice, a reliable coding agent has to solve these problems:

| Problem | What Aider already does | What we'd have to build |
|---|---|---|
| **Choosing what code the model sees** | Builds a **repo map** (a compact, ranked summary of functions and classes) and adds named files to the context | Our own code indexing, token budgeting and file selection |
| **Getting edits in a reliable format** | Prompts tuned per model for **search/replace blocks** (`diff`), whole files, or unified diffs | Prompt design, testing across models |
| **Applying edits safely** | Matches the SEARCH text exactly; tolerates small whitespace differences; rejects edits that don't match and asks the model to retry | A patch engine with fuzzy matching and failure handling |
| **New files, renames, quoting, code fences** | Handled | Many edge cases, each found the hard way |
| **Many models and providers** | Built-in model settings plus litellm for 100+ providers | Per-provider client code and quirks |
| **Lint and repair loops** | Optional built-in lint and test-then-fix loops (`--auto-lint`, `--test-cmd`) | Our own feedback loops |

**What we gained:** the whole agent, with orchestration, security, Jira, GitHub, checks, logging and restart recovery, was built and tested in days, and the hard AI-editing part is maintained by an active open-source project.

**What it cost us.** Aider is designed for a human at the keyboard, so its defaults had to be tamed for unattended use. Every one of these was discovered in testing:

| Aider behaviour | Problem for an unattended agent | Our fix |
|---|---|---|
| Asks questions ("add this file?") | No human to answer | `--yes-always` |
| With `--yes-always`, auto-runs shell commands the model suggests | The model could start servers or run arbitrary code | `--no-suggest-shell-commands` |
| Auto-commits each edit | Messy history | `--no-auto-commits`; the agent makes one well-described commit |
| Falls back to whole-file rewrites for unknown models | Unrequested reformatting, noisy diffs | `--edit-format diff` |
| Only edits files "in the chat" | It missed a file with spaces in its name and gave up | The agent passes files named in the ticket explicitly |
| Adds every file mentioned in the ticket to the context | A 1 MB CSV became ~680k tokens | `.aiderignore` for files over 200 KB |
| Sends a repo map with every request | Wasted tokens under a tight free limit | `--map-tokens 0` when files are named |
| Writes history files into the repo | They'd end up in the PR | History files moved out; `.aider*` excluded from commits |

**When building our own would make sense:** at a larger scale, or if we needed full control over prompts, costs and behaviour (e.g. company-specific rules, other languages' toolchains, or a custom multi-step planning loop). The rest of the agent wouldn't change: Aider is called in one place (`_run_aider` in `app/agent.py`), so it could be swapped for another engine later.

### 4.6 Keeping the queue through restarts

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| In-memory only | Simplest | Restart = lost tickets | Was v1; replaced |
| Redis (e.g. Upstash free) or a database | Proper queue | Another account, service and secret to manage | ❌ for now |
| **Jira itself, as a label** (`ai-agent-queued`) | No new service; visible on the ticket; survives any restart | Only covers tickets the agent received; relies on Jira search | ✅ **Chosen** |

### 4.7 Knowing what happened

| Option | Verdict |
|---|---|
| **Structured logs** in Render (job-tagged lines, step timings, token counts) | ✅ Chosen: free, already there |
| **`/jobs` endpoint** (recent jobs and webhooks, secret-protected) | ✅ Chosen: answers "what happened to ticket X?" in one request |
| **Jira comments** as an audit trail | ✅ Chosen: people see progress where they already work |
| External log/alert services (Sentry, Better Stack, UptimeRobot) | Free and useful, but need extra accounts; possible next step |

---

## 5. Safety design at a glance

| Risk | Protection |
|---|---|
| Someone else triggers the agent | Webhook signed with `WEBHOOK_SECRET` (HMAC), compared in constant time |
| The agent triggers itself | Only ticket creation or a newly added label trigger it |
| Broken code reaches `main` | PRs are **drafts**; a human merges; syntax check with a "Do not merge" flag on failure |
| The AI runs arbitrary commands | Shell-command suggestions disabled |
| Secrets leak | Stored only in Render env / local `.env`; GitHub token scrubbed from logs, errors and PR text; `token=***` in access logs |
| Too much access | Fine-grained GitHub token limited to the one target repo |
| Duplicate work | In-flight dedupe, open-PR check by ticket key |

---

## 6. What would change at a bigger scale

The full, prioritised list is in [roadmap.md](roadmap.md). The main points:

- **More than one worker**, and a real queue (Redis or a database) instead of the Jira label.
- **A paid or higher-limit LLM**, so bigger tickets fit.
- **Run the project's tests** before opening the PR (Aider's `--test-cmd`), not just a syntax check.
- **Multiple repos**, chosen from a Jira component or field.
- **Close the loop:** move the ticket to Done when its PR is merged.
- **Alerts** when a job fails or the service goes down.
- **Revise from review comments:** a reviewer comments on the PR and the agent updates it.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| **Webhook** | An HTTP request one system sends another when something happens (here: Jira → agent on ticket events). |
| **HMAC signature** | A code computed from the request body and a shared secret; proves the webhook came from Jira and wasn't altered. |
| **Aider** | Open-source command-line tool that uses an LLM to edit code in a Git repo. |
| **litellm** | Library giving one interface to 100+ LLM providers; used by Aider and by our PR-description step. |
| **Repo map** | Aider's compact summary of a codebase's files, functions and classes, sent to the model for context. |
| **Search/replace (`diff`) edit format** | The model replies with "find this exact text, replace it with this", so only the changed lines are touched. |
| **Tokens / TPM** | Units of text an LLM processes (~4 characters each). TPM = tokens per minute, a rate limit. |
| **Draft PR** | A pull request that can't be merged until someone marks it ready for review. |
| **Force push** | Overwriting a remote branch; safe here because `ai/…` branches belong only to the agent. |
