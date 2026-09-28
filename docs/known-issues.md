# Known issues and limits

Things to know when running the Jira coding agent: current limitations, problems already fixed (and why), and what to do when something goes wrong.

## Current limitations

| Issue | Impact | Workaround |
|---|---|---|
| **Render free tier sleeps** after ~15 min idle | First ticket after a quiet spell waits 30–60s while the service wakes up. Jira retries if the first delivery times out. | None needed. For always-on, move to an Oracle Cloud Always Free VM or a paid Render instance. |
| **Queue is in memory** | If Render restarts or redeploys mid-job, that ticket is dropped silently. | Remove and re-add the `ai-agent` label to rerun it. |
| **One job at a time** | Several tickets created together are handled one after another. | Expected on the 512 MB free instance; don't raise the worker count there. |
| **Vague tickets fail** | "Improve the code" gives poor or no edits. | Name the file(s) and describe the expected behaviour. Tickets that name a file get that file handed to Aider directly. |
| **Free LLM limits** | Groq's free tier is rate-limited. Free tiers may log or train on prompts. | Only point the agent at code you're fine sending to the provider. Switch provider with `AIDER_MODEL` + its API key. |
| **Groq retires models** | Jobs fail with a model-not-found error. | Run `.venv/bin/python scripts/check_setup.py`; it lists the models available to your key. Update `AIDER_MODEL` on Render. |
| **GitHub token expires** | Clone/push/PR fails after the expiry date chosen at creation. | Create a new fine-grained token (Contents + Pull requests: Read and write, target repo only) and update `GITHUB_TOKEN` on Render. |
| **Draft PRs need a public repo** on GitHub Free | For private repos, draft creation is rejected. | Handled automatically: the agent retries as a normal PR. |
| **Agent PRs can conflict** | Two tickets touching nearby lines of the same file produce PRs that conflict with each other. | Merge one, then resolve the other on GitHub, keeping both changes. |
| **Single target repo** | Every ticket goes to `GITHUB_REPO`. | Multi-repo support (e.g. map by Jira component) is not built yet. |
| **No tests are run** | The agent doesn't run the repo's tests before opening the PR. | Review every PR. Running tests before the PR is a possible next step. |
| **`/jobs` history resets** on every deploy/restart | Older runs aren't in `/jobs`. | Use Render's Logs (limited retention on the free plan). |
| **No alerts** | Nobody is notified when a job fails. | Check Jira comments / `/jobs`. Sentry or UptimeRobot (free) could add alerts. |
| **Webhooks can be lost during a deploy** | A ticket created while Render is redeploying may never reach the agent (seen with CA-3). | Replay with `send_test_webhook.py`, or re-add the label. Render **Build Filters** can skip deploys for docs-only changes. |
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
| Weak PR descriptions | The PR body was just a diff stat and log tail. | An extra LLM call writes Summary / Changes / How to verify from the real diff (`app/pr_writer.py`). |

## Troubleshooting

1. **Check credentials and model:** `.venv/bin/python scripts/check_setup.py` (read-only calls; never prints secrets).
2. **Check the service is up:** open `https://<your-render-url>/health`. `in_flight` lists tickets being worked on.
3. **See what happened to a ticket:** open `https://<your-render-url>/jobs?token=<WEBHOOK_SECRET>&issue=CA-3`: status, step timings, error, and whether the webhook arrived.
4. **Read the logs:** Render dashboard → the service → **Logs**; search for the ticket key or `ERROR`. See [observability.md](observability.md) for what each line means.
5. **Check Jira delivered the webhook:** Jira ⚙ → System → WebHooks. A 401 means the Secret in Jira doesn't match `WEBHOOK_SECRET` on Render.
6. **Replay a ticket without Jira:** `.venv/bin/python scripts/send_test_webhook.py CA-1 --url https://<your-render-url>`.
