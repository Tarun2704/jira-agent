# Roadmap: from prototype to a production tool

The agent works end to end today (Jira ticket → reviewed-ready draft PR, on free tiers). This is what it would need before a real team relies on it every day, grouped by priority. Current limits are in [known-issues.md](known-issues.md); the design reasoning is in [design-and-scenarios.md](design-and-scenarios.md).

## 1. Must-haves before a team relies on it

| # | Improvement | Why it matters | Effort |
|---|---|---|---|
| 1 | **Run the project's own tests before the PR** (Aider's `--test-cmd`, with an automatic fix attempt) | Today only Python syntax is checked; a PR can compile and still be wrong. | Medium |
| 2 | **Control who can trigger the agent** (only certain Jira users or groups can use the `ai-agent` label) | Anyone who can create a ticket can make the agent write code. Ticket text goes straight to the AI, so a malicious ticket could try to manipulate it ("prompt injection"). | Small–medium |
| 3 | **Retry on AI rate limits** with backoff, and a model with higher limits (paid Groq, or Gemini's free tier) | A busy minute or a medium-sized ticket fails today (free tier: ~8k tokens per request). | Small |
| 4 | **Catch up on missed webhooks**: periodically search Jira for labelled tickets the agent never handled | A webhook sent during a deploy is lost today. | Small |
| 5 | **Alerts** (Sentry for failed jobs, UptimeRobot for downtime) | Today nobody is told when something fails. UptimeRobot also stops the free service from sleeping. | Small |
| 6 | **A GitHub App instead of a personal token** | Personal tokens expire and belong to one person. An app has short-lived tokens, a bot identity (`jira-agent[bot]`) and per-repo installation. | Medium |

## 2. Makes it pleasant to use every day

| # | Improvement | Why |
|---|---|---|
| 7 | **Full ticket lifecycle**: PR merged → Done; PR closed unmerged → To Do | The board stays accurate without manual work. |
| 8 | **Revise from PR review comments** (`/agent also handle X` updates the same PR) | That's how people work with a human teammate. |
| 9 | **Ask instead of giving up**: comment a specific question on vague tickets | Better than "made no changes". |
| 10 | **Size check up front**: flag tickets that are too big and suggest splitting them | Avoids wasted runs and confusing failures. |
| 11 | **Jira ticket template** for agent tickets (file, expected behaviour, acceptance criteria) | Better tickets, better PRs. |
| 12 | **Show the PR in Jira's Development panel** (free "GitHub for Jira" app) | One click from ticket to code. |
| 13 | **Slack or Teams notifications** when a PR is ready | People don't need to keep checking Jira. |

## 3. Growing beyond one repo and one person

| # | Improvement | Why |
|---|---|---|
| 14 | **Multiple repos**, chosen from the ticket's Jira component, with per-repo settings (e.g. an `.agent.yml`: test command, allowed folders, model) | Real teams have many repos with different setups. |
| 15 | **Limit what the agent may change**: never CI workflows, secrets or deployment files; a maximum diff size | Limits the damage from a bad or manipulated edit. |
| 16 | **Checks for other languages** (JavaScript, Java, …) via their linters | The syntax check is Python-only today. |
| 17 | **A proper queue and job history** (Redis or a small database), with several workers | Parallel tickets; history that survives deploys; no reliance on Jira labels. |
| 18 | **Always-on hosting** (paid Render instance or a VM) | No sleeping, no lost webhooks during cold starts, more memory. |

## 4. Governance and cost, for company use

| # | Improvement | Why |
|---|---|---|
| 19 | **LLM data policy**: a provider or tier that doesn't train on your code | Free tiers may use prompts for training; company code usually can't go there. |
| 20 | **Usage and cost dashboard**: tokens and cost per ticket, success rate, average time | Shows whether the agent is worth it; catches runaway costs. |
| 21 | **Audit trail**: who triggered what, with which model, prompt and diff, kept for months | Compliance, and investigating problems. |
| 22 | **Budget caps**: maximum tokens or time per ticket, and per day | Stops one bad ticket from burning the budget. |

## Suggested order

1. **Quick wins (a day or two):** #3 retries, #4 catch-up, #5 alerts, #7 ticket lifecycle
2. **Trust and safety:** #2 who can trigger, #15 change limits, #1 run tests
3. **Team features:** #8 revise from PR comments, #9 ask questions, #14 multiple repos
4. **Company rollout:** #6 GitHub App, #17 queue and history, #19–#22 governance
