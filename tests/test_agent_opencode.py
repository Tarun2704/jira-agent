"""The coding job end to end with CODING_BACKEND=opencode: a real git origin, a fake `opencode`, fake Jira/GitHub.

What must hold: only the app commits and pushes, secrets stay away from the agent, whatever the agent does to git is
undone, and cost records show one entry per model request.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import app.agent as agent_mod
from app.agent import AgentError, CodingAgent
from app.config import Settings
from app.jira_client import JiraIssue
from app.jobs import Job
from tests.fake_opencode import FAKE_SOURCE

TOKEN = "ghp_SECRET_TOKEN_123456"
TOKEN_URL = f"https://x-access-token:{TOKEN}@github.com/o/r.git"


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


class FakeJira:
    def __init__(self, *_):
        self.comments, self.moves = [], []
        self.description = "Add a hello file."

    def get_issue(self, key):
        return JiraIssue(key, "Add hello file", self.description, ["ai-agent"], "Task")

    def add_comment(self, key, text):
        self.comments.append(text)

    def transition_to(self, key, status):
        self.moves.append(status)
        return "moved"


class FakeGitHub:
    prs: list = []

    def __init__(self, *_):
        pass

    def default_branch(self):
        return "main"

    def find_open_pr_for_issue(self, key):
        return None

    def create_pr(self, **kw):
        FakeGitHub.prs.append(kw)
        return "https://github.com/o/r/pull/7"

    def pr_number(self, url):
        return 7


@pytest.fixture
def world(tmp_path, monkeypatch):
    """origin (bare) + a fake opencode executable + patched Jira/GitHub; returns a factory for agents."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=seed)
    (seed / "app.py").write_text("print('hi')\n")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
    sh("git", "clone", "-q", "--bare", str(seed), str(bare))

    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(FAKE_SOURCE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    orig_run = CodingAgent._run

    def run(self, cmd, cwd, timeout):
        if cmd[:2] == ["git", "clone"]:  # clone the local origin, but make it look like the real https URL with the token
            dest = Path(cmd[-1])
            out = orig_run(self, ["git", "clone", "-q", "--branch", "main", str(bare), str(dest)], cwd, timeout)
            orig_run(self, ["git", "config", f"url.{bare}.insteadOf", TOKEN_URL], dest, 60)
            orig_run(self, ["git", "remote", "set-url", "origin", TOKEN_URL], dest, 60)
            return out
        return orig_run(self, cmd, cwd, timeout)

    monkeypatch.setattr(CodingAgent, "_run", run)
    monkeypatch.setattr(agent_mod, "JiraClient", FakeJira)
    monkeypatch.setattr(agent_mod, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(agent_mod, "describe_changes", lambda *a, **k: "Adds a hello file.\n\nDetails.")
    FakeGitHub.prs = []
    monkeypatch.setattr(agent_mod.oc, "_KNOWN_MODELS", {})  # (the model list is cached per process)
    for marker in ("/tmp/HOOK_RAN", "/tmp/FSMONITOR_RAN"):
        Path(marker).unlink(missing_ok=True)

    def make(description="Add a hello file.", **overrides):
        values = dict(
            github_token=TOKEN, coding_backend="opencode", opencode_bin=str(fake), workdir=str(tmp_path / "work"),
            trigger_label="", rate_limit_wait_seconds=0, opencode_timeout_seconds=60, jira_status_in_progress="",
            jira_status_in_review="", jira_status_on_failure="", opencode_allow_bash=True,
        )
        settings = Settings(_env_file=None, **{**values, **overrides})
        monkeypatch.setenv("GEMINI_API_KEY", "gem-secret-123")
        a = CodingAgent(settings, Job(issue_key="CA-1", event="test"))
        a.jira.description = description
        return a

    make.bare, make.tmp = bare, tmp_path
    return make


def branch_commits(bare: Path, branch="ai/CA-1-add-hello-file"):
    out = sh("git", "log", "--format=%an|%s", f"main..{branch}", cwd=bare).strip().splitlines()
    return [line.split("|", 1) for line in out]


def test_opencode_edits_and_the_app_commits_pushes_and_opens_the_pr(world):
    a = world()
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    commits = branch_commits(world.bare)
    assert len(commits) == 1 and commits[0][0] == "jira-coding-agent" and commits[0][1].startswith("CA-1: Add hello file")
    assert sh("git", "show", "ai/CA-1-add-hello-file:hello.txt", cwd=world.bare) == "hello from the fake agent\n"
    assert FakeGitHub.prs and FakeGitHub.prs[0]["head"] == "ai/CA-1-add-hello-file"
    assert a.job.pr_url == "https://github.com/o/r/pull/7"
    assert "opencode" in a.job.steps and "aider" not in a.job.steps
    # temporary folders are gone
    assert not (world.tmp / "work" / "CA-1").exists() and not (world.tmp / "work" / "CA-1-opencode").exists()


def test_cost_records_have_one_entry_per_model_request_with_the_backend(world):
    a = world()
    a.handle_issue("CA-1")
    code = [c for c in a.job.calls if c["step"] == "code"]
    assert len(code) == 3 and all(c["backend"] == "opencode" and c["model"] == "google/gemini-2.5-flash" for c in code)
    assert [c["sent"] for c in code] == [1000, 1800, 1600] and [c["received"] for c in code] == [50, 100, 20]
    assert code[0]["cache_hit"] == 200 and code[1]["reported_cost"] == 0.003
    assert [c["step"] for c in a.job.calls].count("pr_description") == 1  # the PR text still comes from the shared path
    from app.costs import make_entry
    # the PR description is written through the shared (litellm) path under AIDER_MODEL's name, so both names need a price
    prices = {"google/gemini-2.5-flash": (1.0, 2.0), "gemini/gemini-2.5-flash": (1.0, 2.0)}
    entry = make_entry("j", "execute", 1, a.job.calls, prices)
    assert entry["requests"] == 4 and entry["cost_usd"] is not None
    run = a.job.runs[0]
    assert run["step"] == "code" and run["model"] == "opencode:google/gemini-2.5-flash" and "Created hello.txt as asked." in run["output"]


def test_the_agent_gets_no_app_secrets_and_no_token_in_git_config(world):
    a = world(description="Add a hello file. FAKE_DUMP")
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    seen = json.loads(sh("git", "show", "ai/CA-1-add-hello-file:dump.json", cwd=world.bare))
    assert seen["remote"] == "https://github.com/o/r.git"
    # (the test's own `insteadOf` helper line mentions the token; the remote URL itself must not)
    assert not any(TOKEN in ln for ln in seen["git_config"].splitlines() if ln.strip().startswith("url =") or "insteadOf" not in ln)
    assert "GITHUB_TOKEN" not in seen["env"] and "JIRA_API_TOKEN" not in seen["env"] and "GEMINI_API_KEY" in seen["env"]
    assert seen["config"]["permission"]["bash"]["git push*"] == "deny"


def test_whatever_the_agent_does_to_git_is_undone(world):
    a = world(description="Add a hello file. FAKE_SNEAKY")
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    commits = branch_commits(world.bare)
    assert len(commits) == 1 and commits[0][0] == "jira-coding-agent"  # the agent's own commit is folded into the app's
    assert sh("git", "show", "ai/CA-1-add-hello-file:sneaky.txt", cwd=world.bare) == "work\n"
    assert not Path("/tmp/HOOK_RAN").exists() and not Path("/tmp/FSMONITOR_RAN").exists()  # planted hook/config never ran
    assert "agent-branch" not in sh("git", "branch", cwd=world.bare)
    assert any("commit on its own" in w for w in a.job.warnings) and any("switched git branches" in w for w in a.job.warnings)


def test_no_changes_posts_what_the_agent_said(world):
    a = world(description="Look at it. FAKE_NOTHING")
    a.handle_issue("CA-1")
    assert a.job.status == "no_changes" and "nothing needs to change" in a.job.detail
    assert any("nothing needs to change" in c for c in a.jira.comments) and FakeGitHub.prs == []


def test_rate_limit_with_no_work_fails_with_a_clear_reason(world):
    a = world(description="Do it. FAKE_RATELIMIT")
    a.handle_issue("CA-1")
    assert a.job.status == "failed" and "rate or usage limit" in a.job.detail and FakeGitHub.prs == []


def test_error_after_partial_work_still_delivers_but_warns(world):
    a = world(description="Do it. FAKE_PARTIAL")
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded"
    assert any("may be incomplete" in w for w in a.job.warnings)
    assert sh("git", "show", "ai/CA-1-add-hello-file:partial.txt", cwd=world.bare) == "half done\n"


def test_fallback_model_takes_over_when_the_main_model_is_rate_limited(world):
    a = world(description="Do it. FAKE_PRIMARY_DOWN", opencode_model="google/primary", opencode_fallback_model="google/fallback")
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    steps = [(c["step"], c["model"]) for c in a.job.calls]
    assert ("code", "google/primary") in steps and ("code_fallback", "google/fallback") in steps
    assert "opencode_fallback" in a.job.steps and any("backup service" in w for w in a.job.warnings)


def test_daily_quota_is_not_retried_on_the_same_model(world, monkeypatch):
    """CA-11: a daily quota resets in hours, so waiting a minute and asking the same model again only wastes time."""
    waits = []
    monkeypatch.setattr(agent_mod.time, "sleep", waits.append)
    a = world(description="Do it. FAKE_DAILY", rate_limit_wait_seconds=45)  # (a plain rate limit would wait 45s)
    a.handle_issue("CA-1")
    assert a.job.status == "failed" and "daily free quota for gemini-2.5-flash" in a.job.detail and "19 hour" in a.job.detail
    assert waits == [] and "opencode_retry" not in a.job.steps and FakeGitHub.prs == []


def test_daily_quota_goes_straight_to_the_fallback_model(world, monkeypatch):
    waits = []
    monkeypatch.setattr(agent_mod.time, "sleep", waits.append)
    a = world(description="Do it. FAKE_DAILY", opencode_fallback_model="google/fallback")
    a.handle_issue("CA-1")
    # the fake reports the quota for every model, so the fallback fails too: what matters is that it was tried, at once
    assert waits == [] and "opencode_fallback" in a.job.steps
    assert [c["step"] for c in a.job.calls if c["step"].startswith("code")] == ["code", "code_fallback"]


def test_health_flags_opencode_model_names_opencode_cannot_use():
    s = Settings(_env_file=None, coding_backend="opencode", opencode_bin="sh",
                 opencode_model="google/gemini-2.5-flash", opencode_fallback_model="gemini/gemini-3.5-flash-lite")
    problems = s.config_problems()
    assert any("OPENCODE_FALLBACK_MODEL" in p and "google/gemini-3.5-flash-lite" in p for p in problems)
    assert not any(p.startswith("OPENCODE_MODEL") for p in problems)


def test_step_cap_is_reported_as_a_warning(world):
    a = world(description="Do it. FAKE_MANY", opencode_max_steps=3)
    a.handle_issue("CA-1")
    # the agent was cut off before it edited anything: a clear failure that names the setting, not "empty reply"
    assert a.job.status == "failed" and "OPENCODE_MAX_STEPS" in a.job.detail and "empty reply" not in a.job.detail
    assert any("OPENCODE_MAX_STEPS" in w for w in a.job.warnings) and FakeGitHub.prs == []


def test_missing_opencode_binary_fails_with_instructions(world):
    a = world(opencode_bin="/nonexistent/opencode")
    a.handle_issue("CA-1")
    assert a.job.status == "failed" and "CODING_BACKEND=aider" in a.job.detail and a.job.error_code == "opencode_missing"


def test_aider_backend_is_unchanged(world, monkeypatch):
    """Same job with CODING_BACKEND=aider: _run_aider is used, tokens come from its 'Tokens:' line, no backend field."""
    a = world(coding_backend="aider")

    def fake_aider(self, issue, repo_dir, message=None, files=None, **kw):
        (repo_dir / "hello.txt").write_text("from aider\n")
        return "Aider v0.86\nTokens: 1.2k sent, 300 received.\n"

    monkeypatch.setattr(CodingAgent, "_run_aider", fake_aider)
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    code = [c for c in a.job.calls if c["step"] == "code"]
    assert code == [{"step": "code", "model": "gemini/gemini-2.5-flash", "sent": 1200, "received": 300,
                     "cache_write": 0, "cache_hit": 0, "reported_cost": None}]
    assert "aider" in a.job.steps and "opencode" not in a.job.steps


def test_shell_access_is_off_unless_enabled(world):
    assert Settings(_env_file=None).opencode_allow_bash is False
    a = world(description="Add a hello file. FAKE_DUMP", opencode_allow_bash=False)
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    seen = json.loads(sh("git", "show", "ai/CA-1-add-hello-file:dump.json", cwd=world.bare))
    assert seen["config"]["permission"]["bash"] == "deny"


def test_opencode_is_the_default_planner_and_health_checks_it(monkeypatch):
    monkeypatch.delenv("PLANNER_BACKEND", raising=False)
    s = Settings(_env_file=None, coding_backend="aider", opencode_bin="/nonexistent/opencode",
                 opencode_planner_model="gemini/gemini-3.1-flash-lite")
    assert s.planner_backend_used == "opencode" and s.backend == "aider"
    problems = s.config_problems()
    assert any("PLANNER_BACKEND=opencode" in p and "not installed" in p for p in problems)
    assert any("OPENCODE_PLANNER_MODEL" in p and "google/gemini-3.1-flash-lite" in p for p in problems)
    assert Settings(_env_file=None, planner_backend="aider").planner_backend_used == "aider"


def test_an_unknown_model_is_named_and_the_fallback_takes_over(world):
    """2026-10-14 e2e: OpenCode reports a model it doesn't know only as "UnknownError: Unexpected server error", so the
    fallback was never tried and the owner got "something went wrong on my side"."""
    a = world(opencode_model="google/gemini-0-no-such-model", opencode_fallback_model="google/fallback")
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    models = [c["model"] for c in a.job.calls if c["step"].startswith("code")]
    assert models[0] == "google/gemini-0-no-such-model" and set(models[1:]) == {"google/fallback"}


def test_opencodes_opaque_error_before_any_request_may_use_the_fallback():
    from app import opencode_runner as oc
    from app.agent import llm_problem, retryable_llm_problem

    log_text = "[opencode] model=google/x | model requests=0 | tokens in=0 out=0 | 1.0s | exit=0\n[error] UnknownError: Unexpected server error."
    assert retryable_llm_problem(oc.opencode_problem(log_text, llm_problem))
    later = log_text.replace("model requests=0 ", "model requests=3 ")  # it worked for a while, then crashed: not a model problem
    assert not retryable_llm_problem(oc.opencode_problem(later, llm_problem))


def test_the_commit_carries_the_runs_delivery_stamp(world):
    a = world()
    a.delivery_id = "d3adb33f"
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    assert "Agent-Delivery: d3adb33f" in sh("git", "log", "-1", "--format=%B", "ai/CA-1-add-hello-file", cwd=world.bare)


def test_a_second_opencode_run_in_the_same_clone_adds_the_exclude_line_once(world, monkeypatch):
    a = world()
    seen = []
    real = agent_mod.oc.run_opencode

    def spy(**kw):
        seen.append((kw["cwd"] / ".git" / "info" / "exclude").read_text().count(".opencode/"))
        return real(**kw)

    monkeypatch.setattr(agent_mod.oc, "run_opencode", spy)
    original = CodingAgent._run_coder

    def twice(self, issue, repo_dir, *args, **kw):  # like the syntax-fix pass, which reuses the clone
        original(self, issue, repo_dir, *args, **kw)
        return original(self, issue, repo_dir, *args, **kw)

    monkeypatch.setattr(CodingAgent, "_run_coder", twice)
    a.handle_issue("CA-1")
    assert seen == [1, 1]
