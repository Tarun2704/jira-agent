"""Stuck jobs, the queue notice's source, Aider taking over from a failed OpenCode, and the approved plan's file scope."""
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app import agent as agent_mod
from app.agent import path_allowed, plan_paths
from app.jobs import Job, JobStuck, run_process
from app import opencode_runner as oc
from tests.test_agent_opencode import FakeGitHub, sh, world  # noqa: F401  (world is a fixture)

_which = shutil.which


def aider_installed(monkeypatch):
    """Pretend Aider is installed (the fallback only happens where it is)."""
    monkeypatch.setattr(agent_mod.shutil, "which", lambda name, *a, **k: "/usr/local/bin/aider" if name == "aider" else _which(name, *a, **k))


def fake_aider(monkeypatch, calls):
    def run_aider(self, issue, repo_dir, message=None, files=None, **k):
        calls.append({"message": message, **k})
        (repo_dir / "hello.txt").write_text("from aider\n")
        return "Applied edit to hello.txt\nTokens: 1k sent, 50 received."
    monkeypatch.setattr(agent_mod.CodingAgent, "_run_aider", run_aider)


# ---- run_process / Job ---------------------------------------------------------------------------------------------

def test_run_process_works_like_subprocess_run_and_each_line_is_progress(tmp_path):
    job = Job("CA-1", "x")
    job.last_progress -= 1000
    p = run_process(["sh", "-c", "echo hi; echo oops >&2; exit 3"], cwd=tmp_path, timeout=10, job=job)
    assert (p.returncode, p.stdout, p.stderr) == (3, "hi\n", "oops\n") and job.idle_seconds() < 5


def test_run_process_times_out(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_process(["sleep", "5"], cwd=tmp_path, timeout=0.3)


def test_a_stuck_job_has_its_process_and_children_killed(tmp_path):
    job = Job("CA-1", "x")
    threading.Timer(0.5, job.stop_stuck).start()
    with pytest.raises(JobStuck):
        run_process(["sh", "-c", "sleep 30 & echo $! > child.pid; wait"], cwd=tmp_path, timeout=60, job=job)
    child = int((tmp_path / "child.pid").read_text())
    import os
    import time
    with pytest.raises(ProcessLookupError):  # killed with its parent's process group
        for _ in range(50):
            os.kill(child, 0)
            time.sleep(0.05)


def test_the_watchdog_stops_only_jobs_without_a_sign_of_life(monkeypatch):
    from app import main
    idle, busy = Job("CA-1", "x"), Job("CA-2", "x")
    idle.last_progress -= 10_000
    stopped = []
    monkeypatch.setattr(idle, "stop_stuck", lambda: stopped.append("CA-1") or 0)
    monkeypatch.setattr(busy, "stop_stuck", lambda: stopped.append("CA-2") or 0)
    monkeypatch.setattr(main, "_running", {"CA-1": idle, "CA-2": busy})
    assert main._stop_stuck_jobs() == ["CA-1"] and stopped == ["CA-1"]


def test_what_a_queued_job_waits_for(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_order", [("CA-1", "execute", "coding"), ("CA-2", "plan", "coding"), ("CA-3", "plan", "plan"),
                                         ("CA-4", "plan", "plan")])
    monkeypatch.setattr(main, "_running", {"CA-1": Job("CA-1", "x"), "CA-3": Job("CA-3", "x")})
    assert main._ahead("CA-2") == ["CA-1's coding run"]  # one job at a time on the small server
    assert main._ahead("CA-4") == ["CA-3's plan"]  # PLAN_WORKERS=1
    assert main._ahead("CA-1") == [] and main._ahead("CA-9") == []  # running, or unknown


def test_opencode_stopped_as_stuck_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_KNOWN_MODELS", {"google": {"google/m"}})
    fake = tmp_path / "opencode"
    from tests.fake_opencode import FAKE_SOURCE
    fake.write_text(FAKE_SOURCE)
    fake.chmod(0o755)
    (tmp_path / "repo").mkdir()
    sh("git", "init", "-q", cwd=tmp_path / "repo")
    job = Job("CA-1", "x")
    threading.Timer(1.0, job.stop_stuck).start()
    run = oc.run_opencode(binary=str(fake), cwd=tmp_path / "repo", prompt="FAKE_HANG", model="google/m",
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, config={}, timeout=60, job=job)
    log = oc.render_log(run)
    assert run.stopped == "stuck" and "[stopped] no progress" in log and oc.tool_problem(log) == "it made no progress"


# ---- Aider takes over when OpenCode itself fails --------------------------------------------------------------------

def test_aider_takes_over_when_opencode_crashes(world, monkeypatch):
    aider_installed(monkeypatch)
    calls = []
    fake_aider(monkeypatch, calls)
    a = world(description="Do it. FAKE_BADCONFIG")  # OpenCode exits with an error before doing anything
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    assert len(calls) == 1 and "aider_instead" in a.job.steps and a.used_backend == "aider"
    assert any("OpenCode failed" in w and "Aider did the work" in w for w in a.job.warnings)
    assert any(c["step"] == "code_aider" for c in a.job.calls)


def test_aider_takes_over_when_opencode_is_stuck(world, monkeypatch):
    aider_installed(monkeypatch)
    calls = []
    fake_aider(monkeypatch, calls)
    a = world(description="Do it. FAKE_HANG")
    threading.Timer(1.5, a.job.stop_stuck).start()
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    assert len(calls) == 1 and any("no progress" in w for w in a.job.warnings)


def test_no_fallback_for_model_problems(world, monkeypatch):
    aider_installed(monkeypatch)
    calls = []
    fake_aider(monkeypatch, calls)
    a = world(description="Do it. FAKE_RATELIMIT")  # the model's problem: the backup model's job, not Aider's
    a.handle_issue("CA-1")
    assert a.job.status == "failed" and calls == []


def test_no_fallback_when_aider_is_not_installed(world, monkeypatch):
    monkeypatch.setattr(agent_mod.shutil, "which", lambda name, *a, **k: None if name == "aider" else _which(name, *a, **k))
    a = world(opencode_bin="/nonexistent/opencode")
    a.handle_issue("CA-1")
    assert a.job.status == "failed" and a.job.error_code == "opencode_missing"  # the real problem, not "aider missing"


def test_the_opencode_planner_hands_over_to_the_aider_planner(monkeypatch, tmp_path):
    from app import planner
    from app.store import Session
    from tests.test_planner import ISSUE, _opencode_planner

    stopped = "[opencode] model=google/m | model requests=20\n[tool] grep x\n[stopped] step limit reached (20 model requests)"
    p, seen = _opencode_planner(monkeypatch, tmp_path, stopped)
    monkeypatch.setattr(planner, "can_fall_back", lambda s: True)
    p.agent._run_aider = lambda *a, **k: "PLAN_START\n## Plan\n1. fix `a.py`\n\n## Files\n- a.py\nPLAN_END"
    result = p.run(ISSUE, Session("CA-1"))
    assert "fix" in result.body and any("Aider wrote this plan" in w for w in result.warnings)


# ---- only the approved plan's files -----------------------------------------------------------------------------------

def test_plan_paths_include_new_files_folders_and_the_files_section():
    plan = ("## Plan\n1. Add `top_years.py` reading `crime/data.csv`.\n2. Add tests in `tests/`.\n3. Run `python top_years.py 5`.\n\n"
            "## Files\n- requirements.txt: add pandas\n- src/util.py (new)\n")
    assert plan_paths(plan) >= {"top_years.py", "crime/data.csv", "tests/", "requirements.txt", "src/util.py"}
    allowed = plan_paths(plan)
    assert path_allowed("tests/test_top.py", allowed) and path_allowed("top_years.py", allowed)
    assert not path_allowed("README.md", allowed) and not path_allowed("app.py", allowed)


def test_changes_outside_the_approved_plan_are_undone(world):
    a = world(description="Do it. FAKE_TWO")
    a.plan = "## Plan\n1. Create `hello.txt` with a greeting.\n\n## Files\n- hello.txt (new)"
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded", a.job.detail
    branch = next(b for b in sh("git", "branch", "--format=%(refname:short)", cwd=world.bare).split() if b.startswith("ai/"))
    assert sh("git", "show", f"{branch}:hello.txt", cwd=world.bare) == "hello from the fake agent\n"
    assert sh("git", "show", f"{branch}:app.py", cwd=world.bare) == "print('hi')\n"  # undone
    assert any("doesn't include" in w and "`app.py`" in w for w in a.job.warnings)


def test_nothing_left_after_undoing_is_reported_as_no_changes(world):
    a = world()  # writes hello.txt
    a.plan = "## Plan\n1. Change `app.py` to say hello."
    a.handle_issue("CA-1")
    assert a.job.status == "no_changes" and "hello.txt" in a.job.detail and FakeGitHub.prs == []


def test_without_a_plan_nothing_is_checked(world):
    a = world(description="Do it. FAKE_TWO")  # the direct flow (no plan-first): as before
    a.handle_issue("CA-1")
    assert a.job.status == "succeeded" and not any("doesn't include" in w for w in a.job.warnings)


def test_a_stuck_opencode_planner_is_reported_as_stuck_without_fallback(monkeypatch, tmp_path):
    """e2e 2026-10-19 (CA-25): with TOOL_FALLBACK off, a stuck planner was reported as "the AI didn't give an answer"."""
    from app.agent import AgentError
    from app.store import Session
    from tests.test_planner import ISSUE, _opencode_planner

    p, _ = _opencode_planner(monkeypatch, tmp_path, "", tool_fallback=False)

    def stuck(*a, **k):
        raise AgentError("Stopped OpenCode (plan): the job made no progress for 1 minute(s) (JOB_STUCK_SECONDS).", code="stuck")

    p.agent.run_opencode_readonly = stuck
    with pytest.raises(AgentError) as e:
        p.run(ISSUE, Session("CA-1"))
    assert e.value.code == "stuck"


def test_run_opencode_readonly_raises_stuck(world, monkeypatch):
    a = world()
    run = oc.OpenCodeRun(model="google/m", stopped="stuck")
    monkeypatch.setattr(agent_mod.oc, "run_opencode", lambda **k: run)
    with pytest.raises(agent_mod.AgentError) as e:
        a.run_opencode_readonly(agent_mod.JiraIssue("CA-1", "t", "d", [], "Task"), Path(world.tmp), "plan it",
                                model="google/m", step="plan", timeout=60, max_steps=5)
    assert e.value.code == "stuck"


def test_the_watchdog_names_the_step_that_is_running():
    job = Job("CA-1", "x")
    with job.step("clone"):
        pass
    with job.step("opencode_plan"):
        assert job.current_step == "opencode_plan"
    assert job.current_step == "start"


def test_pr_descriptions_use_the_cheaper_model_when_set(world, monkeypatch):
    seen = []
    monkeypatch.setattr(agent_mod, "describe_changes", lambda model, *a, fallback="", **k: seen.append((model, fallback)) or "Adds a file.")
    world(pr_description_model="gemini/gemini-3.1-flash-lite", aider_model="gemini/gemini-2.5-flash",
          aider_fallback_model="gemini/gemini-3.1-flash-lite").handle_issue("CA-1")
    world(aider_model="gemini/gemini-2.5-flash", aider_fallback_model="gemini/gemini-3.5-flash-lite").handle_issue("CA-1")
    # the cheap model writes it; its backup is never the same model (then AIDER_MODEL) · not set: as before
    assert seen == [("gemini/gemini-3.1-flash-lite", "gemini/gemini-2.5-flash"), ("gemini/gemini-2.5-flash", "gemini/gemini-3.5-flash-lite")]
