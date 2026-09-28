# Setting up on a new laptop

Step-by-step guide to working on this project from a different computer.

## First: what you do NOT need to redo

The agent runs in the cloud. These keep working no matter which laptop you use, so **don't recreate them**:

| Already set up | Where |
|---|---|
| The running agent | Render service `jira-agent` (`https://jira-agent-r9w8.onrender.com`) |
| The Jira webhook | Jira ⚙ → System → WebHooks → `coding-agent` |
| The code | GitHub: `Tarun2704/jira-agent` |
| API keys / tokens | Stored in Render → the service → **Environment** |

A new laptop is only needed to **change the code, test it locally, and push**. Every push to `main` redeploys Render automatically.

Tickets keep working even with no laptop switched on.

---

## Step 1: Install the tools

| Tool | Why | Get it |
|---|---|---|
| **Git** | Clone and push the code | macOS: comes with Xcode tools (`xcode-select --install`). Windows: git-scm.com |
| **Python 3.11 or newer** | Run tests and helper scripts | python.org, or `brew install python@3.12` on macOS |
| **Docker Desktop** | Run the full agent locally (it includes Aider and git) | docker.com/products/docker-desktop |
| **VS Code** (optional) | Editor | code.visualstudio.com |

Check they work:

```bash
git --version
python3 --version      # Windows: python --version
docker --version
```

## Step 2: Give the laptop access to the GitHub repo

The laptop needs to push to `Tarun2704/jira-agent`. Use a GitHub account that has access: **Tarun2704** (owner) or **tarungunuguntla** (collaborator).

**Option A: SSH key (recommended)**

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"   # press Enter to accept defaults
cat ~/.ssh/id_ed25519.pub                            # Windows: type %USERPROFILE%\.ssh\id_ed25519.pub
```

Copy the output, then on github.com (logged into the account you'll push with): **Settings → SSH and GPG keys → New SSH key** → paste → Save.

Test it:

```bash
ssh -T git@github.com
# "Hi <username>! You've successfully authenticated..." means it works
```

**Option B: HTTPS with a token.** Clone with `https://github.com/Tarun2704/jira-agent.git`. When git asks for a password, paste a GitHub token that has **Contents: Read and write** on `jira-agent` (not your account password).

## Step 3: Clone the code

```bash
git clone git@github.com:Tarun2704/jira-agent.git      # Option B: https://github.com/Tarun2704/jira-agent.git
cd jira-agent
```

Set who your commits are from, for this repo only:

```bash
git config user.name "Tarun"
git config user.email "tarungunuguntla0632@gmail.com"
```

## Step 4: Create the `.env` file

`.env` holds the secrets and is **never** in GitHub, so the new laptop doesn't have it. Two ways to get it:

**Option A: copy the values from Render (easiest, always up to date)**
1. Render dashboard → service **jira-agent** → **Environment**.
2. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env          # Windows: copy .env.example .env
   ```
3. For each variable, reveal the value in Render and paste it into `.env`.

**Option B: copy the file from your old laptop.** Use AirDrop, a USB stick or a password manager. **Never** send it by email or chat, or commit it.

The variables you need:

| Variable | Notes |
|---|---|
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Your Jira site and the API token |
| `GITHUB_TOKEN`, `GITHUB_REPO` | Token for the **target** repo (`Tarun2704/Crimes-in-India`), not for `jira-agent` |
| `GROQ_API_KEY`, `AIDER_MODEL` | e.g. `groq/openai/gpt-oss-120b` |
| `WEBHOOK_SECRET` | Must be **identical** to Render and the Jira webhook's Secret |
| `TRIGGER_LABEL`, `OPEN_DRAFT_PR`, `AIDER_EDIT_FORMAT` | Defaults: `ai-agent`, `true`, `diff` |

> If you create a **new** token instead of reusing one, you must also update it in Render (see "Changing a secret" below), otherwise the laptop and the deployed agent use different keys.

## Step 5: Install Python dependencies and check the setup

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/check_setup.py
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python scripts\check_setup.py
```

`check_setup.py` makes read-only calls and never prints secrets. All lines should show ✅:

```
✅ env vars: all set
✅ Jira auth: logged in as Tarun
✅ Jira projects: visible projects: ['CA', 'KAN']
✅ GitHub repo: Tarun2704/Crimes-in-India (public), default branch 'main', push=True
✅ GitHub repo has commits: yes
✅ GitHub PR access: ok
✅ Groq key: valid
✅ Groq model: 'openai/gpt-oss-120b' available
```

If a line shows ❌, the message says what's wrong (e.g. a retired Groq model lists the ones that are available).

## Step 6: Run the tests

```bash
.venv/bin/python -m pytest -q          # Windows: .venv\Scripts\python -m pytest -q
```

Should end with `passed` and no failures. No credentials are needed for tests.

## Step 7: Run the agent locally (optional)

Only needed when you've changed the agent's code and want to try it before deploying. Docker must be running.

```bash
docker build -t jira-agent .
docker run --env-file .env -p 8000:8000 jira-agent
```

In a second terminal, send it a ticket that exists in Jira with the `ai-agent` label:

```bash
.venv/bin/python scripts/send_test_webhook.py CA-5
```

Watch the first terminal for `Queued`, `Running aider`, `Opened ...`.

> ⚠️ This is the **real** flow: it comments on the real ticket and opens a real PR on `Crimes-in-India`. Jira can't reach your laptop, so the local copy only runs when you send webhooks yourself; the Render copy keeps handling real tickets as normal.

## Step 8: Make a change and deploy it

```bash
git pull                                # get the latest first
# ... edit code ...
.venv/bin/python -m pytest -q           # tests pass?
git add -A
git commit -m "Describe the change"
git push
```

Then:
1. Render → service → **Events**: wait for the new deploy to show **Live** (about 3–10 minutes).
2. Open `https://jira-agent-r9w8.onrender.com/health`. It should show `{"status":"ok",...}`.
3. Create a small test ticket in Jira to confirm it end to end.

---

## Changing a secret

When a token expires or you replace it, update **every** place it's used:

| Secret | Update in |
|---|---|
| `JIRA_API_TOKEN` | Render Environment + your `.env` |
| `GITHUB_TOKEN` | Render Environment + your `.env` |
| `GROQ_API_KEY` / `AIDER_MODEL` | Render Environment + your `.env` |
| `WEBHOOK_SECRET` | Render Environment + your `.env` + **Jira webhook's Secret field** |

Saving environment variables in Render restarts the service automatically.

## Checklist

- [ ] Git, Python 3.11+, Docker installed
- [ ] SSH key (or token) added to a GitHub account with access to `Tarun2704/jira-agent`
- [ ] Repo cloned, `git config user.name/user.email` set
- [ ] `.env` created with values matching Render
- [ ] `check_setup.py` all ✅
- [ ] Tests pass
- [ ] (Optional) Docker build works

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied (publickey)` on clone/push | The SSH key isn't added to a GitHub account with access. Run `ssh -T git@github.com` to see which account the key belongs to. |
| `Permission to Tarun2704/jira-agent.git denied to <user>` | That account isn't the owner or a collaborator. Add it under repo **Settings → Collaborators**. |
| `check_setup.py`: Jira HTTP 401 | Wrong `JIRA_EMAIL` or `JIRA_API_TOKEN`; the email must be the Atlassian login email. |
| `check_setup.py`: GitHub HTTP 404 | Token doesn't have access to `GITHUB_REPO`, or the repo name is misspelled. |
| `ModuleNotFoundError: dotenv` | Dependencies not installed in the venv: rerun Step 5's `pip install`. |
| `send_test_webhook.py` returns 401 | `WEBHOOK_SECRET` in `.env` differs from the one the running agent uses. |
| Docker: `Cannot connect to the Docker daemon` | Start Docker Desktop. |

More in [known-issues.md](known-issues.md). How the flow works: [how-it-works.md](how-it-works.md).
