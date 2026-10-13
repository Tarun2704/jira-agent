"""The coding job: clone repo -> branch -> run Aider -> commit -> push -> open PR."""
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.friendly import explain
from app.checks import CheckResult, format_checks_md, python_syntax_check
from app.config import Settings
from app.github_client import GitHubClient
from app.jira_client import JiraClient, JiraIssue
from app.jobs import Job, parse_aider_tokens
from app.retry import is_transient_git_error, wait_for
from app.pr_writer import build_pr_body, describe_changes, summary_line

log = logging.getLogger(__name__)



# Aider prints LLM provider failures into its output instead of failing loudly. These are recognised
# only at the points where the run produced nothing useful (no plan, no changes), because Aider also
# prints transient errors that it then retries successfully.
_LLM_PROBLEMS = [
    (r"ContextWindowExceeded|estimated chat context|exceeds the [\d,]+ token|context (length|window)|request too large|reduce the length|exceeds the (maximum|limit)|too many tokens|token limit",
     "The request was too large for the model (token/context limit). Use a smaller ticket or fewer files, or a model with a bigger limit."),
    (r"RateLimitError|rate.?limit|tokens per (minute|day)|\bTPM\b|\bTPD\b|quota|insufficient_quota|429",
     "The LLM provider's rate or usage limit was hit. Wait a bit and reply to try again."),
    (r"ContentPolicyViolation|content[_ ]filter|content management policy|safety|blocked|moderation",
     "The model refused or blocked the request (content filter)."),
    (r"AuthenticationError|invalid api key|incorrect api key|unauthorized|\b401\b|PermissionDenied|\b403\b",
     "The LLM API key was rejected. Check the key and the model name."),
    (r"NotFoundError|model.*not (found|exist)|does not exist|decommissioned|\b404\b",
     "The model name wasn't found at the provider. Check AIDER_MODEL / PLANNER_MODEL."),
    (r"APIConnectionError|ServiceUnavailable|InternalServerError|overloaded|\b50[0-4]\b|timed? ?out|Timeout|APIError",
     "The LLM provider was unavailable or timed out. Try again in a minute."),
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
    if re.search(r"PerDay", output):  # a daily quota: waiting a minute can't help, so say so (and let a fallback model take over)
        model = (re.search(r'"model":\s*"([^"]+)"', output) or [None, "the model"])[1]
        delay = re.search(r'"retryDelay":\s*"(\d+)s"', output)
        resets = f" It resets in about {max(1, round(int(delay.group(1)) / 3600))} hour(s)." if delay else ""
        return (f"The daily free quota for {model} is used up (daily quota: free keys allow only a few requests per day per model).{resets} "
                "Set a fallback model (PLANNER_FALLBACK_MODEL / AIDER_FALLBACK_MODEL), switch models, or use a key with billing.\n"
                f"Provider said: {lines[0][:200]}")
    for pattern, reason in _LLM_PROBLEMS:
        if re.search(pattern, blob, re.I):
            return f"{reason}\nProvider said: {lines[0][:300]}"
    return None


def retryable_llm_problem(problem: str | None) -> bool:
    """Rate limits and provider outages can pass or be avoided with another model; a bad key or a blocked prompt can't."""
    return bool(problem) and ("rate or usage limit" in problem or "daily quota" in problem or "unavailable or timed out" in problem)


_NOISE = ("Aider v", "Main model:", "Model:", "Git repo:", "Repo-map:", "Added ", "Tokens:", "Cost:", "Warning:",
          "Analytics", "Scraping", "Page didn't", "Unable to install pandoc", "http")


def aider_reply(output: str, limit: int = 1200) -> str:
    """What the model actually said, without Aider's start-up lines (for telling you why nothing changed)."""
    body = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith(_NOISE) and "requirements.txt" != ln.strip()]
    return "\n".join(body).strip()[-limit:]


def is_rate_limit(problem: str | None) -> bool:
    return bool(problem) and "rate or usage limit" in problem


def empty_reply(output: str) -> bool:
    """True if Aider ran but the model said (almost) nothing: typically a blocked or empty completion."""
    body = [
        ln for ln in output.splitlines()
        if ln.strip() and not ln.startswith(("Aider v", "Main model:", "Model:", "Git repo:", "Repo-map:", "Added ", "Tokens:", "Cost:", "Warning:"))
    ]
    return len("\n".join(body).strip()) < 40

class AgentError(Exception):
    pass


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
            "from the discussion. Follow it. If the plan and the ticket text disagree, the plan wins.\n\n"
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
        followup: dict | None = None, deletions: list[str] | None = None,
    ):
        self.s = settings
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
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
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
            self.job.finish("failed", error[-1500:])
            log.exception("Job failed at step %r", next(reversed(self.job.steps), "start"))
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
            with job.step("aider"):  # the prompt tells the model which files are already gone, so it does the rest of the plan
                aider_log = self._run_aider(issue, repo_dir)
            job.add_tokens(*parse_aider_tokens(aider_log), model=self.s.aider_model, step="code")
            restore_crlf(repo_dir, crlf)
            if self.deleted and llm_problem(aider_log):
                job.warnings.append("The AI service had a problem, so only the file deletions were made; the rest of the plan was not done. " + explain(llm_problem(aider_log)))

            self._git("add", "-A", cwd=repo_dir)
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                first_problem = llm_problem(aider_log)
                if is_rate_limit(first_problem) and not self.s.aider_fallback_model and self.s.rate_limit_wait_seconds:
                    log.warning("Rate limited; waiting %ss and trying the same model once more", self.s.rate_limit_wait_seconds)
                    job.warnings.append("The AI service was busy, so I waited a moment and tried again.")
                    time.sleep(self.s.rate_limit_wait_seconds)
                    with job.step("aider_retry"):
                        aider_log = self._run_aider(issue, repo_dir)
                    job.add_tokens(*parse_aider_tokens(aider_log), model=self.s.aider_model, step="code_retry")
                    restore_crlf(repo_dir, crlf)
                    self._git("add", "-A", cwd=repo_dir)
                    first_problem = llm_problem(aider_log)
                if retryable_llm_problem(first_problem) and self.s.aider_fallback_model:
                    log.warning("Main model failed (%s); retrying once with %s", first_problem.splitlines()[0], self.s.aider_fallback_model)
                    job.warnings.append("The main AI service wasn't available, so a backup service did the work.")
                    with job.step("aider_fallback"):
                        aider_log = self._run_aider(issue, repo_dir, model=self.s.aider_fallback_model)
                    job.add_tokens(*parse_aider_tokens(aider_log), model=self.s.aider_fallback_model, step="code_fallback")
                    restore_crlf(repo_dir, crlf)
                    self._git("add", "-A", cwd=repo_dir)
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                problem = llm_problem(aider_log) or ("The model returned an empty reply (possibly blocked by its content filter)." if empty_reply(aider_log) else None)
                if problem:
                    raise AgentError(f"The LLM call failed, so nothing was changed. {problem}")
                log.info("Aider made no changes. Output tail: %s", aider_log[-500:].replace("\n", " | "))
                said = aider_reply(aider_log) or "(the model gave no explanation)"
                self.jira.add_comment(
                    issue_key,
                    "🤖 I looked at the code and didn't change anything. Here is what I found:\n"
                    f"{said}",
                )
                return "no_changes", said
            log.info("Changed files: %s", self._git("diff", "--cached", "--stat", cwd=repo_dir).strip().replace("\n", " | "))

            check, aider_log = self._check_and_fix(issue, repo_dir, crlf, aider_log)
            job.checks[check.name] = check.status
            if check.retried and not self._git("status", "--porcelain", cwd=repo_dir).strip():
                self.jira.add_comment(issue_key, "🤖 Coding agent's fix attempt removed all its changes; nothing to submit.")
                return "no_changes", "fix attempt removed all changes"

            jira_url = f"{self.s.jira_base_url.rstrip('/')}/browse/{issue.key}"
            with job.step("describe"):
                usage: dict[str, int] = {}
                description = describe_changes(
                    self.s.aider_model, issue, self._git("diff", "--cached", cwd=repo_dir), usage=usage
                )
            job.add_tokens(usage.get("sent", 0), usage.get("received", 0), model=self.s.aider_model, step="pr_description")
            if description is None:
                log.warning("PR description generation failed; using fallback description")
            summary = summary_line(description)

            with job.step("commit_push"):
                commit_msg = (
                    f"{issue.key}: {issue.summary}\n\n" + (f"{summary}\n\n" if summary else "") + f"Jira: {jira_url}"
                )
                if fu:
                    commit_msg = f"{issue.key} (round {fu['round']}): {summary or issue.summary}\n\nJira: {jira_url}"
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

        log.warning("Syntax errors, asking Aider to fix them: %s", " | ".join(result.errors))
        with self.job.step("aider_fix"):
            fix_log = self._run_aider(
                issue, repo_dir, message=build_fix_prompt(issue, result.errors), files=result.failed_files
            )
        self.job.add_tokens(*parse_aider_tokens(fix_log), model=self.s.aider_model, step="fix")
        restore_crlf(repo_dir, crlf)
        self._git("add", "-A", cwd=repo_dir)

        result = python_syntax_check(repo_dir, changed())
        result.retried = True
        log.info("Check %s after fix attempt: %s - %s", result.name, result.status, result.summary)
        return result, f"{aider_log}\n\n----- fix attempt after syntax check -----\n{fix_log}"

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
            "--model-settings-file", str(AIDER_MODEL_SETTINGS_FILE),
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
