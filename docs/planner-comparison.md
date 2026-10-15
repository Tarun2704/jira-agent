# Planner comparison: Aider vs OpenCode (2026-10-14)

`PLANNER_BACKEND` chooses who reads the code and writes the plan. This page records how the two compared on the same
tickets, so the choice (and the `auto` mode in [todo.md](todo.md)) rests on evidence.

## Setup

- Same model for both: `gemini-3.1-flash-lite` (Aider: `gemini/...`, OpenCode: `google/...`), free tier.
- Only the planner ran (no code, no PR, no Jira or Slack writes), inside the production Docker image.
- Four tickets on two repos:

| Ticket | Repo | Size | What the change really needs |
|---|---|---|---|
| CA-11 | Crimes-in-India | 57 files | new `crime_summary.py` (the ticket names the CSV) |
| CA-7 | Crimes-in-India | 57 files | `display_page` in `Crimes in India Dashboard.py` (named in the ticket) |
| FA-1 | fastapi/fastapi | 3,182 files | the existing `except json.JSONDecodeError` block in `fastapi/routing.py` (~line 464) |
| FA-2 | fastapi/fastapi | 3,182 files | the `datetime.timedelta` entry of `ENCODERS_BY_TYPE` in `fastapi/encoders.py` (line 91) |

FA-1 and FA-2 were written for this test; neither names a file, which is the hard case for the Aider planner.

## Results

| Ticket | Planner | Requests | Time | Cost (list price) | Right file named | Saw the code it plans to change |
|---|---|---|---|---|---|---|
| CA-11 | Aider | 2 | 18.0 s | $0.0015 | yes | yes |
| CA-11 | OpenCode | 2 | 7.5 s | $0.0039 | yes | yes |
| CA-7 | Aider | 1 | 6.1 s | $0.0023 | yes | yes |
| CA-7 | OpenCode | 3 | 9.4 s | $0.0039 | yes | yes |
| FA-1 | Aider | 2 | 17.2 s | $0.0060 | yes | **no** (`routing.py` is 259 KB, over the 200 KB limit, so it was hidden from Aider) |
| FA-1 | OpenCode | 13 | 31.9 s | $0.0260 | yes | yes (12 searches/reads, then `routing.py`) |
| FA-2 | Aider | 2 | 20.6 s | $0.0098 | yes | **no** (`encoders.py` wasn't in the 300-path shortlist) |
| FA-2 | OpenCode | 4 | 22.6 s | $0.0078 | yes | yes (`grep jsonable_encoder`, then read `encoders.py`) |
| **Total** | Aider | **7** | 62 s | **$0.020** | 4/4 | 2/4 |
| **Total** | OpenCode | **22** | 71 s | **$0.042** | 4/4 | 4/4 |

Free-tier key: the real charge was $0; costs are what the same calls cost at list price.

## What the plans said (the part the numbers don't show)

Both planners named the right file every time, so "found the file" isn't the difference. On the big repo the difference
is whether the plan is written **from the code** or **guessed around it**:

- **FA-1, Aider**: "Locate the request body parsing logic (*likely* in `fastapi/routing.py` or `fastapi/dependencies/utils.py`).
  Wrap the JSON parsing call in a try-except block…" That try/except **already exists**; Aider never saw it.
  **OpenCode**: "Edit `fastapi/routing.py` in the `except json.JSONDecodeError` block to add `lineno` and `colno` to the
  error's `ctx`." That's the exact place.
- **FA-2, Aider**: "import `timedelta`… detect `timedelta` instances" (as if there were no handler). There is one, a
  table entry, so this plan invites a second, conflicting code path.
  **OpenCode**: "Update `ENCODERS_BY_TYPE` … instead of `lambda td: td.total_seconds()`", and it flagged that this changes
  the output for existing users.
- **Small repo (CA-11, CA-7)**: both plans equivalent. The tickets named their files, so both planners started from them.

## Why the Aider planner misses on big repos

1. **Shortlist by path, not content.** Files the ticket doesn't name are found by ranking every file *path* by the words
   it shares with the ticket and keeping the top 300 (`candidate_paths` in `app/planner.py`). For FA-2 the top of the list
   was translation-test scripts (their paths share words like "number"); `fastapi/encoders.py` shares no word with the
   ticket ("encoders" vs "encoder") and wasn't in the 300.
2. **Big files are hidden.** Files over 200 KB are kept away from Aider (`.aiderignore`, to protect the token budget), so
   `routing.py` couldn't be read even when it was picked.
3. **A small repo map.** With files in the chat, Aider's repo map was 1,024 tokens: a few lines out of 3,000 files.

OpenCode avoids all three: it greps file contents, and reads the part of a big file it needs.

## Recommendation

Decision (2026-10-14): **`PLANNER_BACKEND=opencode` is the default**; set `PLANNER_BACKEND=aider` per environment to save requests.


- **Small repos, or tickets that name their files:** `PLANNER_BACKEND=aider`. Same plans, about a third of the requests.
- **Big repos with tickets in plain words:** `PLANNER_BACKEND=opencode`. About 3× the requests and 2× the cost here, but
  the plan matches the real code. A wrong plan costs more than that, because the coding step follows it.
- **Next** (in [todo.md](todo.md)): content search for the Aider planner (cheap; fixes cause 1), then `PLANNER_BACKEND=auto`
  (Aider below ~300 candidate files, OpenCode above).

Caveats: four tickets, one model, one run each. OpenCode's request count varies with how much it explores
(2 to 13 here); `OPENCODE_PLAN_MAX_STEPS` (default 20) caps it.

## Reproducing

`scripts/compare_planners.py` runs `Planner.run()` for each ticket with each backend inside the image (nothing is pushed
or posted; see its header for options):

```
docker run --rm --env-file .env -e REDIS_URL= -e PYTHONPATH=/app \
  -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/tmp:/work" jira-agent \
  python /app/scripts/compare_planners.py --out /work/plans CA-11 CA-7 --fastapi
```
