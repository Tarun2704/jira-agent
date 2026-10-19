# The OpenCode coding backend

The service can do the **coding step** with either of two tools. You pick one with a single environment variable and can switch back and forth to compare them on the same ticket.

| `CODING_BACKEND` | Tool | What it is |
|---|---|---|
| `aider` (default) | [Aider](https://aider.chat) | One model call that returns edits. You tell it which files to look at. Cheap and predictable. |
| `opencode` | [OpenCode](https://opencode.ai) | A coding **agent**: it searches and reads the repo itself, edits files, and (if you allow it) runs commands. Takes several model requests per ticket. |

Only the *coding* step changes. Planning (`app/planner.py`), the PR description, git (clone, branch, commit, push), the PR, Jira, Slack, queueing and cost records are identical for both backends. **Code changes are made by the tool; every commit and push is made by this app's code, never by the tool.**

> The planner and the PR description still use Aider's model settings (`AIDER_MODEL`, `PLANNER_MODEL`) and the `aider-chat` package, whatever `CODING_BACKEND` is. So keep Aider installed and `AIDER_MODEL` set even when you test OpenCode.

---

## 1. Compare the two on the same ticket

1. Set `CODING_BACKEND=aider` (Render → Environment, or `.env` locally). Restart/redeploy.
2. Run the ticket ([operations.md §1](operations.md)). Note the PR and open `/usage/CA-5?token=<WEBHOOK_SECRET>`.
3. Close that PR ([operations.md §2](operations.md)), set `CODING_BACKEND=opencode`, redeploy.
4. Remove and re-add the `ai-agent` label. A new PR appears, made by OpenCode.
5. Compare: the two PRs (did it solve the ticket? how big is the diff?), and `/usage/CA-5` for each (requests, tokens, cost). `/runs/CA-5` shows what each tool was asked and answered.

What to look at in `/usage/<KEY>` (each run is under `rounds[].jobs[].calls[]`):

| Field | Meaning |
|---|---|
| `step` | `code`, `code_retry`, `code_fallback`, `fix` (a syntax-fix attempt), `pr_description`, planning steps |
| `backend` | `opencode` for OpenCode calls (absent for Aider's) |
| `sent` / `received` | Tokens in / out. For OpenCode this is **per model request**: you'll see several entries for one coding step. |
| `cache_hit`, `cache_write` | Cached prompt tokens. Counted as Aider reports them: cache writes are *included* in `sent`, cache hits are not. |
| `reported_cost` | OpenCode's own cost figure, using its built-in price list and cache discounts. Used when the model has no price in this app. |
| `cost_usd` | This app's estimate from its built-in prices or `MODEL_PRICES` (see below) |

**Prices.** Prices are looked up by the model's name, and OpenCode's names (`anthropic/claude-opus-5-5`) differ from Aider's (`claude-opus-5-5`). Both spellings of the usual models are built in (`DEFAULT_PRICES` in `app/costs.py`). For any other model, list both names in `MODEL_PRICES`, or the total shows as "unknown":

```
MODEL_PRICES={"gemini/gemini-2.5-flash":[0.30,2.50],"google/gemini-2.5-flash":[0.30,2.50]}
```

(Use the real prices for your model.) With two numbers, cached tokens are billed at the input price (an upper bound); give four numbers `[input, output, cache_write, cache_hit]` to price them exactly.

**Expect OpenCode to cost more per ticket** (several requests, each carrying the growing conversation) and to use far more requests, which matters on free tiers with request-per-minute limits. What you are buying is that it finds the right files itself and can check its own work. For a ticket that names its file and asks for a small change, Aider is usually enough; OpenCode earns its cost on vaguer tickets.

---

## 2. How an OpenCode run works, step by step

This replaces **Step 10 and Step 11** of [how-it-works.md](how-it-works.md). Everything before (clone, branch, plan) and after (line endings, checks, commit, push, PR) is unchanged.

**Step 10': Pick hint files.** Same function as for Aider (`mentioned_files()` plus the files in the approved plan). For OpenCode they are only *hints* ("start with these files"): it can open any file itself. If there are none, it searches the repo.

**Step 11': Run OpenCode** (`CodingAgent._run_opencode()` in `app/agent.py`, using `app/opencode_runner.py`):

1. **Prepare a private folder** `/tmp/jira-agent/<KEY>-opencode/` as OpenCode's home (its session history lives there). It is deleted afterwards, so tickets can't leak into each other. A shared cache folder (`/tmp/jira-agent/.opencode-cache`) keeps the model list so it isn't downloaded every time.
2. **Build a scrubbed environment.** The agent process gets only: `PATH`, its private `HOME`, and the API key for *its model's provider* (e.g. `GEMINI_API_KEY` for `google/...`). It does **not** get `GITHUB_TOKEN`, `JIRA_API_TOKEN`, Slack, Redis or webhook secrets. (`OPENCODE_PASS_ENV` can add more variable names if a custom provider needs them.)
3. **Hide the GitHub token from the clone.** The clone's `origin` URL contains the token. For the run it is replaced by the plain URL, so `git remote -v` or reading `.git/config` shows nothing secret. The original `.git/config` is saved first.
4. **Build the OpenCode config** (passed inline as `OPENCODE_CONFIG_CONTENT`, so nothing is written into the repo): file edits allowed; **shell commands denied** unless `OPENCODE_ALLOW_BASH=true`; web fetch, sub-agents and "ask the user" denied; file tools restricted to the repo; auto-update, sharing, snapshots, language servers and formatters off. A repo's own `opencode.json` is ignored. If shell access is on, `git commit/push/add/checkout/reset/config/...` are denied by name.
5. **Run** `opencode run --model <model> --format json "<prompt>"` as a child process in its own process group, with stdin closed (an open stdin makes OpenCode wait for input forever), under a timeout (`OPENCODE_TIMEOUT_SECONDS`, default 15 min) and a cap on model requests (`OPENCODE_MAX_STEPS`, default 40). The prompt is the same ticket prompt Aider gets, plus a few rules (don't touch git, don't leave scratch files, summarize at the end).
6. **Read its event stream as it runs.** OpenCode prints one JSON event per line: each tool call, each piece of assistant text, each finished model request with its token counts, and errors. The app turns them into (a) token records, one per model request, and (b) a short text log: `[opencode]` header, `[tool] …` lines, `[said]` + the agent's final message, `[error] …`, `[stopped] …`. That log goes where Aider's output used to go (PR text, "no changes" comment, `/runs`).
7. **Clean up, always** (even on error or timeout): kill the process group (so a test run or server the agent started dies with it); **restore `.git/config` byte for byte** (which also puts the token URL back for the app's own push); **delete `.git/hooks`**; delete the private folder.
8. **Repair git.** If the agent committed anyway (e.g. `sh -c 'git commit …'` slips past name-based denial), the app moves `HEAD` back so that work becomes ordinary uncommitted changes, and puts the repo back on its own branch if the agent switched. A warning is added to the job. Result: the PR always has exactly one commit, made by the app.
9. Back in the shared flow: line endings are restored, `git add -A`, syntax checks (one automatic fix attempt, using OpenCode again), PR description, **the app commits and pushes**, the PR is opened.

### What the run can end as

| Outcome | What the app does |
|---|---|
| Edits made, no errors | Normal PR. |
| Agent says "nothing needs changing" | Same as Aider: a comment with the agent's explanation. |
| Provider error (rate limit, bad key, quota) and **no edits** | Same handling as Aider: wait and retry once (`RATE_LIMIT_WAIT_SECONDS`), or use `OPENCODE_FALLBACK_MODEL`; otherwise a failed job with a plain-words reason. OpenCode already retries rate limits itself (with backoff, for a minute or more) before giving up. |
| Provider error **after** some edits | The PR is still opened, with a warning that the change may be incomplete. |
| Step cap reached | Edits so far are kept, with a warning; if there were none, a failed job that names `OPENCODE_MAX_STEPS`. |
| Timeout | Failed job ("OpenCode timed out after N s"). |
| `opencode` not installed | Failed job telling you to install it or set `CODING_BACKEND=aider`. |

---

## 3. Settings

| Variable | Default | Meaning |
|---|---|---|
| `CODING_BACKEND` | `aider` | `aider` or `opencode` |
| `OPENCODE_MODEL` | `google/gemini-2.5-flash` | `provider/model`. `google/…` uses your existing `GEMINI_API_KEY` (the app also hands it to OpenCode as `GOOGLE_GENERATIVE_AI_API_KEY`, the only name Google's SDK reads); `openrouter/…` uses `OPENROUTER_API_KEY`; `openai/…` uses `OPENAI_API_KEY`; `anthropic/…` uses `ANTHROPIC_API_KEY`. |
| `OPENCODE_FALLBACK_MODEL` | empty | Tried once if the main model is rate-limited or down (the key must be available too, or pass it with `OPENCODE_PASS_ENV`). |
| `OPENCODE_ALLOW_BASH` | `false` | `true` lets the agent run shell commands. See §4 before turning it on. |
| `OPENCODE_MAX_STEPS` | `40` | Stop after this many model requests (cost bound). `0` = no cap. |
| `OPENCODE_TIMEOUT_SECONDS` | `900` | Hard time limit per run. |
| `OPENCODE_VARIANT` | empty | Provider-specific reasoning effort (e.g. `low`, `high`), passed as `--variant`. |
| `OPENCODE_PASS_ENV` | empty | Extra environment variable *names* to hand to the agent (comma separated). |
| `OPENCODE_EXTRA_CONFIG` | empty | JSON merged into the OpenCode config (e.g. a custom provider). |
| `OPENCODE_BIN` | `opencode` | Path to the executable. |
| `OPENCODE_CHAT_MODEL` | empty | Cheaper model that answers questions about a plan (e.g. `google/gemini-3.1-flash-lite`); when it says the plan should change, `OPENCODE_PLANNER_MODEL`/`OPENCODE_MODEL` does the turn instead. |
| `TOOL_FALLBACK` | `true` | When OpenCode itself fails (crashed or killed for memory, stuck, timed out, couldn't start, or used up its requests without changing anything), Aider does that planning or coding step instead, if Aider is installed (the Docker image has it). Its half-done edits are undone first. Model problems (rate limits, quotas) use the backup model instead. |
| `PLANNER_BACKEND` | `opencode` | Who writes the plan: `opencode` (read-only run that searches the code; right on big repos, ~3× the requests) or `aider` (~2 requests; finds files by name, fine on small repos). Independent of `CODING_BACKEND`. See [planner-comparison.md](planner-comparison.md). |
| `OPENCODE_PLANNER_MODEL` | empty | `provider/model` for the OpenCode planner. Empty = `OPENCODE_MODEL`. Its fallback is `OPENCODE_FALLBACK_MODEL`. |
| `OPENCODE_PLAN_MAX_STEPS` | `20` | Model requests one OpenCode plan may use. |

**Choosing a model:** an agent depends on reliable tool calling. Small or free-tier models often fail at it (malformed edits, giving up early); a mid-size model such as Gemini 2.5 Flash is the practical minimum. If OpenCode flails on a free model, try a better one before judging the tool.

---

## 4. What the agent can and can't do (and the risks left)

| | Allowed? |
|---|---|
| Read, search and edit files in the clone | Yes |
| Run shell commands | **No** by default (`OPENCODE_ALLOW_BASH=true` to allow) |
| `git commit`, `push`, branch changes | No (denied by name, and undone afterwards if it slips through) |
| See GitHub/Jira/Slack/Redis secrets | No, with shell access off (scrubbed environment, token removed from the remote). With `OPENCODE_ALLOW_BASH=true`: yes, via `/proc` (see below). |
| See its model provider's API key | Yes (it needs it). It is blanked out of anything the app stores or posts. |
| Use web fetch, sub-agents | No |

**Why shell access is off by default.** A Jira ticket is untrusted text, and an agent that can run commands can be talked into running them ("also run `curl … | sh`"). The protections above (scrubbed environment, token-free remote, git repair) cut the damage, but they are not a sandbox:

- Name-based command denial is a guard rail for the model, not a security boundary.
- An agent with a shell can read other files in the container, including other jobs' clones in `/tmp/jira-agent/`, and can make network requests. (The planner's clone no longer holds the GitHub token: it is removed right after cloning.)
- It runs as the same user as the app, so `cat /proc/$PPID/environ` would show the **app's** full environment, every secret included. The scrubbed environment only protects the agent's own process.
- On Render, the service's dependencies are the app's, not the target repo's, so most test suites **can't run there anyway** (see §6). Shell access adds risk for little gain.

Turn it on only for repos and tickets you trust, and prefer a separate, secret-free container for it (see [roadmap.md](roadmap.md) and the production notes below).

**Moving toward production:** run the agent in its own sandbox (a separate container or service with no app secrets and restricted network egress; a hosted sandbox service such as E2B, or a Docker-based runner you operate, are the usual choices) and send it the repo and prompt, getting back the diff. `run_opencode()` is the only function that would need to change, because everything it takes in and returns is plain data.

---

## 5. Installing OpenCode

**On Render / Docker:** the `Dockerfile` already does it. A first stage downloads a **pinned** OpenCode version (`OPENCODE_VERSION`, currently `1.19.0`) and the final image contains only the standalone binary (no Node.js), plus `ripgrep`, which OpenCode's search uses. To bump the version, change the `ARG` and run the check below after deploying.

**Locally:** `npm install -g opencode-ai@1.19.0` (needs Node.js), or see opencode.ai/docs for other installers.

**Verify with your real settings** (costs a few cents at most; touches no Jira or GitHub):

```bash
python scripts/check_opencode.py          # add --bash to let the agent run commands for this check
```

It checks the binary, the version, that your API key reaches the agent, that a small edit works and the agent doesn't commit, and prints the requests and tokens used. On Render, run it from the service's **Shell**. `/health` also lists configuration problems (missing binary, too little memory) under `problems`.

> The Docker image was written but **not built** where this was developed (no Docker available there). The first Render deploy is its first real build; if it fails, the error will be in the *Build* log in the first stage (the `npm install` / `cp` lines).

---

## 6. Render: memory, tests and deploys

**Memory is the big one.** One OpenCode run peaks at roughly **550 MB** (measured), on top of the web app and possibly an Aider planning process (~300 MB each). Render's **Free** instance has **512 MB**, so the coding job would be killed mid-run. Use an instance with at least 1 GB, and 2 GB if planning and coding can run at once (the default: they use separate queues). With the Free plan, keep `CODING_BACKEND=aider`. The app warns in `/health` → `problems` when the container memory limit is under 1 GB and OpenCode is selected.

**Can tests run on Render? Two different things:**

1. *This project's own tests* (`pytest`) run in **GitHub Actions** on every push (`.github/workflows/tests.yml`), not on Render. That is the right place. Make Render wait for them: in the Render service's settings, set auto-deploy to deploy only **after CI checks pass** (look for the Auto-Deploy option; the label may differ slightly), so a push that breaks tests never goes live. Don't install `pytest` in the production image.
2. *The target repo's tests* (the code the agent is changing) can't realistically run on Render: the agent's container doesn't have that repo's dependencies, and installing them is both slow and a supply-chain risk (the agent is told not to install). The standard answer is to let the **target repo's own CI run on the PR** the agent opens (GitHub Actions on the draft PR). A natural next step is to feed a failing CI result back to the agent as a follow-up. If you want the agent itself to iterate against tests, that needs a proper sandbox with the repo's dependencies, which is the production setup in §4.

**Other Render notes:**
- The first OpenCode run after each deploy is a little slower (it fetches the model list into the cache folder, which lives in `/tmp` and is cleared by restarts).
- A run holds one worker for up to `OPENCODE_TIMEOUT_SECONDS`; the single coding worker means tickets still go one at a time.
- Free-tier LLM limits bite harder with an agent: it sends many requests per ticket. Watch the request counts in `/usage`.

---

## 7. Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| Job fails: "OpenCode could not be started" | Binary missing (local run, or image built without it) | Install it or set `CODING_BACKEND=aider`; `/health` → `problems` says so |
| Job killed / service restarts during coding | Out of memory (Free plan) | Bigger instance, or use Aider |
| "rate or usage limit" | Provider limit; agents make many requests | Wait, set `OPENCODE_FALLBACK_MODEL`, use a paid/higher-limit model, lower `OPENCODE_MAX_STEPS` |
| "ProviderAuthError: Google Generative AI API key is missing" | Key variable not reaching the agent (fixed for `GEMINI_API_KEY` in this version) | Make sure `GEMINI_API_KEY` is in `.env`/Render env and `OPENCODE_MODEL` starts with `google/`; run `scripts/check_opencode.py` |
| "OpenCode reported an error: … Authentication/401/403" | Wrong or missing key for the model's provider | The key variable must match the provider (`google/` → `GEMINI_API_KEY`). Run `scripts/check_opencode.py`. |
| "OpenCode exited with code 1: Configuration is invalid…" | Bad `OPENCODE_EXTRA_CONFIG` | Fix the JSON |
| "used up its limit of model requests" | Ticket too big/vague, or a weak model looping | Make the ticket specific, name the files, raise `OPENCODE_MAX_STEPS`, try a stronger model |
| "made no changes" with an explanation | The agent decided nothing was needed, or couldn't | Read its explanation; sharpen the ticket |
| Warning "made a git commit on its own" | The agent tried to commit | Harmless (the app folded it in); tells you the model ignores instructions |
| Costs show "unknown" | Model not in the built-in prices or `MODEL_PRICES` | Add both the `provider/model` and Aider-style names (§1) |

Logs: search the Render log for `OpenCode` (start, finish with request/token counts) and the ticket key. `/runs/<KEY>` has the full transcript of each run.

---

## 8. Where the code is

| File | Role |
|---|---|
| `app/opencode_runner.py` | Everything OpenCode-specific: config, scrubbed environment, process control, event parsing, log format, prompt rules, memory check |
| `app/agent.py` | `_run_coder()` (chooses the backend), `_run_opencode()` (the steps in §2), `_repair_git_state()` |
| `app/config.py` | The settings in §3 |
| `scripts/check_opencode.py` | The smoke test |
| `tests/test_opencode_runner.py`, `tests/test_agent_opencode.py`, `tests/fake_opencode.py` | Tests against a fake `opencode` that speaks the real JSON format, with a real git origin |
