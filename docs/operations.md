# Operations runbook

Everyday tasks, done by hand in Jira and GitHub: running a ticket, running it again, fixing a stuck ticket, and cleaning up after tests or a demo.

No command line is needed for anything here except the optional "replay" in section 5.

---

## 1. Run a new ticket

1. In Jira, create a ticket in the **CA** project.
2. Write a **specific, small** task: name the file, describe the expected result, and add "Don't change anything else". Keep it additive (a new function or file, a few changed lines); the free AI tier can't handle big rewrites. See [known-issues.md](known-issues.md).
3. Add the label **`ai-agent`** before saving.
4. Within about a minute (longer if the service was asleep): the ticket moves to **In Progress**, then **In Review**, with a comment linking the PR.

---

## 2. Run the same ticket again

Use this when you want a fresh attempt at an existing ticket (e.g. to re-record a demo, or after improving the description).

**Step 1: close the ticket's open PR on GitHub**
1. Open the PR (the link is in the agent's Jira comment, or find it under the target repo's **Pull requests** tab).
2. Scroll to the bottom and click **Close pull request**.
3. Optional: click **Delete branch** (the button appears after closing). Not required: the agent overwrites its own `ai/…` branch on the next run.

> If you skip this step, the agent won't redo the work. It comments "Skipped: a pull request for this ticket is already open".

**Step 2 (optional): improve the ticket's description** so the next attempt is better.

**Step 3: remove and re-add the label in Jira**
1. Open the ticket.
2. In **Labels**, remove `ai-agent` (click the ✕ on it) and let it save.
3. Add `ai-agent` back.

Re-adding the label is what triggers the agent. Editing the description or commenting does **not** trigger it (by design, so the agent can't trigger itself).

The ticket goes To Do → In Progress → In Review again, and a new PR appears.

---

## 3. Fix a stuck ticket

| Symptom | Cause | Fix |
|---|---|---|
| Ticket has `ai-agent-queued` but nothing is happening for several minutes | The job was cut off (e.g. a restart) and not recovered, or the queue label was left behind | Check `/jobs?issue=KEY` (see [observability.md](observability.md)). If nothing is running, remove `ai-agent-queued`, then remove and re-add `ai-agent`. |
| Ticket stuck in **In Progress** after a failure | The status move back failed | Drag it back to **To Do** on the board. |
| No agent comment at all after a few minutes | The webhook never arrived (e.g. sent during a deploy) | Remove and re-add `ai-agent`, or replay it (section 5). |
| Comment "made no changes" | Ticket too vague, or too big for the free AI tier | Make it more specific or smaller, then rerun (section 2). |
| Comment "Skipped: a pull request … is already open" | A PR already exists for this ticket | Close it first (section 2, step 1). |

---

## 4. Clean up after testing or a demo

### Jira: delete tickets
1. Open the ticket.
2. Click **•••** (More actions) at the top right → **Delete** → confirm.

Deleted tickets **can't be recovered**. If you'd rather keep them, drag them to **Done** instead. Ticket numbers are never reused, so the next ticket continues from the last number (e.g. CA-6).

### GitHub: close PRs
1. Open the target repo → **Pull requests**.
2. Open each agent PR (titles start with the ticket key, e.g. `CA-5: …`) → **Close pull request**.

GitHub doesn't allow deleting PRs; closed ones remain under the **Closed** filter. The **Open** list is what people see by default.

### GitHub: delete the agent's branches
1. Open the target repo → the branch dropdown → **View all branches** (or go to `https://github.com/<owner>/<repo>/branches`).
2. Click the 🗑 (delete) icon next to each branch starting with `ai/`.

Never delete `main`.

### The agent's job history (`/jobs`)
Nothing to do: it lives in memory and clears itself on every restart or deploy.

### Before recording a demo: checklist
- [ ] Jira board: no leftover test tickets (deleted or in Done)
- [ ] GitHub: no open agent PRs; no `ai/` branches
- [ ] Open `https://jira-agent-r9w8.onrender.com/health` to wake the service up
- [ ] No local copy of the agent running (`docker ps` shows nothing), so only Render handles the ticket

---

## 5. Optional: replay a ticket without touching Jira

If a webhook was lost, you can send it again from a computer with the project and `.env` set up ([setup-new-machine.md](setup-new-machine.md)):

```bash
.venv/bin/python scripts/send_test_webhook.py CA-5 --url https://jira-agent-r9w8.onrender.com
```

This behaves like Jira sending "ticket created". The ticket still needs the `ai-agent` label, and still no open PR.

---

## 6. Check what's happening

| Where | What it tells you |
|---|---|
| The **Jira ticket** | Agent comments and the current status |
| **Render → jira-agent → Logs** | Every step, live; search for the ticket key |
| `https://jira-agent-r9w8.onrender.com/jobs?token=<WEBHOOK_SECRET>&issue=CA-5` | That ticket's jobs (status, timings, checks, PR link) and every webhook with its reason |
| `https://jira-agent-r9w8.onrender.com/health` | Whether the service is up |

Details: [observability.md](observability.md).
