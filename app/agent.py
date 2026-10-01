"""The coding job: clone repo -> branch -> run Aider -> commit -> push -> open PR."""
import logging
import re
import shutil
import subprocess
from pathlib import Path

from app.checks import CheckResult, format_checks_md, python_syntax_check
from app.config import Settings
from app.github_client import GitHubClient
from app.jira_client import JiraClient, JiraIssue
from app.jobs import Job, parse_aider_tokens
from app.pr_writer import build_pr_body, describe_changes, summary_line

log = logging.getLogger(__name__)


class AgentError(Exception):
    pass


def build_task_prompt(issue: JiraIssue) -> str:
    return (
        f"You are fixing Jira ticket {issue.key} ({issue.issue_type}).\n\n"
        f"Title: {issue.summary}\n\n"
        f"Description:\n{issue.description or '(no description)'}\n\n"
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
    def __init__(self, settings: Settings, job: Job | None = None):
        self.s = settings
        self.jira = JiraClient(settings)
        self.gh = GitHubClient(settings)
        self.job = job or Job(issue_key="?", event="manual")

    def _git(self, *args: str, cwd: Path, timeout: int = 300) -> str:
        return self._run(["git", *args], cwd=cwd, timeout=timeout)

    def _run(self, cmd: list[str], cwd: Path, timeout: int) -> str:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            out = self._redact(proc.stdout + proc.stderr)
            raise AgentError(f"`{self._redact(' '.join(cmd[:3]))}` failed:\n{out[-2000:]}")
        return proc.stdout

    def _redact(self, text: str) -> str:
        return text.replace(self.s.github_token, "***")

    def _move(self, issue_key: str, status: str) -> None:
        """Best-effort Jira status change; never fails the job."""
        if not status:
            return
        try:
            result = self.jira.transition_to(issue_key, status)
        except Exception:
            log.warning("Could not move %s to %r", issue_key, status, exc_info=True)
            return
        if result in ("moved", "already"):
            self.job.jira_status = status
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
                self.jira.add_comment(issue_key, f"🤖 Coding agent failed:\n{{noformat}}{error[-1500:]}{{noformat}}")
            except Exception:
                log.exception("Could not post failure comment to %s", issue_key)
            self._move(issue_key, self.s.jira_status_on_failure)

    def _handle(self, issue_key: str) -> tuple[str, str | None]:
        job = self.job
        with job.step("fetch_ticket"):
            issue = self.jira.get_issue(issue_key)
        log.info("Ticket: %r (%s)", issue.summary, issue.issue_type)
        if self.s.trigger_label and self.s.trigger_label not in issue.labels:
            log.info("Skipped: ticket no longer has label %r", self.s.trigger_label)
            return "skipped", f"label {self.s.trigger_label!r} removed"

        branch = branch_name(issue)
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

        self.jira.add_comment(issue_key, f"🤖 Coding agent picked this up. Working on branch {{{{{branch}}}}}...")
        self._move(issue_key, self.s.jira_status_in_progress)

        base = self.gh.default_branch()
        repo_dir = Path(self.s.workdir) / issue_key
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        clone_url = f"https://x-access-token:{self.s.github_token}@github.com/{self.s.github_repo}.git"
        try:
            with job.step("clone"):
                self._run(
                    ["git", "clone", "--depth", "50", "--branch", base, clone_url, str(repo_dir)],
                    cwd=repo_dir.parent,
                    timeout=300,
                )
                self._git("config", "user.name", "jira-coding-agent", cwd=repo_dir)
                self._git("config", "user.email", "jira-coding-agent@users.noreply.github.com", cwd=repo_dir)
                self._git("checkout", "-b", branch, cwd=repo_dir)
                # Keep Aider's cache files out of the commit without touching .gitignore.
                with open(repo_dir / ".git" / "info" / "exclude", "a") as f:
                    f.write("\n.aider*\n")
            log.info("Cloned %s@%s into branch %s", self.s.github_repo, base, branch)

            crlf = crlf_files(repo_dir)
            with job.step("aider"):
                aider_log = self._run_aider(issue, repo_dir)
            job.add_tokens(*parse_aider_tokens(aider_log))
            restore_crlf(repo_dir, crlf)

            self._git("add", "-A", cwd=repo_dir)
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                log.info("Aider made no changes. Output tail: %s", aider_log[-500:].replace("\n", " | "))
                self.jira.add_comment(
                    issue_key,
                    "🤖 Coding agent finished but made no changes. "
                    "Try adding more detail (file names, expected behaviour) to the description.\n"
                    f"{{noformat}}{aider_log[-1500:]}{{noformat}}",
                )
                return "no_changes", "Aider made no changes"
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
            job.add_tokens(usage.get("sent", 0), usage.get("received", 0))
            if description is None:
                log.warning("PR description generation failed; using fallback description")
            summary = summary_line(description)

            with job.step("commit_push"):
                commit_msg = (
                    f"{issue.key}: {issue.summary}\n\n" + (f"{summary}\n\n" if summary else "") + f"Jira: {jira_url}"
                )
                self._git("commit", "-m", commit_msg, cwd=repo_dir)
                diff_stat = self._git("diff", "--stat", f"{base}..HEAD", cwd=repo_dir)
                # ai/* branches belong to the agent; overwrite a leftover branch from a closed PR.
                self._git("push", "--force", "-u", "origin", branch, cwd=repo_dir)

            with job.step("create_pr"):
                pr_url = self.gh.create_pr(
                    head=branch,
                    base=base,
                    title=f"{issue.key}: {issue.summary}",
                    body=build_pr_body(
                        issue=issue,
                        jira_url=jira_url,
                        description=description,
                        diff_stat=diff_stat,
                        agent_log=aider_log,
                        checks_md=format_checks_md([check]),
                    ),
                    draft=self.s.open_draft_pr,
                )
            job.pr_url = pr_url
            checks_note = (
                "\n\n⚠️ *Checks failed:* syntax errors remain; see the PR before merging."
                if check.status == "failed" else ""
            )
            self.jira.add_comment(
                issue_key,
                f"🤖 Pull request opened: {pr_url}" + (f"\n\n*What changed:* {summary}" if summary else "") + checks_note,
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
        self.job.add_tokens(*parse_aider_tokens(fix_log))
        restore_crlf(repo_dir, crlf)
        self._git("add", "-A", cwd=repo_dir)

        result = python_syntax_check(repo_dir, changed())
        result.retried = True
        log.info("Check %s after fix attempt: %s - %s", result.name, result.status, result.summary)
        return result, f"{aider_log}\n\n----- fix attempt after syntax check -----\n{fix_log}"

    def _run_aider(
        self, issue: JiraIssue, repo_dir: Path, message: str | None = None, files: list[str] | None = None
    ) -> str:
        history_dir = Path(self.s.workdir) / f"{issue.key}-aider"
        history_dir.mkdir(parents=True, exist_ok=True)
        if files is None:
            tracked = [p for p in self._git("ls-files", "-z", cwd=repo_dir).split("\0") if p]
            hidden = write_aiderignore(repo_dir, tracked)
            if hidden:
                log.info("Hidden from Aider (too large for the model): %d file(s)", len(hidden))
            files = mentioned_files(issue, tracked, repo_dir)
            self.job.files = files
            log.info("Files named in ticket: %s", files or "none (Aider will use its repo map)")
        cmd = [
            "aider",
            "--model", self.s.aider_model,
            "--edit-format", self.s.aider_edit_format,
            "--message", message or build_task_prompt(issue),
            "--yes-always",
            # --yes-always would otherwise auto-run any shell command the model suggests.
            "--no-suggest-shell-commands",
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
        cmd += files
        log.info("Running aider with model %s", self.s.aider_model)
        try:
            return self._redact(self._run(cmd, cwd=repo_dir, timeout=self.s.aider_timeout_seconds))
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"Aider timed out after {self.s.aider_timeout_seconds}s") from e
        finally:
            shutil.rmtree(history_dir, ignore_errors=True)
