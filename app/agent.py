"""The coding job: clone repo -> branch -> run Aider -> commit -> push -> open PR."""
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.errors import kind_of, reason
from app.friendly import explain
from app.checks import CheckResult, format_checks_md, python_syntax_check
from app.config import Settings
from app.github_client import GitHubClient
from app.jira_client import JiraClient, JiraIssue
from app.jobs import Job, JobStuck, run_process
from app import opencode_runner as oc
from app.retry import is_transient_git_error, wait_for
from app.pr_writer import build_pr_body, describe_changes, summary_line, takes_temperature

log = logging.getLogger(__name__)



# Aider prints LLM provider failures into its output instead of failing loudly. These are recognised
# only at the points where the run produced nothing useful (no plan, no changes), because Aider also
# prints transient errors that it then retries successfully.
# (pattern in the provider's output, error kind): the kind's `reason` in app/errors.py is the sentence reported.
_LLM_PROBLEMS = [
    (r"ContextWindowExceeded|estimated chat context|exceeds the [\d,]+ token|context (length|window)|request too large|reduce the length|exceeds the (maximum|limit)|too many tokens|token limit",
     "too_large"),
    (r"RateLimitError|rate.?limit|tokens per (minute|day)|\bTPM\b|\bTPD\b|quota|insufficient_quota|429", "rate_limit"),
    (r"ContentPolicyViolation|content[_ ]filter|content management policy|safety(?!_settings)|blocked|moderation", "content_blocked"),
    (r"AuthenticationError|invalid api key|incorrect api key|unauthorized|\b401\b|PermissionDenied|\b403\b", "auth"),
    # a 400 that isn't about size or content: the provider refused a setting or parameter (a model settings file, a
    # temperature the model doesn't accept...). Not the ticket's fault, and another model may well accept it.
    (r"BadRequestError|INVALID_ARGUMENT|invalid_request_error|Invalid value at", "invalid_request"),
    (r"NotFoundError|model.*not (found|exist)|does not exist|decommissioned|\b404\b", "model_not_found"),
    (r"APIConnectionError|ServiceUnavailable|InternalServerError|overloaded|\b50[0-4]\b|timed? ?out|Timeout|APIError", "provider_down"),
]
_EDIT_FAILED = re.compile(r"SearchReplaceNoExactMatch|did not match any|failed to match|UnifiedDiffNoMatch", re.I)
# Provider errors are checked first: Aider prints help links (including the edit-errors page) next to them.
_PROVIDER_ERROR = re.compile(r"RateLimitError|rate limited you|QuotaFailure|RESOURCE_EXHAUSTED|ContextWindowExceeded|AuthenticationError|ServiceUnavailable|InternalServerError", re.I)
_ERROR_LINE = re.compile(r"litellm\.|Error|exceed|limit|quota|blocked|refus|unauthori[sz]ed", re.I)


def llm_problem(output: str) -> str | None:
    """A plain-words reason (plus the provider's own error line) if Aider's output shows an LLM failure."""
    if not _PROVIDER_ERROR.search(output) and _EDIT_FAILED.search(output):  # not an LLM outage: the model answered, but its edit didn't fit the file
        return ("Aider couldn't apply the model's edit (its change didn't match the file's text, or it tried to edit a file it can't, "
                "such as a binary). Reply with the exact file and what to change, or ask for a plan only.")
    lines = [
        ln.strip() for ln in output.splitlines()
        if _ERROR_LINE.search(ln) and not ln.lstrip().startswith(("Tokens:", "Cost:", "http", "See ", "For more"))
    ]
    if not lines:
        return None
    blob = "\n".join(lines)
    # A daily quota: waiting a minute can't help, so say so (and let a fallback model take over). Aider prints Google's
    # full error ("PerDay", "retryDelay"); OpenCode only its message ("model: gemini-2.5-flash ... retry in 19h15m").
    hours_left = re.search(r"retry in (\d+)h", output)
    if re.search(r"PerDay", output) or (hours_left and int(hours_left.group(1)) >= 1):
        model = (re.search(r'"model":\s*"([^"]+)"', output) or re.search(r"\bmodel: ([\w.\-/]+)", output) or [None, "the model"])[1]
        delay = re.search(r'"retryDelay":\s*"(\d+)s"', output)
        hours = round(int(delay.group(1)) / 3600) if delay else int(hours_left.group(1)) if hours_left else 0
        resets = f" It resets in about {max(1, hours)} hour(s)." if delay or hours_left else ""
        return (f"The daily free quota for {model} is used up (daily quota: free keys allow only a few requests per day per model).{resets} "
                "Set a fallback model (PLANNER_FALLBACK_MODEL / AIDER_FALLBACK_MODEL / OPENCODE_FALLBACK_MODEL), switch models, or use a key with billing.\n"
                f"Provider said: {lines[0][:200]}")
    for pattern, code in _LLM_PROBLEMS:
        if re.search(pattern, blob, re.I):
            return f"{reason(code)}\nProvider said: {lines[0][:300]}"
    return None


def retryable_llm_problem(problem: str | None) -> bool:
    """Problems another model can get around (the `backup` column in app/errors.py): rate limits, outages and
    model-specific setup errors. A bad key or a blocked prompt would fail on the fallback too."""
    return bool(problem) and kind_of(problem).backup


_NOISE = ("Aider v", "Main model:", "Model:", "Git repo:", "Repo-map:", "Added ", "Tokens:", "Cost:", "Warning:",
          "Analytics", "Scraping", "Page didn't", "Unable to install pandoc", "http")


def aider_reply(output: str, limit: int = 1200) -> str:
    """What the model actually said, without Aider's start-up lines (for telling you why nothing changed)."""
    body = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith(_NOISE) and "requirements.txt" != ln.strip()]
    return "\n".join(body).strip()[-limit:]


def is_rate_limit(problem: str | None) -> bool:
    return bool(problem) and kind_of(problem).code == "rate_limit"


def empty_reply(output: str) -> bool:
    """True if Aider ran but the model said (almost) nothing: typically a blocked or empty completion."""
    body = [
        ln for ln in output.splitlines()
        if ln.strip() and not ln.startswith(("Aider v", "Main model:", "Model:", "Git repo:", "Repo-map:", "Added ", "Tokens:", "Cost:", "Warning:"))
    ]
    return len("\n".join(body).strip()) < 40

def coder_problem(output: str, backend: str) -> str | None:
    """llm_problem() for whichever backend produced `output` (same plain-words reasons for both)."""
    return oc.opencode_problem(output, llm_problem) if backend == "opencode" else llm_problem(output)


def coder_reply(output: str, backend: str, limit: int = 1200) -> str:
    return oc.opencode_reply(output, limit) if backend == "opencode" else aider_reply(output, limit)


def coder_empty(output: str, backend: str) -> bool:
    """True if the model said (almost) nothing, typically a blocked or empty completion."""
    return len(oc.opencode_reply(output)) < 5 if backend == "opencode" else empty_reply(output)


def coder_stopped(output: str, backend: str) -> str | None:
    """Why the agent was cut off before finishing, if it was (OpenCode only: Aider runs are one call)."""
    if backend == "opencode" and "[stopped] step limit" in output:
        return ("The coding agent used up its limit of model requests (OPENCODE_MAX_STEPS) without making a change. "
                "Use a smaller or more specific ticket (name the files), or raise the limit.")
    return None


_PLAN_PATH = re.compile(r"`([^`\s]+)`")


def plan_paths(plan: str) -> set[str]:
    """Paths an approved plan names: backticked paths (`src/a.py`, a folder like `tests/`) and the entries of its
    Files and Delete sections, including files it will create. What the coding step may change (see
    _undo_outside_plan). A plan saying `crime.py` also covers `src/crime.py` (see path_allowed)."""
    found = {m.group(1) for m in _PLAN_PATH.finditer(plan or "")}
    for section in re.findall(r"## (?:Files|Delete)[^\n]*\n(.*?)(?=\n## |\Z)", plan or "", flags=re.S):
        for line in section.splitlines():
            entry = re.split(r"\s[:\u2014-]\s|:\s|\s\(", line.strip().lstrip("-*0123456789. "), maxsplit=1)[0]
            found.add(entry.strip("`'\" "))
    paths = {p.removeprefix("./") for p in found if p and " " not in p and ("." in p or "/" in p)}
    return {p for p in paths if not p.startswith(("http:", "https:"))}


def path_allowed(path: str, allowed: set[str]) -> bool:
    if path in allowed:
        return True
    if any(a.endswith("/") and path.startswith(a) for a in allowed):
        return True
    return any("/" not in a and path.rsplit("/", 1)[-1] == a for a in allowed)


def can_fall_back(s: Settings) -> bool:
    """May Aider take over when OpenCode itself fails? TOOL_FALLBACK is on and Aider is installed here (else the
    original problem is the one to report)."""
    return s.tool_fallback and shutil.which("aider") is not None


def tool_failure(e: Exception) -> str | None:
    """Why OpenCode itself failed, from the error it raised (None: not a tool failure, e.g. a model problem)."""
    return {"opencode_missing": "it could not be started", "stuck": "it made no progress",
            "tool_timeout": "it took too long"}.get(getattr(e, "code", None) or "")


def stuck_error(s: Settings, what: str) -> "AgentError":
    minutes = max(round(s.job_stuck_seconds / 60), 1)
    return AgentError(f"Stopped {what}: the job made no progress for {minutes} minute(s) (JOB_STUCK_SECONDS).", code="stuck")


def strip_credentials(url: str) -> str:
    """https://x-access-token:SECRET@github.com/o/r.git -> https://github.com/o/r.git"""
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url)


class AgentError(Exception):
    """A failure to report. `code` (a kind in app/errors.py) when the raiser knows it; else it's recognised from the text."""

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        self.code = code


def build_task_prompt(issue: JiraIssue, plan: str = "", history: str = "", deleted: list[str] | None = None) -> str:
    prompt = (
        f"You are fixing Jira ticket {issue.key} ({issue.issue_type}).\n\n"
        f"Title: {issue.summary}\n\n"
        f"Description:\n{issue.description or '(no description)'}\n\n"
    )
    if history:
        prompt += (
            f"{history}\n\n"
            "This is a FOLLOW-UP round. The ticket text above describes earlier rounds, and what they did is already "
            "in the code. Do NOT redo or re-check it. Do only what the approved plan below asks for this round.\n\n"
        )
    if plan:
        prompt += (
            "The ticket owner has reviewed and approved the plan below, including any clarifications "
            "from the discussion. Follow it. If the plan and the ticket text disagree, the plan wins.\n"
            "Change ONLY the files the plan names, and create only the new files it names. If another file seems to "
            "need a change, don't change it: say so in your final summary instead (changes to other files are undone).\n\n"
            f"Approved plan:\n{plan}\n\n"
        )
    if deleted:
        prompt += (
            "These files were ALREADY DELETED from the repository by the system, as the approved plan asked: "
            + ", ".join(f"`{d}`" for d in deleted)
            + ". Do not delete, recreate or edit them. Carry on with the rest of the plan; if nothing else remains, "
            "make no changes and reply that nothing else is needed.\n\n"
        )
    return prompt + (
        "Make the smallest correct change to the codebase that resolves this ticket. "
        "Follow the existing code style. Update or add tests if the repo has them. "
        "Do not change unrelated code."
    )


def build_fix_prompt(issue: JiraIssue, errors: list[str]) -> str:
    return (
        f"Your change for Jira ticket {issue.key} ({issue.summary}) left Python syntax errors:\n\n"
        + "\n".join(errors)
        + "\n\nFix these syntax errors. Keep the intended change for the ticket and don't change anything else."
    )


MAX_CONTEXT_FILE_BYTES = 200_000
MAX_DELETIONS = 10
AIDER_MODEL_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "aider-model-settings.yml"


def aider_settings_file(model: str, dest_dir: Path) -> Path:
    """The model settings file Aider gets: aider-model-settings.yml, plus "send no temperature" for a model that
    shouldn't get one (see pr_writer.takes_temperature) and isn't listed there. Aider would otherwise send 0."""
    if takes_temperature(model):
        return AIDER_MODEL_SETTINGS_FILE
    import yaml  # (installed with Aider)

    entries = yaml.safe_load(AIDER_MODEL_SETTINGS_FILE.read_text()) or []
    if any(e.get("name") == model for e in entries):
        return AIDER_MODEL_SETTINGS_FILE
    path = dest_dir / "model-settings.yml"
    path.write_text(yaml.safe_dump(entries + [{"name": model, "use_temperature": False}], sort_keys=False))
    return path


def mentioned_files(issue: JiraIssue, tracked: list[str], repo_dir: Path) -> list[str]:
    """Repo files the ticket names by path or basename, so Aider can edit them directly.

    Aider only edits files added to its chat; it doesn't reliably pick up names with
    spaces from the model's reply, so we pre-select them from the ticket text.
    """
    text = f"{issue.summary}\n{issue.description}".lower()
    found = []
    for path in tracked:
        if path.lower() in text or Path(path).name.lower() in text:
            if (repo_dir / path).stat().st_size <= MAX_CONTEXT_FILE_BYTES:
                found.append(path)
    return found


MAX_CONTEXT_FILES = 8
DELIVERY_TRAILER = "Agent-Delivery: "
MAX_DELIVERY_DIFF = 12000  # characters of a round's diff kept for later follow-ups


def merge_files(base: list[str], extra: list[str], tracked: list[str], repo_dir: Path) -> list[str]:
    """`base` plus the `extra` paths that really exist in the repo and fit the model's context.

    Paths come from an LLM (the planner), so anything not tracked in git is dropped.
    """
    ok = set(tracked)
    out = list(base)
    for raw in extra:
        path = raw.strip().removeprefix("./")
        f = repo_dir / path
        if path in ok and path not in out and f.is_file() and f.stat().st_size <= MAX_CONTEXT_FILE_BYTES:
            out.append(path)
    return out[:MAX_CONTEXT_FILES]


def write_aiderignore(repo_dir: Path, tracked: list[str]) -> list[str]:
    """Hide tracked files too big for the model's context (data files, notebooks) from Aider.

    Aider adds any file the ticket mentions by name to the model's context; a single 1 MB CSV
    is ~250k tokens, far beyond the model's limit. Returns the ignored paths.
    """
    big = [p for p in tracked if (repo_dir / p).is_file() and (repo_dir / p).stat().st_size > MAX_CONTEXT_FILE_BYTES]
    # gitignore syntax: anchor to the repo root and escape glob characters.
    lines = ["/" + re.sub(r"([\\*?\[\]!#])", r"\\\1", p) for p in big]
    (repo_dir / ".aiderignore").write_text("\n".join(lines) + "\n")
    return big


def crlf_files(repo_dir: Path) -> set[str]:
    """Tracked files committed with Windows (CRLF) line endings."""
    out = subprocess.run(
        ["git", "ls-files", "--eol", "-z"], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout
    # Each entry: "i/crlf  w/crlf  attr/                 \t<path>"
    return {e.split("\t", 1)[1] for e in out.split("\0") if e.startswith("i/crlf") and "\t" in e}


def restore_crlf(repo_dir: Path, paths: set[str]) -> None:
    """Undo CRLF->LF conversions so diffs only show real changes."""
    for path in paths:
        f = repo_dir / path
        if f.exists():
            data = f.read_bytes()
            fixed = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if fixed != data:
                f.write_bytes(fixed)


def branch_name(issue: JiraIssue) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue.summary.lower()).strip("-")[:40].rstrip("-")
    return f"ai/{issue.key}-{slug}" if slug else f"ai/{issue.key}"


class CodingAgent:
    def __init__(
        self, settings: Settings, job: Job | None = None, plan: str = "", plan_files: list[str] | None = None,
        followup: dict | None = None, deletions: list[str] | None = None, delivery_id: str = "",
    ):
        self.s = settings
        self.delivery_id = delivery_id  # stamped into the commit, see Session.delivery_id
        self.jira = JiraClient(settings)
        self.gh = GitHubClient(settings)
        self.job = job or Job(issue_key="?", event="manual")
        self.plan = plan  # the approved plan (with Q&A), added to Aider's instructions
        self.plan_files = plan_files or []  # files the plan identified, given to Aider like ticket-named files
        # A follow-up round: {"round", "mode": "continue"|"fresh", "branch", "pr_number", "pr_url",
        # "previous_pr", "history", "require_label"}. None = the original first-round behaviour.
        self.followup = followup
        self.deletions = list(deletions or [])  # files the approved plan deletes; removed before Aider runs
        self.deleted: list[str] = []  # the ones actually removed
        # The backend doing the coding: CODING_BACKEND, until OpenCode itself fails and Aider takes over (TOOL_FALLBACK)
        self.used_backend = settings.backend

    def _apply_deletions(self, repo_dir: Path) -> None:
        """Remove the plan's files with git, first. Only tracked files inside the repo; anything else is reported, not removed."""
        root = repo_dir.resolve()
        tracked = set(self._git("ls-files", "-z", cwd=repo_dir).split("\0"))
        for rel in self.deletions[:MAX_DELETIONS]:
            target = (repo_dir / rel).resolve()
            if rel in tracked and not rel.startswith(".git/") and root in target.parents and target.is_file():
                self._git("rm", "-q", "-f", "--", rel, cwd=repo_dir)
                self.deleted.append(rel)
                log.info("Deleted %s as the approved plan asked", rel)
            else:
                self.job.warnings.append(f"I didn't delete `{rel}`: it isn't a file in the repository branch.")

    def _git(self, *args: str, cwd: Path, timeout: int = 300) -> str:
        return self._run(["git", *args], cwd=cwd, timeout=timeout)

    def _run(self, cmd: list[str], cwd: Path, timeout: int) -> str:
        # clone / push / fetch talk to GitHub: retry those on network trouble (not on "branch not found" etc.)
        networked = cmd[0] == "git" and any(c in cmd[:6] for c in ("clone", "push", "fetch", "pull"))
        tries = 3 if networked else 1
        for attempt in range(1, tries + 1):
            try:  # (every line it prints counts as progress; the watchdog can stop it if the job is stuck)
                proc = run_process(cmd, cwd=cwd, timeout=timeout, job=self.job)
            except JobStuck as e:
                raise stuck_error(self.s, f"`{self._redact(' '.join(cmd[:2]))}`") from e
            if proc.returncode == 0:
                return proc.stdout
            out = self._redact(proc.stdout + proc.stderr)
            if attempt < tries and is_transient_git_error(out):
                log.warning("git %s failed (%s); retry %d", cmd[1], out.strip().splitlines()[-1][:120] if out.strip() else "", attempt)
                time.sleep(wait_for(attempt, None, 2.0))
                continue
            raise AgentError(f"`{self._redact(' '.join(cmd[:3]))}` failed:\n{out[-2000:]}")
        raise AssertionError("unreachable")  # pragma: no cover

    def _redact(self, text: str) -> str:
        return text.replace(self.s.github_token, "***")

    def _move(self, issue_key: str, status: str) -> None:
        """Best-effort Jira status change; never fails the job."""
        if not status:
            return
        try:
            result = self.jira.transition_to(issue_key, status)
        except Exception as e:
            log.warning("Could not move %s to %r", issue_key, status, exc_info=True)
            self.job.warnings.append(f"I couldn't move the Jira ticket to {status!r}.")
            return
        if result in ("moved", "already"):
            self.job.jira_status = status
        else:
            self.job.warnings.append(f"I couldn't move the Jira ticket to {status!r} (its workflow doesn't allow that from the current status).")
        log.info("Jira status -> %r: %s", status, result)

    def handle_issue(self, issue_key: str) -> None:
        try:
            status, detail = self._handle(issue_key)
            if status == "no_changes":
                self._move(issue_key, self.s.jira_status_on_failure)
            self.job.finish(status, detail)
        except Exception as e:
            error = self._redact(str(e))
            kind = kind_of(error, getattr(e, "code", None))
            self.job.finish("failed", error[-1500:], code=kind.code)
            log.exception("Job failed at step %r (%s)", next(reversed(self.job.steps), "start"), kind.code)
            if kind.admin:
                log.error("ADMIN: %s needs fixing [%s]: %s", issue_key, kind.code, error[:300])
            try:
                self.jira.add_comment(issue_key, f"🤖 I couldn't finish this ticket. {explain(error)} (Reference: {self.job.id})")
            except Exception:
                log.exception("Could not post failure comment to %s", issue_key)
            self._move(issue_key, self.s.jira_status_on_failure)

    def _handle(self, issue_key: str) -> tuple[str, str | None]:
        job = self.job
        with job.step("fetch_ticket"):
            issue = self.jira.get_issue(issue_key)
        log.info("Ticket: %r (%s)", issue.summary, issue.issue_type)
        fu = self.followup
        if self.s.trigger_label and (fu is None or fu.get("require_label")) and self.s.trigger_label not in issue.labels:
            log.info("Skipped: ticket no longer has label %r", self.s.trigger_label)
            return "skipped", f"label {self.s.trigger_label!r} removed"

        continuing = bool(fu and fu.get("mode") == "continue")  # push more commits onto the open PR's branch
        branch = fu["branch"] if fu else branch_name(issue)
        existing = None
        if fu is None:  # a follow-up round was already checked against the PR's state by the workflow
            with job.step("check_open_pr"):
                existing = self.gh.find_open_pr_for_issue(issue_key)
        if existing:
            log.info("Skipped: PR already open for %s: %s", issue_key, existing)
            job.pr_url = existing
            self.jira.add_comment(
                issue_key,
                f"🤖 Skipped: a pull request for this ticket is already open: {existing}\n"
                f"To run the agent again, close or merge that PR, then remove and re-add the "
                f"{{{{{self.s.trigger_label}}}}} label.",
            )
            return "skipped", "PR already open"

        what = f"Round {fu['round']}: " if fu else ""
        self.jira.add_comment(issue_key, f"🤖 {what}Coding agent picked this up. Working on branch {{{{{branch}}}}}...")
        self._move(issue_key, self.s.jira_status_in_progress)

        base = self.gh.default_branch()
        repo_dir = Path(self.s.workdir) / issue_key
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        clone_url = f"https://x-access-token:{self.s.github_token}@github.com/{self.s.github_repo}.git"
        try:
            with job.step("clone"):
                self._run(
                    ["git", "clone", "--depth", "50", "--branch", branch if continuing else base, clone_url, str(repo_dir)],
                    cwd=repo_dir.parent,
                    timeout=300,
                )
                self._git("config", "user.name", "jira-coding-agent", cwd=repo_dir)
                self._git("config", "user.email", "jira-coding-agent@users.noreply.github.com", cwd=repo_dir)
                if not continuing:
                    self._git("checkout", "-b", branch, cwd=repo_dir)
                # Keep Aider's cache files out of the commit without touching .gitignore.
                with open(repo_dir / ".git" / "info" / "exclude", "a") as f:
                    f.write("\n.aider*\n")
            log.info("Cloned %s@%s into branch %s", self.s.github_repo, branch if continuing else base, branch)

            if self.deletions:
                with job.step("delete_files"):
                    self._apply_deletions(repo_dir)
            crlf = crlf_files(repo_dir)
            cname = self.s.backend  # "aider" | "opencode": names the job steps
            with job.step(cname):  # the prompt tells the model which files are already gone, so it does the rest of the plan
                aider_log = self._run_coder(issue, repo_dir, step="code")
            backend = self.used_backend  # (Aider, if OpenCode itself failed and Aider took over)
            restore_crlf(repo_dir, crlf)
            if self.deleted and coder_problem(aider_log, backend):
                job.warnings.append("The AI service had a problem, so only the file deletions were made; the rest of the plan was not done. " + explain(coder_problem(aider_log, backend)))

            self._git("add", "-A", cwd=repo_dir)
            if backend == "opencode" and coder_problem(aider_log, backend) and self._git("status", "--porcelain", cwd=repo_dir).strip():
                # an agent can fail after it has already edited files: say so instead of presenting half a change as done
                job.warnings.append("The AI service had a problem partway through, so this change may be incomplete. " + explain(coder_problem(aider_log, backend)))
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                first_problem = coder_problem(aider_log, backend)
                fallback_model = self.s.aider_fallback_model if backend == "aider" else self.s.opencode_fallback_model
                if is_rate_limit(first_problem) and not fallback_model and self.s.rate_limit_wait_seconds:
                    log.warning("Rate limited; waiting %ss and trying the same model once more", self.s.rate_limit_wait_seconds)
                    job.warnings.append("The AI service was busy, so I waited a moment and tried again.")
                    time.sleep(self.s.rate_limit_wait_seconds)
                    with job.step(f"{cname}_retry"):
                        aider_log = self._run_coder(issue, repo_dir, step="code_retry")
                    restore_crlf(repo_dir, crlf)
                    self._git("add", "-A", cwd=repo_dir)
                    first_problem = coder_problem(aider_log, backend)
                if retryable_llm_problem(first_problem) and fallback_model:
                    log.warning("Main model failed (%s); retrying once with %s", first_problem.splitlines()[0], fallback_model)
                    job.warnings.append("The main AI service wasn't available, so a backup service did the work.")
                    with job.step(f"{cname}_fallback"):
                        aider_log = self._run_coder(issue, repo_dir, model=fallback_model, step="code_fallback")
                    backend = self.used_backend
                    restore_crlf(repo_dir, crlf)
                    self._git("add", "-A", cwd=repo_dir)
            undone = self._undo_outside_plan(repo_dir)
            if undone and not self._git("status", "--porcelain", cwd=repo_dir).strip():
                said = (f"The only changes were to files the approved plan doesn't include ({', '.join(undone)}), so I undid "
                        "them. If those files should change, ask for it and I'll plan it.")
                self.jira.add_comment(issue_key, f"🤖 I didn't change anything. {said}")
                return "no_changes", said
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                stopped = coder_stopped(aider_log, backend)
                problem = coder_problem(aider_log, backend) or ("The model returned an empty reply (possibly blocked by its content filter)." if coder_empty(aider_log, backend) and not stopped else None)
                if problem:
                    raise AgentError(f"The LLM call failed, so nothing was changed. {problem}")
                if stopped:  # the model worked; the agent ran out of requests before it changed anything
                    raise AgentError(f"Nothing was changed. {stopped}")
                log.info("%s made no changes. Output tail: %s", cname, aider_log[-500:].replace("\n", " | "))
                said = coder_reply(aider_log, backend) or "(the model gave no explanation)"
                self.jira.add_comment(
                    issue_key,
                    "🤖 I looked at the code and didn't change anything. Here is what I found:\n"
                    f"{said}",
                )
                return "no_changes", said
            log.info("Changed files: %s", self._git("diff", "--cached", "--stat", cwd=repo_dir).strip().replace("\n", " | "))

            check, aider_log = self._check_and_fix(issue, repo_dir, crlf, aider_log)
            if check.retried:
                self._undo_outside_plan(repo_dir)
            job.checks[check.name] = check.status
            if check.retried and not self._git("status", "--porcelain", cwd=repo_dir).strip():
                self.jira.add_comment(issue_key, "🤖 Coding agent's fix attempt removed all its changes; nothing to submit.")
                return "no_changes", "fix attempt removed all changes"

            jira_url = f"{self.s.jira_base_url.rstrip('/')}/browse/{issue.key}"
            with job.step("describe"):
                usage: dict[str, int] = {}
                description = describe_changes(
                    self.s.aider_model, issue, self._git("diff", "--cached", cwd=repo_dir), usage=usage,
                    fallback=self.s.aider_fallback_model,
                )
            job.add_tokens(usage.get("sent", 0), usage.get("received", 0), model=usage.get("model", self.s.aider_model),
                           step="pr_description", reported_cost=usage.get("cost"))
            if description is None:
                log.warning("PR description generation failed; using fallback description")
            summary = summary_line(description)

            with job.step("commit_push"):
                commit_msg = (
                    f"{issue.key}: {issue.summary}\n\n" + (f"{summary}\n\n" if summary else "") + f"Jira: {jira_url}"
                )
                if fu:
                    commit_msg = f"{issue.key} (round {fu['round']}): {summary or issue.summary}\n\nJira: {jira_url}"
                if self.delivery_id:
                    commit_msg += f"\n\n{DELIVERY_TRAILER}{self.delivery_id}"
                self._git("commit", "-m", commit_msg, cwd=repo_dir)
                diff_stat = self._git("diff", "--stat", f"{base}..HEAD", cwd=repo_dir) if not continuing else self._git("diff", "--stat", "HEAD~1", "HEAD", cwd=repo_dir)
                round_diff = self._git("diff", "HEAD~1", "HEAD", cwd=repo_dir)[:MAX_DELIVERY_DIFF]
                if continuing:
                    # Someone else may have pushed to the branch since we cloned it: never overwrite that.
                    try:
                        self._git("push", "origin", branch, cwd=repo_dir)
                    except AgentError as e:
                        raise AgentError(
                            f"Couldn't add the commit to {branch}: the branch changed while I worked "
                            f"(or the push was refused). Ask me again and I'll start from its latest state.\n{e}"
                        ) from e
                else:
                    # ai/* branches belong to the agent; overwrite a leftover branch from a closed PR.
                    self._git("push", "--force", "-u", "origin", branch, cwd=repo_dir)

            with job.step("create_pr"):
                if continuing:
                    pr_url, pr_number = fu["pr_url"], fu["pr_number"]
                    try:
                        self.gh.add_pr_comment(
                            pr_number,
                            f"\U0001F916 **Round {fu['round']}**: pushed a follow-up commit.\n\n"
                            + (f"{summary}\n\n" if summary else "") + f"```\n{diff_stat.strip()[-1500:]}\n```",
                        )
                    except Exception as e:
                        log.warning("Could not comment on PR #%s", pr_number, exc_info=True)
                        job.warnings.append(f"Pushed to PR #{pr_number}, but couldn't leave a comment on it.")
                else:
                    body = build_pr_body(
                        issue=issue,
                        jira_url=jira_url,
                        description=description,
                        diff_stat=diff_stat,
                        agent_log=aider_log,
                        checks_md=format_checks_md([check]),
                    )
                    if fu and fu.get("previous_pr"):
                        body = f"**Follow-up (round {fu['round']})** to {fu['previous_pr']}\n\n" + body
                    try:
                        pr_url = self.gh.create_pr(
                            head=branch, base=base, title=f"{issue.key}: {issue.summary}"
                            + (f" (follow-up {fu['round']})" if fu else ""),
                            body=body, draft=self.s.open_draft_pr,
                        )
                    except Exception as e:
                        raise AgentError(
                            f"The branch {branch} was pushed, but opening the pull request failed "
                            f"({self._redact(str(e))[:300]}). Open it by hand: "
                            f"https://github.com/{self.s.github_repo}/compare/{base}...{branch}?expand=1"
                        ) from e
                    pr_number = self.gh.pr_number(pr_url)
            job.delivery = {"branch": branch, "pr_number": pr_number, "pr_url": pr_url, "diff": round_diff, "summary": summary or "", "deleted": list(self.deleted)}
            job.pr_url = pr_url
            checks_note = (
                "\n\n⚠️ *Checks failed:* syntax errors remain; see the PR before merging."
                if check.status == "failed" else ""
            )
            self.jira.add_comment(
                issue_key,
                (f"🤖 Round {fu['round']}: pushed to the pull request: {pr_url}" if continuing else f"🤖 Pull request opened: {pr_url}")
                + (f"\n\n*What changed:* {summary}" if summary else "") + checks_note,
            )
            log.info("Opened %s", pr_url)
            self._move(issue_key, self.s.jira_status_in_review)
            return "succeeded", ("checks failed: " + "; ".join(check.errors)) if check.status == "failed" else None
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

    def _check_and_fix(self, issue: JiraIssue, repo_dir: Path, crlf: set[str], aider_log: str) -> tuple[CheckResult, str]:
        """Syntax-check the changed files; on errors, give Aider one attempt to fix them."""
        def changed() -> list[str]:
            out = self._git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", cwd=repo_dir)
            return [f for f in out.split("\0") if f]

        with self.job.step("checks"):
            result = python_syntax_check(repo_dir, changed())
        log.info("Check %s: %s - %s", result.name, result.status, result.summary)
        if result.status != "failed":
            return result, aider_log

        log.warning("Syntax errors, asking %s to fix them: %s", self.used_backend, " | ".join(result.errors))
        with self.job.step(f"{self.used_backend}_fix"):
            fix_log = self._run_coder(
                issue, repo_dir, message=build_fix_prompt(issue, result.errors), files=result.failed_files, step="fix"
            )
        restore_crlf(repo_dir, crlf)
        self._git("add", "-A", cwd=repo_dir)

        result = python_syntax_check(repo_dir, changed())
        result.retried = True
        log.info("Check %s after fix attempt: %s - %s", result.name, result.status, result.summary)
        return result, f"{aider_log}\n\n----- fix attempt after syntax check -----\n{fix_log}"

    # ------------------------------------------------------------------------------------------------------------
    # Coding step: one entry point for both backends. Everything outside this (clone, commit, push, PR) is shared.
    # ------------------------------------------------------------------------------------------------------------
    def _run_coder(
        self, issue: JiraIssue, repo_dir: Path, message: str | None = None, files: list[str] | None = None,
        *, model: str | None = None, step: str = "code",
    ) -> str:
        """Run the coding backend once, record its token use on the job, and return its text log.

        If OpenCode itself fails (crashed, e.g. out of memory; stuck; timed out; not installed; used up its requests
        without changing anything), Aider does the same step instead, once (TOOL_FALLBACK). Model problems (rate limits,
        quotas) are not this: they get the backup model in _handle."""
        if self.used_backend == "opencode":
            before = self._git("status", "--porcelain", cwd=repo_dir)
            try:
                out = self._run_opencode(issue, repo_dir, message=message, files=files, model=model, step=step)
                why = oc.tool_problem(out)
                if why == oc.STEP_LIMIT and self._git("status", "--porcelain", cwd=repo_dir) != before:
                    why = None  # it changed things before it ran out: kept, with a warning (see _run_opencode)
            except AgentError as e:
                why = tool_failure(e)
                if not (why and self._can_fall_back()):
                    raise
            if not (why and self._can_fall_back()):
                return out
            return self._aider_instead(issue, repo_dir, why, message=message, files=files, step=step)
        return self._aider_step(issue, repo_dir, message=message, files=files, model=model, step=step)

    def _undo_outside_plan(self, repo_dir: Path) -> list[str]:
        """With an approved plan, only the files it names may change: undo (and report) changes to any other file.
        Returns the files undone. Without a plan (the direct flow), or a plan that names no files, nothing is checked."""
        allowed = plan_paths(self.plan) | set(self.plan_files) | set(self.deletions)
        if not self.plan or not allowed:
            return []
        out = self._git("diff", "--cached", "--name-only", "--no-renames", "-z", cwd=repo_dir)
        outside = [p for p in out.split("\0") if p and not path_allowed(p, allowed)]
        for path in outside:
            if self._git("ls-tree", "--name-only", "HEAD", "--", path, cwd=repo_dir).strip():
                self._git("restore", "--source=HEAD", "--staged", "--worktree", "--", path, cwd=repo_dir)
            else:  # a file the agent added
                self._git("rm", "-q", "--cached", "-f", "--", path, cwd=repo_dir)
                (repo_dir / path).unlink(missing_ok=True)
        if outside:
            log.warning("Undid changes outside the approved plan: %s", ", ".join(outside))
            self.job.warnings.append(
                "I undid changes to files the approved plan doesn't include: " + ", ".join(f"`{p}`" for p in outside)
                + ". If they should change, ask for it and I'll plan it."
            )
        return outside

    def _can_fall_back(self) -> bool:
        return can_fall_back(self.s)

    def _aider_instead(self, issue: JiraIssue, repo_dir: Path, why: str, *, message, files, step: str) -> str:
        """OpenCode failed as a tool: undo its half-done edits and let Aider do this step (and the rest of the job)."""
        log.warning("OpenCode failed (%s); Aider does this step instead", why)
        self.job.warnings.append(f"The coding tool OpenCode failed ({why}), so Aider did the work instead.")
        # Unstaged edits are OpenCode's; staged ones are the app's (deletions, or an earlier step's accepted work).
        self._git("checkout", "--", ".", cwd=repo_dir)
        self._git("clean", "-fdq", cwd=repo_dir)
        self.used_backend = "aider"
        self.job.stuck = False
        self.job.progress()
        with self.job.step("aider_instead"):
            return self._aider_step(issue, repo_dir, message=message, files=files, step=f"{step}_aider")

    def _aider_step(self, issue: JiraIssue, repo_dir: Path, *, message=None, files=None, model=None, step: str) -> str:
        kwargs: dict = {}
        if message is not None:
            kwargs["message"] = message
        if files is not None:
            kwargs["files"] = files
        if model is not None:
            kwargs["model"] = model
        out = self._run_aider(issue, repo_dir, **kwargs)
        self.job.add_aider(out, model=model or self.s.aider_model, step=step)
        return out

    def _repair_git_state(self, repo_dir: Path, branch: str, head: str) -> None:
        """Undo anything the agent did to git itself. Only this app commits: the agent's commits are folded back into
        the working tree (so they end up in the app's single commit) and it is put back on the app's branch."""
        current = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir).strip()
        if current != branch:
            self.job.warnings.append("The coding agent switched git branches on its own; I put it back.")
            try:
                self._git("checkout", "-q", branch, cwd=repo_dir)
            except AgentError as e:
                raise AgentError(f"The coding agent left the repository on branch {current!r} and I couldn't switch back to {branch!r}.") from e
        if self._git("rev-parse", "HEAD", cwd=repo_dir).strip() != head:
            self.job.warnings.append("The coding agent made a git commit on its own; I folded its work into my own commit.")
            self._git("reset", "--soft", head, cwd=repo_dir)

    def _run_opencode(
        self, issue: JiraIssue, repo_dir: Path, message: str | None = None, files: list[str] | None = None,
        *, model: str | None = None, step: str = "code",
    ) -> str:
        """Run OpenCode once in the clone. It may edit files (and run tests); it may not commit or push."""
        s, job = self.s, self.job
        model = model or s.opencode_model
        if files is None:
            tracked = [p for p in self._git("ls-files", "-z", cwd=repo_dir).split("\0") if p]
            files = merge_files(mentioned_files(issue, tracked, repo_dir), self.plan_files, tracked, repo_dir)
            job.files = files
            log.info("Files hinted to OpenCode (ticket + plan): %s", files or "none (it will search the repo itself)")
        task = message or build_task_prompt(issue, self.plan, (self.followup or {}).get("history", ""), self.deleted)
        prompt = oc.build_prompt(task, files, s.opencode_allow_bash)

        run_dir = Path(s.workdir) / f"{issue.key}-opencode"  # private HOME/session store for this run, deleted afterwards
        shutil.rmtree(run_dir, ignore_errors=True)
        (run_dir / "home").mkdir(parents=True)
        cache = Path(s.workdir) / ".opencode-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env = oc.build_env(models=[model], home=run_dir / "home", cache=cache, pass_env=s.opencode_pass_env)
        secrets = oc.secret_values(env)

        def redact(text: str) -> str:
            text = self._redact(text)
            for value in secrets:
                text = text.replace(value, "***")
            return text

        exclude = repo_dir / ".git" / "info" / "exclude"
        if ".opencode/" not in exclude.read_text():  # (a syntax-fix run uses the same clone)
            with open(exclude, "a") as f:
                f.write("\n.opencode/\n")
        # Before the agent runs: remember git's config and the branch position, and take the GitHub token out of the
        # remote URL so a shell command like `git remote -v` or `cat .git/config` can't reveal it.
        git_config = repo_dir / ".git" / "config"
        saved_config = git_config.read_bytes()
        head = self._git("rev-parse", "HEAD", cwd=repo_dir).strip()
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir).strip()
        # (`config --get`, not `remote get-url`: the latter applies url.*.insteadOf rewrites and would return a different URL)
        origin = self._git("config", "--get", "remote.origin.url", cwd=repo_dir).strip()
        self._git("remote", "set-url", "origin", strip_credentials(origin), cwd=repo_dir)

        label = "fix" if message else "code"
        record_model = f"opencode:{model}"
        prompt_sent = redact(task)
        log.info("Running OpenCode with model %s (bash %s, max %s model requests)", model, "on" if s.opencode_allow_bash else "off", s.opencode_max_steps or "unlimited")
        try:
            try:
                run = oc.run_opencode(
                    binary=s.opencode_bin, cwd=repo_dir, prompt=prompt, model=model, env=env,
                    config=oc.build_config(allow_bash=s.opencode_allow_bash, extra_json=s.opencode_extra_config),
                    timeout=s.opencode_timeout_seconds, max_steps=s.opencode_max_steps, variant=s.opencode_variant,
                    title=f"{issue.key} {label}", job=job,
                )
            except oc.OpenCodeNotInstalled as e:
                job.record_run(label, record_model, prompt_sent, f"(OpenCode could not start: {e})", ok=False)
                raise AgentError(
                    f"OpenCode could not be started ({redact(str(e))}). Install it (the Dockerfile does) or set CODING_BACKEND=aider.",
                    code="opencode_missing",
                ) from e
        finally:
            # After the agent: restore git's config byte for byte (this also puts the token URL back for the app's own
            # push) and delete any hooks it planted, so nothing the agent wrote can run when the app commits or pushes.
            git_config.write_bytes(saved_config)
            shutil.rmtree(repo_dir / ".git" / "hooks", ignore_errors=True)
            shutil.rmtree(run_dir, ignore_errors=True)
            self._repair_git_state(repo_dir, branch, head)

        text = oc.render_log(run, redact)
        if run.stopped in ("timeout", "stuck"):
            self._add_opencode_tokens(run, model, step)
            job.record_run(label, record_model, prompt_sent, text, ok=False)
            if run.stopped == "stuck":
                raise stuck_error(s, "OpenCode")
            raise AgentError(f"OpenCode timed out after {s.opencode_timeout_seconds}s", code="tool_timeout")
        self._add_opencode_tokens(run, model, step)
        if run.stopped == "max_steps":
            job.warnings.append(
                f"The coding agent was stopped after {len(run.steps)} model requests (OPENCODE_MAX_STEPS), so the change may be incomplete."
            )
        log.info("OpenCode finished: %d request(s), %d tokens in / %d out, %d tool call(s), %.1fs",
                 len(run.steps), run.sent, run.received, len(run.tools), run.duration_s)
        job.record_run(label, record_model, prompt_sent, text, ok=not run.errors)
        return text

    def _add_opencode_tokens(self, run: "oc.OpenCodeRun", model: str, step: str) -> None:
        # One call entry per model request, so /usage shows how many requests the agent needed (Aider counts one per run).
        for st in run.steps or [{"sent": 0, "received": 0, "cache_hit": 0, "cache_write": 0, "cost": 0.0}]:
            self.job.add_tokens(
                st["sent"], st["received"], model=model, step=step, backend="opencode",
                cache_hit=st["cache_hit"], cache_write=st["cache_write"], reported_cost=round(st["cost"], 6),
            )

    def run_opencode_readonly(self, issue: JiraIssue, repo_dir: Path, prompt: str, *, model: str, step: str,
                              timeout: int, max_steps: int) -> str:
        """OpenCode with no edit and no shell rights (used for planning): it reads and searches; its last message is
        the answer. The caller's clone must not hold credentials (the planner strips them right after cloning)."""
        s, job = self.s, self.job
        run_dir = Path(s.workdir) / f"{issue.key}-opencode-{step}"
        shutil.rmtree(run_dir, ignore_errors=True)
        (run_dir / "home").mkdir(parents=True)
        cache = Path(s.workdir) / ".opencode-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env = oc.build_env(models=[model], home=run_dir / "home", cache=cache, pass_env=s.opencode_pass_env)
        secrets = oc.secret_values(env)

        def redact(text: str) -> str:
            text = self._redact(text)
            for value in secrets:
                text = text.replace(value, "***")
            return text

        record_model = f"opencode:{model}"
        log.info("Running OpenCode read-only (%s) with model %s, max %s model requests", step, model, max_steps or "unlimited")
        try:
            run = oc.run_opencode(
                binary=s.opencode_bin, cwd=repo_dir, prompt=prompt, model=model, env=env,
                config=oc.build_config(allow_bash=False, read_only=True, extra_json=s.opencode_extra_config),
                timeout=timeout, max_steps=max_steps, variant=s.opencode_variant, title=f"{issue.key} {step}", job=job,
            )
        except oc.OpenCodeNotInstalled as e:
            job.record_run(step, record_model, redact(prompt), f"(OpenCode could not start: {e})", ok=False)
            raise AgentError(f"OpenCode could not be started ({redact(str(e))}). Install it or set PLANNER_BACKEND=aider.",
                             code="opencode_missing") from e
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        text = oc.render_log(run, redact)
        self._add_opencode_tokens(run, model, step)
        log.info("OpenCode (%s) finished: %d request(s), %d tokens in / %d out, %d tool call(s), %.1fs",
                 step, len(run.steps), run.sent, run.received, len(run.tools), run.duration_s)
        job.record_run(step, record_model, redact(prompt), text, ok=not run.errors and not run.stopped)
        if run.stopped == "stuck":  # (like the coding step: a stuck run is that, not "an empty reply")
            raise stuck_error(s, f"OpenCode ({step})")
        return text

    def _run_aider(
        self,
        issue: JiraIssue,
        repo_dir: Path,
        message: str | None = None,
        files: list[str] | None = None,
        *,
        model: str | None = None,
        chat_mode: str | None = None,
        timeout: int | None = None,
        extra_files: list[str] | None = None,
    ) -> str:
        """Run Aider once. chat_mode="ask" makes it read-only (used for planning)."""
        history_dir = Path(self.s.workdir) / f"{issue.key}-aider"
        history_dir.mkdir(parents=True, exist_ok=True)
        if files is None:
            tracked = [p for p in self._git("ls-files", "-z", cwd=repo_dir).split("\0") if p]
            hidden = write_aiderignore(repo_dir, tracked)
            if hidden:
                log.info("Hidden from Aider (too large for the model): %d file(s)", len(hidden))
            files = mentioned_files(issue, tracked, repo_dir)
            extra = self.plan_files if extra_files is None else extra_files
            files = merge_files(files, extra, tracked, repo_dir)
            self.job.files = files
            log.info("Files given to Aider (ticket + plan): %s", files or "none (Aider will use its repo map)")
        cmd = [
            "aider",
            "--model", model or self.s.aider_model,
            "--edit-format", self.s.aider_edit_format,
            "--message", message or build_task_prompt(issue, self.plan, (self.followup or {}).get("history", ""), self.deleted),
            "--yes-always",
            # --yes-always would otherwise auto-run any shell command the model suggests.
            "--no-suggest-shell-commands",
            # Aider would otherwise fetch every URL in the prompt (e.g. the earlier PR link) and try to
            # install pandoc into the working directory, which ended up committed as a stray .deb file.
            "--no-detect-urls",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-gitignore",
            "--no-check-update",
            "--no-show-model-warnings",
            "--no-pretty",
            "--no-stream",
            "--analytics-disable",
            "--chat-history-file", str(history_dir / "chat.md"),
            "--input-history-file", str(history_dir / "input.txt"),
            "--model-settings-file", str(aider_settings_file(model or self.s.aider_model, history_dir)),
        ]
        if files:
            # The named files are all the model needs; the repo map would only add tokens.
            cmd += ["--map-tokens", str(self.s.aider_map_tokens_when_files_named)]
        if self.s.aider_reasoning_effort:
            cmd += ["--reasoning-effort", self.s.aider_reasoning_effort, "--no-check-model-accepts-settings"]
        if chat_mode:
            cmd += ["--chat-mode", chat_mode]
        cmd += files
        timeout = timeout or self.s.aider_timeout_seconds
        log.info("Running aider with model %s%s", model or self.s.aider_model, f" (mode {chat_mode})" if chat_mode else "")
        step = "plan" if chat_mode == "ask" else ("fix" if message else "code")
        prompt_sent = self._redact(message or build_task_prompt(issue, self.plan, (self.followup or {}).get("history", "")))
        try:
            out = self._redact(self._run(cmd, cwd=repo_dir, timeout=timeout))
            self.job.record_run(step, model or self.s.aider_model, prompt_sent, out)
            return out
        except subprocess.TimeoutExpired as e:
            self.job.record_run(step, model or self.s.aider_model, prompt_sent, f"(timed out after {timeout}s)", ok=False)
            raise AgentError(f"Aider timed out after {timeout}s") from e
        except Exception as e:
            self.job.record_run(step, model or self.s.aider_model, prompt_sent, self._redact(str(e)), ok=False)
            raise
        finally:
            shutil.rmtree(history_dir, ignore_errors=True)
