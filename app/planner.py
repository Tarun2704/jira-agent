"""The planner: reads the repo (Aider in read-only `ask` mode) and writes a short plan.

Each call is stateless, since Aider keeps nothing between runs. The conversation (your answers,
earlier questions, the current plan) is stored in Redis and fed back into the prompt each time.
"""
import dataclasses
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.agent import (
    MAX_CONTEXT_FILE_BYTES, MAX_CONTEXT_FILES, AgentError, CodingAgent, empty_reply, llm_problem, merge_files,
    aider_reply, is_rate_limit, mentioned_files, retryable_llm_problem,
)
from app.config import Settings
from app.jira_client import JiraIssue
from app.jobs import Job, parse_aider_tokens
from app.pr_writer import _complete
from app.store import Session

log = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 12  # keep prompts small: free-tier models cap tokens per request
MAX_MESSAGE_CHARS = 1500
MAX_PLAN_CHARS = 6000

PROMPT = """You are a senior engineer planning the work for Jira ticket {key} ({issue_type}) BEFORE any code is written.
You may read the repository, but you must not edit files or write the implementation.

Title: {summary}

Description:
{description}
{history}{conversation}{current_plan}
Your task: {task}

Write for a busy reviewer: short, concrete, no filler. Use real file paths from the repository.
Ask a question only when the answer would change what gets built and the ticket and code don't already
answer it. Do not ask about things you can look up in the repository.

Reply in exactly this layout. Put a line containing only the word PLAN_START first and a line
containing only the word PLAN_END last, with nothing before or after them:

PLAN_START
## Questions
(one bullet per blocking question, or the single word None)
## Plan
(numbered steps, in order, each one short and imperative)
## Delete
(existing files to delete completely, one path per line in backticks, or the single word None. The system removes exactly these files BEFORE the code is written. Do not list a file you only edit; do not describe deletions anywhere else as steps to be done by code.)
## Files
(bullets: `path/to/file` and what changes there; files that are only deleted are not listed here)
## Risks
(bullets, or the single word None)
PLAN_END"""

CHAT_PROMPT = """You are a senior engineer chatting with the owner of Jira ticket {key} about your plan. Nothing has been coded yet.

Title: {summary}

Description:
{description}
{history}
Your current plan (version {version}):
{plan}
{conversation}
The owner's newest message(s) are the last [Ticket owner] lines above. Reply like a helpful colleague in a chat:
- If they ask a question or make a comment, answer it directly and briefly, using the ticket, the plan and the repository. Do NOT change the plan for that.
- Change the plan only if they asked for a change, answered one of your open questions in a way that alters the steps or files, or showed that the plan is wrong. A clarification that leaves the steps and files as they are is not a change.
- If you truly need one more answer from them, ask one short question in your reply.
- Never repeat the whole plan in your reply.

Reply in exactly this layout:

REPLY_START
(1 to 5 plain sentences to the owner. If you changed the plan, say in a sentence what changed and why.)
REPLY_END
PLAN_CHANGED: no
(write yes instead of no if, and only if, the plan changed; then add the full updated plan as below.
Write show instead of no if the owner only asked to see the current plan again; then add no plan.)
PLAN_START
## Questions
(bullets, or the single word None)
## Plan
(numbered steps)
## Delete
(existing files to delete completely, one path per line in backticks, or the single word None. The system removes exactly these files BEFORE the code is written. Do not list a file you only edit; do not describe deletions anywhere else as steps to be done by code.)
## Files
(bullets: `path` and what changes there; files that are only deleted are not listed here)
## Risks
(bullets, or the single word None)
PLAN_END

Only include the PLAN_START to PLAN_END part when PLAN_CHANGED is yes."""

FOLLOWUP_PROMPT = """You are a senior engineer. Jira ticket {key} was already worked on and delivered (see the earlier work below). The ticket owner has just written to you. Nothing new has been coded yet.

Title: {summary}

Description (as it is now):
{description}
{history}{conversation}
The owner's newest message(s) are the last [Ticket owner] lines above. Reply like a helpful colleague in a chat:
- If they ask a question or comment, answer it directly and briefly, using the earlier work, the diff and the repository (the files shown are the code as it is NOW, including the earlier changes). Do NOT make a plan for that.
- If they ask for a change, or give feedback or review comments to address, write a plan for ONLY the new work: what must change from the code as it is now. Do not repeat what is already done.
- If you truly need one more answer from them, ask one short question in your reply.

Reply in exactly this layout:

REPLY_START
(1 to 5 plain sentences to the owner. If you wrote a plan, say in a sentence what it covers.)
REPLY_END
PLAN_CHANGED: no
(write yes instead of no if, and only if, the owner asked for new work; then add the plan as below)
PLAN_START
## Questions
(bullets, or the single word None)
## Plan
(numbered steps)
## Delete
(existing files to delete completely, one path per line in backticks, or the single word None. The system removes exactly these files BEFORE the code is written. Do not list a file you only edit; do not describe deletions anywhere else as steps to be done by code.)
## Files
(bullets: `path` and what changes there; files that are only deleted are not listed here)
## Risks
(bullets, or the single word None)
PLAN_END

Only include the PLAN_START to PLAN_END part when PLAN_CHANGED is yes."""

TASK_INITIAL = "Investigate the repository as needed, then write the plan, with questions if you have any."
TASK_FOLLOWUP = (
    "This is a follow-up round. The new input above says what must change now. Look at the code as it is now "
    "(it already contains the earlier work) and write a plan for ONLY the new work."
)
TASK_REVISE = (
    "The conversation above happened after your current plan. Update the plan to reflect it: keep what still "
    "applies, change what the person asked to change, and drop questions they have answered."
)


@dataclass
class PlanResult:
    questions: str  # "" when there are none
    body: str  # full Markdown (Questions only if open, then Plan, Files, Risks)
    files: list[str] = field(default_factory=list)  # repo files the plan reads/changes (set by Planner.run)
    reply: str = ""  # what to say to the owner (chat rounds)
    show: bool = False  # the owner asked to see the current plan again: re-post it, unchanged
    warnings: list[str] = field(default_factory=list)  # non-fatal problems to tell the owner about
    changed: bool = True  # False: the owner only needed an answer; the existing plan stands
    delete_files: list[str] = field(default_factory=list)  # files to delete, checked against the repo (set by Planner.run)


# ---- finding the relevant files when the ticket doesn't name any ---------------------------------

MAX_CANDIDATE_PATHS = 300  # keeps the discovery prompt to a few thousand tokens
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz", ".lock", ".csv", ".tsv", ".xlsx",
    ".xls", ".parquet", ".pkl", ".ipynb", ".woff", ".woff2", ".ttf", ".map", ".mp4", ".mp3", ".db", ".sqlite",
}

DISCOVERY_PROMPT = """You help find which files in a code repository matter for a Jira ticket.

Ticket {key}: {summary}

Description:
{description}
{notes}
Files in the repository, one per line:
{paths}

List the files (at most {limit}) that must be read or changed to do this ticket, most relevant first.
Output one path per line, copied exactly from the list above, and nothing else.
If none of them can be relevant, output the single word NONE."""


def candidate_paths(tracked: list[str], repo_dir: Path, text: str, limit: int = MAX_CANDIDATE_PATHS) -> list[str]:
    """Text files small enough for a model, ranked by how many ticket words their path shares."""
    words = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    scored = []
    for path in tracked:
        f = repo_dir / path
        if Path(path).suffix.lower() in SKIP_SUFFIXES or not f.is_file() or f.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            continue
        overlap = sum(1 for w in re.findall(r"[a-z0-9]{3,}", path.lower()) if w in words)
        scored.append((-overlap, path))
    scored.sort()
    return [p for _, p in scored[:limit]]


def parse_paths(reply: str, allowed: set[str], limit: int = MAX_CONTEXT_FILES) -> list[str]:
    """Paths from the model's reply that really exist (it may add bullets, quotes or chatter)."""
    out: list[str] = []
    for line in reply.splitlines():
        path = line.strip().lstrip("-*0123456789. ").strip("`'\" ").removeprefix("./")
        if path in allowed and path not in out:
            out.append(path)
    return out[:limit]


def discover_files(
    model: str, issue: JiraIssue, notes: str, tracked: list[str], repo_dir: Path, usage: dict | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    """Ask the model which repo files matter. [] if it can't tell or the call fails (planning goes on)."""
    paths = candidate_paths(tracked, repo_dir, f"{issue.summary}\n{issue.description}\n{notes}")
    if not paths:
        return []
    prompt = DISCOVERY_PROMPT.format(
        key=issue.key,
        summary=issue.summary,
        description=issue.description or "(no description)",
        notes=f"\nNotes from the ticket owner:\n{notes[:MAX_PLAN_CHARS]}\n" if notes else "",
        paths="\n".join(paths),
        limit=MAX_CONTEXT_FILES,
    )
    try:
        reply, call_usage = _complete(model, prompt)
        if usage is not None:
            usage.update(call_usage)
    except Exception as e:
        log.warning("File discovery failed for %s; planning without it", issue.key, exc_info=True)
        if warnings is not None:
            reason = llm_problem(f"{type(e).__name__}: {e}") or f"{type(e).__name__}: {str(e)[:200]}"
            warnings.append(f"Couldn't search the repo for relevant files ({reason.splitlines()[0]}). I planned without that step, so the file list may be incomplete.")
        return []
    return parse_paths(reply, set(paths))


_BACKTICKED = re.compile(r"`([^`\n]+)`")


def plan_files_from_text(plan_md: str, tracked: set[str]) -> list[str]:
    """Repo files the plan mentions (backticked paths, or the start of a bullet in its Files section)."""
    found: list[str] = []

    def add(raw: str) -> None:
        path = raw.strip().strip("`'\" ").removeprefix("./")
        if path in tracked and path not in found:
            found.append(path)

    for m in _BACKTICKED.finditer(plan_md):
        add(m.group(1))
    files_section = re.search(r"## Files\n(.*?)(?=\n## |\Z)", plan_md, flags=re.S)
    for line in (files_section.group(1).splitlines() if files_section else []):
        add(re.split(r"\s[:\u2014-]\s|:\s", line.strip().lstrip("-* "), maxsplit=1)[0])
    return found[:MAX_CONTEXT_FILES]


def build_conversation(session: Session) -> str:
    msgs = session.messages[-MAX_HISTORY_MESSAGES:]
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        who = "You" if m["role"] == "agent" else "Ticket owner"
        lines.append(f"[{who}] {m['text'][:MAX_MESSAGE_CHARS]}")
    return "\nConversation so far (oldest first):\n" + "\n".join(lines) + "\n"


def build_history(session: Session, context: str = "", max_chars: int = 6000) -> str:
    """Earlier rounds of this ticket (what was asked, planned, changed, delivered) plus any new input.

    Newest rounds get the most room, and only the latest round's diff is included, so a long-lived
    ticket still fits a free-tier model's request limit.
    """
    parts: list[str] = []
    rounds = session.rounds
    for i, r in enumerate(rounds):
        latest = i == len(rounds) - 1
        outcome = r.get("outcome", "")
        head = f"Round {r.get('n', i + 1)} ({outcome or 'done'}"
        head += f", PR {r['pr_url']}" if r.get("pr_url") else ""
        head += ")"
        lines = [head]
        if r.get("request"):
            lines.append(f"Asked: {r['request'][:600]}")
        if r.get("summary"):
            lines.append(f"What changed: {r['summary'][:600]}")
        if r.get("files"):
            lines.append("Files: " + ", ".join(r["files"][:8]))
        if r.get("deleted"):
            lines.append("Files deleted: " + ", ".join(r["deleted"][:8]))
        if latest and r.get("plan"):
            lines.append("Plan that was approved:\n" + r["plan"][:1800])
        if latest and r.get("diff"):
            lines.append("Diff of that round:\n" + r["diff"][:4000])
        parts.append("\n".join(lines))
    text = ""
    if parts:
        text = "\nEarlier work on this ticket (oldest first):\n" + "\n\n".join(parts) + "\n"
        if len(text) > max_chars:  # keep the newest rounds, which are the ones that matter
            text = "\nEarlier work on this ticket (older rounds left out):\n" + text[-(max_chars - 60):] + "\n"
    if context:
        text += f"\nNew input for this round:\n{context[:MAX_PLAN_CHARS]}\n"
    return text


def chat_style(session: Session) -> bool:
    """True when the planner's reply uses the REPLY / PLAN_CHANGED layout (it can answer without a new plan)."""
    return (bool(session.plan) and not session.followup) or (session.followup and not session.force_plan)


def build_planner_prompt(issue: JiraIssue, session: Session, context: str = "") -> str:
    common = dict(
        key=issue.key,
        summary=issue.summary,
        description=issue.description or "(no description)",
        history=build_history(session, context),
        conversation=build_conversation(session),
    )
    if session.followup and not session.force_plan:  # delivered ticket, you wrote: answer, or plan the new work
        return FOLLOWUP_PROMPT.format(**common)
    if session.plan and not session.followup:  # a plan exists: the owner is chatting about it
        return CHAT_PROMPT.format(version=session.plan_version, plan=session.plan[:MAX_PLAN_CHARS], **common)
    return PROMPT.format(
        issue_type=issue.issue_type, current_plan="", task=TASK_FOLLOWUP if session.rounds else TASK_INITIAL, **common
    )


_REPLY_START = re.compile(r"^\W*REPLY_START\W*$", re.M)
_REPLY_END = re.compile(r"^\W*REPLY_END\W*$", re.M)
_CHANGED = re.compile(r"PLAN_CHANGED\s*:\s*\**\s*(yes|no|show)", re.I)
_START = re.compile(r"^\W*PLAN_START\W*$", re.M)
_END = re.compile(r"^\W*PLAN_END\W*$", re.M)
_HEADING = re.compile(r"^#{1,4}\s*(Questions|Plan|Delete|Files|Risks)\s*:?\s*$", re.M | re.I)
_NONE = {"none", "- none", "* none", "none.", "n/a", "- n/a", "no questions", "-"}


def _is_none(text: str) -> bool:
    t = text.strip().lower()
    return not t or t in _NONE or all(line.strip().lower() in _NONE for line in t.splitlines() if line.strip())


def parse_planner_output(output: str) -> PlanResult | None:
    """Pull the plan out of Aider's output. None if the model didn't follow the layout.

    Uses the LAST PLAN_START: Aider may echo the prompt before the model's reply.
    """
    starts = list(_START.finditer(output))
    if not starts:
        return None
    start = starts[-1]
    end = _END.search(output, start.end())
    block = output[start.end(): end.start() if end else len(output)]
    if not end:  # reply cut off before PLAN_END: drop Aider's trailing token-count line
        block = re.split(r"^Tokens:", block, maxsplit=1, flags=re.M)[0]

    sections: dict[str, str] = {}
    matches = list(_HEADING.finditer(block))
    for i, m in enumerate(matches):
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        sections[m.group(1).lower()] = block[m.end(): nxt].strip()

    plan = sections.get("plan", "")
    if _is_none(plan):
        return None
    questions = "" if _is_none(sections.get("questions", "")) else sections["questions"]
    parts = []
    if questions:
        parts.append(f"## Questions\n{questions}")
    parts.append(f"## Plan\n{plan}")
    for name in ("files", "risks"):
        if not _is_none(sections.get(name, "")):
            parts.append(f"## {name.capitalize()}\n{sections[name]}")
    result = PlanResult(questions=questions, body="\n\n".join(parts))
    result.delete_files = parse_delete_section(sections.get("delete", ""))
    return result


MAX_DELETIONS = 10


def parse_delete_section(text: str) -> list[str]:
    """Paths from a plan's "## Delete" section: backticked paths, else one bare path per line."""
    if _is_none(text):
        return []
    out: list[str] = []
    for line in text.splitlines():
        found = _BACKTICKED.findall(line) or [line.strip().lstrip("-*0123456789. ").strip()]
        for raw in found:
            path = raw.strip().strip("`'\" ").removeprefix("./")
            if path and path.lower() not in _NONE and path not in out:
                out.append(path)
    return out


def check_deletions(paths: list[str], tracked: set[str]) -> tuple[list[str], list[str]]:
    """(valid, dropped): only tracked files, never anything under .git, at most MAX_DELETIONS."""
    valid: list[str] = []
    dropped: list[str] = []
    for p in paths:
        ok = p in tracked and not p.startswith(".git/") and ".." not in Path(p).parts and len(valid) < MAX_DELETIONS
        (valid if ok else dropped).append(p)
    return valid, dropped


def with_delete_section(body: str, paths: list[str]) -> str:
    """The plan text with a "## Delete" section listing exactly the checked paths (none: no section)."""
    body = re.sub(r"^## Delete\n.*?(?=^## |\Z)", "", body, flags=re.S | re.M).strip()
    if not paths:
        return body
    section = "## Delete\n" + "\n".join(f"- `{p}`" for p in paths)
    if "\n## Files" in body:
        return body.replace("\n## Files", f"\n{section}\n\n## Files", 1)
    return f"{body}\n\n{section}"


MAX_LISTING_CHARS = 3000


def repo_listing(tracked: list[str], repo_dir: Path) -> str:
    """Names of repo files too big for the model (binaries, data files), so the planner knows they exist.

    Only these: Aider adds every file whose name appears in the prompt to the model's context. Big files are
    hidden from Aider (.aiderignore) so naming them is safe; naming ordinary files would load them all.
    """
    big = [p for p in tracked if (repo_dir / p).is_file() and (repo_dir / p).stat().st_size > MAX_CONTEXT_FILE_BYTES]
    out, size = [], 0
    for p in big:
        size += len(p) + 1
        if size > MAX_LISTING_CHARS:
            out.append("... (list cut short)")
            break
        out.append(p)
    if not out:
        return ""
    return ("\n\nFiles in the repository that are too large to open (they exist; you can name them in the plan, "
            "to delete one, list it in the Delete section; you cannot read them):\n" + "\n".join(out))


def parse_chat_output(output: str) -> PlanResult | None:
    """Parse a chat round: a reply for the owner, and a new plan only if the plan changed.

    Tolerant: a model that skips the reply but gives a valid plan counts as "plan updated".
    """
    starts = list(_REPLY_START.finditer(output))
    reply = ""
    if starts:
        end = _REPLY_END.search(output, starts[-1].end())
        reply = output[starts[-1].end(): end.start() if end else len(output)]
        if not end:  # no REPLY_END: stop at the next marker
            reply = re.split(r"^\W*(?:PLAN_CHANGED|PLAN_START)", reply, maxsplit=1, flags=re.M)[0]
    elif (end := _REPLY_END.search(output)):  # forgot REPLY_START: take what precedes REPLY_END
        reply = "\n".join(
            ln for ln in output[: end.start()].splitlines()
            if ln.strip() and not ln.startswith(("Aider v", "Main model:", "Model:", "Git repo:", "Repo-map:", "Added ", "Analytics", "Warning:"))
            and ln.strip() != "requirements.txt" and "tokens, auto refresh" not in ln
        )
    reply = reply.strip()

    flags = _CHANGED.findall(output[starts[-1].start():] if starts else output)
    says_changed = bool(flags) and flags[-1].lower() == "yes"
    plan = parse_planner_output(output)

    if reply and flags and flags[-1].lower() == "show":
        return PlanResult(questions="", body="", reply=reply, changed=False, show=True)
    if reply and not says_changed:
        return PlanResult(questions="", body="", reply=reply, changed=False)
    if plan is not None:  # said yes, or gave a plan without a reply
        plan.reply = reply or "I've updated the plan."
        return plan
    if reply:  # said "yes" but the plan didn't come through: keep the old plan and say so
        note = "\n\n_(I couldn't write out the updated plan, so the plan above is unchanged. Tell me again if you want the change.)_"
        return PlanResult(questions="", body="", reply=reply + note, changed=False)
    return None


def approved_plan_text(session: Session) -> str:
    """What the coding agent is told: the plan minus open questions, plus the owner's own words."""
    body = re.sub(r"## Questions\n.*?(?=\n## |\Z)", "", session.plan, flags=re.S).strip()
    said = [m["text"][:MAX_MESSAGE_CHARS] for m in session.messages if m["role"] == "user"]
    if said:
        body += "\n\n## Messages from the ticket owner during planning (context only; the plan above already reflects any changes they asked for)\n" + "\n".join(f"- {s}" for s in said)
    return body


class Planner:
    def __init__(self, settings: Settings, job: Job):
        self.s = settings
        self.job = job
        self.warnings: list[str] = []
        # Reuse the coding agent's git/Aider helpers (token redaction, timeouts, file selection).
        self.agent = CodingAgent(settings, job)

    def run(self, issue: JiraIssue, session: Session, context: str = "", branch: str | None = None) -> PlanResult:
        """`branch`: read the code from that branch (an open PR's) instead of the default branch."""
        a = self.agent
        base = a.gh.default_branch()
        repo_dir = Path(self.s.workdir) / f"{issue.key}-plan"
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://x-access-token:{self.s.github_token}@github.com/{self.s.github_repo}.git"
        tracked: list[str] = []
        used_fallback = False
        try:
            with self.job.step("clone"):
                a._run(["git", "clone", "--depth", "50", "--branch", branch or base, clone_url, str(repo_dir)],
                       cwd=repo_dir.parent, timeout=300)
                with open(repo_dir / ".git" / "info" / "exclude", "a") as f:
                    f.write("\n.aider*\n")
            tracked = [p for p in a._git("ls-files", "-z", cwd=repo_dir).split("\0") if p]

            with self.job.step("find_files"):
                files = self._files_for_plan(issue, session, tracked, repo_dir)
            log.info("Files for the plan: %s", files or "none found (Aider will use its repo map)")

            message = build_planner_prompt(issue, session, context) + repo_listing(tracked, repo_dir)

            def ask(model: str) -> str:
                out = a._run_aider(
                    issue, repo_dir, message=message, model=model, chat_mode="ask",
                    timeout=self.s.planner_timeout_seconds, extra_files=files,
                )
                self.job.add_tokens(*parse_aider_tokens(out))
                return out

            chat = chat_style(session)
            parse = parse_chat_output if chat else parse_planner_output
            with self.job.step("aider_plan"):
                output = ask(self.s.effective_planner_model)
                problem = llm_problem(output)
                if parse(output) is None and is_rate_limit(problem) and not self.s.planner_fallback_model and self.s.rate_limit_wait_seconds:
                    log.warning("Planner rate limited; waiting %ss and retrying once", self.s.rate_limit_wait_seconds)
                    self.warnings.append(f"The model was rate limited, so I waited {self.s.rate_limit_wait_seconds}s and tried again.")
                    time.sleep(self.s.rate_limit_wait_seconds)
                    output = ask(self.s.effective_planner_model)
                    problem = llm_problem(output)
                if parse(output) is None and retryable_llm_problem(problem) and self.s.planner_fallback_model:
                    log.warning("Planner model failed (%s); retrying with %s", problem.splitlines()[0], self.s.planner_fallback_model)
                    self.warnings.append(
                        f"The planner model failed ({problem.splitlines()[0]}); the fallback model {self.s.planner_fallback_model} wrote this."
                    )
                    output = ask(self.s.planner_fallback_model)
                    used_fallback = True
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

        result = parse(output)
        if result is None:
            log.warning("Planner reply not in the expected layout. Tail: %s", output[-800:].replace("\n", " | "))
            problem = llm_problem(output) or (
                "The model returned an empty reply (possibly blocked by its content filter)." if empty_reply(output) else None
            )
            if problem and used_fallback:
                problem = f"The fallback model {self.s.planner_fallback_model} was tried after the main model failed, and it failed too: {problem}"
            if problem:
                extra = f"\nAider's output ended with:\n{aider_reply(output, 700)}" if "couldn't apply the model's edit" in problem else ""
                raise AgentError(f"The LLM call failed. {problem}{extra}")
            raise AgentError("The planner's reply wasn't in the expected format. Reply to try again.\n" + output[-600:])
        result.warnings = list(self.warnings)
        if not result.changed:  # an answer only: the plan, and the files it is about, stay as they were
            result.files = list(session.files)
            result.delete_files = list(session.delete_files)
            return result
        valid, dropped = check_deletions(result.delete_files, set(tracked))
        if dropped:
            result.warnings.append("I left these out of the files to delete because they aren't tracked files in the repo (or are over the limit): " + ", ".join(f"`{d}`" for d in dropped))
        result.delete_files = valid
        result.body = with_delete_section(result.body, valid)
        # Remember the files the plan is about: later revisions and the coding run start from them.
        result.files = [f for f in dict.fromkeys(self.job.files + plan_files_from_text(result.body, set(tracked))) if f not in valid][:MAX_CONTEXT_FILES]
        return result

    def _files_for_plan(self, issue: JiraIssue, session: Session, tracked: list[str], repo_dir: Path) -> list[str]:
        """Files named in the ticket or chat, plus those from earlier rounds; else ask the model to find them."""
        owner_notes = "\n".join(m["text"] for m in session.messages if m["role"] == "user")
        scan = dataclasses.replace(issue, description=f"{issue.description}\n{owner_notes}")
        known = merge_files(mentioned_files(scan, tracked, repo_dir), session.files, tracked, repo_dir)
        if known:
            return known
        usage: dict[str, int] = {}
        found = discover_files(self.s.effective_planner_model, issue, owner_notes, tracked, repo_dir, usage, self.warnings)
        self.job.add_tokens(usage.get("sent", 0), usage.get("received", 0))
        return merge_files([], found, tracked, repo_dir)
