"""The coding job: clone repo -> branch -> run Aider -> commit -> push -> open PR."""
import logging
import re
import shutil
import subprocess
from pathlib import Path

from app.config import Settings
from app.github_client import GitHubClient
from app.jira_client import JiraClient, JiraIssue
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


MAX_CONTEXT_FILE_BYTES = 200_000


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
    def __init__(self, settings: Settings):
        self.s = settings
        self.jira = JiraClient(settings)
        self.gh = GitHubClient(settings)

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

    def handle_issue(self, issue_key: str) -> None:
        try:
            self._handle(issue_key)
        except Exception as e:
            log.exception("Job for %s failed", issue_key)
            try:
                self.jira.add_comment(
                    issue_key, f"🤖 Coding agent failed:\n{{noformat}}{self._redact(str(e))[-1500:]}{{noformat}}"
                )
            except Exception:
                log.exception("Could not post failure comment to %s", issue_key)

    def _handle(self, issue_key: str) -> None:
        issue = self.jira.get_issue(issue_key)
        if self.s.trigger_label and self.s.trigger_label not in issue.labels:
            log.info("%s lacks label %r, skipping", issue_key, self.s.trigger_label)
            return

        branch = branch_name(issue)
        if existing := self.gh.find_open_pr_for_issue(issue_key):
            log.info("PR already open for %s: %s", issue_key, existing)
            self.jira.add_comment(
                issue_key,
                f"🤖 Skipped: a pull request for this ticket is already open: {existing}\n"
                f"To run the agent again, close or merge that PR, then remove and re-add the "
                f"{{{{{self.s.trigger_label}}}}} label.",
            )
            return

        self.jira.add_comment(issue_key, f"🤖 Coding agent picked this up. Working on branch {{{{{branch}}}}}...")

        base = self.gh.default_branch()
        repo_dir = Path(self.s.workdir) / issue_key
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        clone_url = f"https://x-access-token:{self.s.github_token}@github.com/{self.s.github_repo}.git"
        try:
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

            crlf = crlf_files(repo_dir)
            aider_log = self._run_aider(issue, repo_dir)
            restore_crlf(repo_dir, crlf)

            self._git("add", "-A", cwd=repo_dir)
            if not self._git("status", "--porcelain", cwd=repo_dir).strip():
                self.jira.add_comment(
                    issue_key,
                    "🤖 Coding agent finished but made no changes. "
                    "Try adding more detail (file names, expected behaviour) to the description.\n"
                    f"{{noformat}}{aider_log[-1500:]}{{noformat}}",
                )
                return

            jira_url = f"{self.s.jira_base_url.rstrip('/')}/browse/{issue.key}"
            description = describe_changes(
                self.s.aider_model, issue, self._git("diff", "--cached", cwd=repo_dir)
            )
            summary = summary_line(description)

            commit_msg = f"{issue.key}: {issue.summary}\n\n" + (f"{summary}\n\n" if summary else "") + f"Jira: {jira_url}"
            self._git("commit", "-m", commit_msg, cwd=repo_dir)
            diff_stat = self._git("diff", "--stat", f"{base}..HEAD", cwd=repo_dir)
            # ai/* branches belong to the agent; overwrite a leftover branch from a closed PR.
            self._git("push", "--force", "-u", "origin", branch, cwd=repo_dir)

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
                ),
                draft=self.s.open_draft_pr,
            )
            self.jira.add_comment(
                issue_key,
                f"🤖 Pull request opened: {pr_url}" + (f"\n\n*What changed:* {summary}" if summary else ""),
            )
            log.info("Opened %s for %s", pr_url, issue_key)
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

    def _run_aider(self, issue: JiraIssue, repo_dir: Path) -> str:
        history_dir = Path(self.s.workdir) / f"{issue.key}-aider"
        history_dir.mkdir(parents=True, exist_ok=True)
        tracked = self._git("ls-files", "-z", cwd=repo_dir).split("\0")
        files = mentioned_files(issue, [p for p in tracked if p], repo_dir)
        log.info("Files named in %s: %s", issue.key, files or "none (Aider will use its repo map)")
        cmd = [
            "aider",
            "--model", self.s.aider_model,
            "--edit-format", self.s.aider_edit_format,
            "--message", build_task_prompt(issue),
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
            *files,
        ]
        log.info("Running aider for %s with model %s", issue.key, self.s.aider_model)
        try:
            return self._redact(self._run(cmd, cwd=repo_dir, timeout=self.s.aider_timeout_seconds))
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"Aider timed out after {self.s.aider_timeout_seconds}s") from e
        finally:
            shutil.rmtree(history_dir, ignore_errors=True)
