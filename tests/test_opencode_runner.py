"""OpenCode runner: config, scrubbed environment, event parsing, process handling (against a fake executable)."""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from app import opencode_runner as oc
from tests.fake_opencode import FAKE_SOURCE


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "bin" / "opencode"
    path.parent.mkdir()
    path.write_text(FAKE_SOURCE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "app.py").write_text("print('hi')\n")
    return d


def _run(fake_bin, repo, tmp_path, prompt="do it", **kw):
    env = oc.build_env(models=["google/gemini-2.5-flash"], home=tmp_path / "home", cache=tmp_path / "cache",
                       source={"PATH": os.environ["PATH"], "GEMINI_API_KEY": "gem-secret-123", "GITHUB_TOKEN": "gh-secret"})
    args = dict(binary=str(fake_bin), cwd=repo, prompt=prompt, model="google/gemini-2.5-flash", env=env,
                config=oc.build_config(allow_bash=True), timeout=30)
    args.update(kw)
    return oc.run_opencode(**args)


# --- config ---------------------------------------------------------------------------------------------------------

def test_config_denies_git_writes_web_and_subagents():
    perm = oc.build_config(allow_bash=True)["permission"]
    assert perm["edit"] == "allow" and perm["webfetch"] == "deny" and perm["task"] == "deny" and perm["question"] == "deny"
    assert perm["bash"]["*"] == "allow"
    for blocked in ("git commit*", "git push*", "git checkout*", "git config*", "git remote*"):
        assert perm["bash"][blocked] == "deny"


def test_config_without_bash_denies_all_shell_commands():
    assert oc.build_config(allow_bash=False)["permission"]["bash"] == "deny"


def test_extra_config_is_merged_and_bad_json_is_ignored():
    extra = json.dumps({"provider": {"x": {"name": "X"}}, "permission": {"webfetch": "ask"}})
    cfg = oc.build_config(allow_bash=True, extra_json=extra)
    assert cfg["provider"]["x"]["name"] == "X" and cfg["permission"]["webfetch"] == "ask" and cfg["permission"]["edit"] == "allow"
    assert oc.build_config(allow_bash=True, extra_json="{not json")["permission"]["webfetch"] == "deny"
    assert oc.build_config(allow_bash=True, extra_json="[1]")["permission"]["webfetch"] == "deny"


# --- environment ----------------------------------------------------------------------------------------------------

def test_env_contains_only_the_chosen_providers_key_and_no_app_secrets(tmp_path):
    source = {"PATH": "/bin", "GEMINI_API_KEY": "gem-secret-123", "OPENAI_API_KEY": "openai-secret-1", "GITHUB_TOKEN": "gh",
              "JIRA_API_TOKEN": "jira", "SLACK_BOT_TOKEN": "slack", "REDIS_URL": "rediss://x", "WEBHOOK_SECRET": "w", "LANG": "C.UTF-8"}
    env = oc.build_env(models=["google/gemini-2.5-flash"], home=tmp_path / "h", cache=tmp_path / "c", source=source)
    assert env["GEMINI_API_KEY"] == "gem-secret-123" and env["PATH"] == "/bin" and env["LANG"] == "C.UTF-8"
    for leaked in ("OPENAI_API_KEY", "GITHUB_TOKEN", "JIRA_API_TOKEN", "SLACK_BOT_TOKEN", "REDIS_URL", "WEBHOOK_SECRET"):
        assert leaked not in env
    assert env["HOME"] == str(tmp_path / "h") and env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert oc.secret_values(env) == ["gem-secret-123"]


def test_google_key_is_also_passed_under_the_name_googles_sdk_reads(tmp_path):
    # Found on a real run: with only GEMINI_API_KEY set, OpenCode fails with "Google Generative AI API key is missing".
    for have in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env = oc.build_env(models=["google/gemini-2.5-flash"], home=tmp_path, cache=tmp_path, source={"PATH": "/bin", have: "gem-secret-123"})
        assert env["GOOGLE_GENERATIVE_AI_API_KEY"] == "gem-secret-123" and env[have] == "gem-secret-123"
    own = oc.build_env(models=["google/x"], home=tmp_path, cache=tmp_path,
                       source={"PATH": "/bin", "GEMINI_API_KEY": "gem-secret-123", "GOOGLE_GENERATIVE_AI_API_KEY": "explicit-key-99"})
    assert own["GOOGLE_GENERATIVE_AI_API_KEY"] == "explicit-key-99"  # an explicitly set value is never overridden
    other = oc.build_env(models=["openai/gpt-x"], home=tmp_path, cache=tmp_path, source={"PATH": "/bin", "GEMINI_API_KEY": "gem-secret-123"})
    assert "GOOGLE_GENERATIVE_AI_API_KEY" not in other and "GEMINI_API_KEY" not in other  # no alias, no leak, for other providers
    assert set(oc.secret_values(env)) == {"gem-secret-123"}  # both names are redacted from logs


def test_env_unknown_provider_uses_its_conventional_key_name_and_pass_env(tmp_path):
    source = {"PATH": "/bin", "ACME_API_KEY": "acme-secret-9", "MY_PROXY_KEY": "mine-secret-9", "OTHER": "x"}
    env = oc.build_env(models=["acme/m1"], home=tmp_path, cache=tmp_path, pass_env="MY_PROXY_KEY, NOPE", source=source)
    assert env["ACME_API_KEY"] == "acme-secret-9" and env["MY_PROXY_KEY"] == "mine-secret-9" and "OTHER" not in env


# --- events ---------------------------------------------------------------------------------------------------------

def test_events_are_folded_into_tokens_tools_and_text():
    run = oc.OpenCodeRun(model="m")
    oc.handle_event(run, {"type": "step_start"})
    oc.handle_event(run, {"type": "tool_use", "part": {"tool": "edit", "state": {"status": "completed", "input": {"filePath": "a.py"}}}})
    oc.handle_event(run, {"type": "step_finish", "part": {"tokens": {"input": 1000, "output": 50, "reasoning": 30, "cache": {"read": 200, "write": 100}}, "cost": 0.5}})
    oc.handle_event(run, {"type": "text", "part": {"text": "All done."}})
    oc.handle_event(run, {"type": "mystery", "part": {}})
    assert run.steps == [{"sent": 1100, "received": 80, "cache_hit": 200, "cache_write": 100, "cost": 0.5}]
    assert run.tools == ["edit a.py"] and run.final_text == "All done." and (run.sent, run.received) == (1100, 80)


def test_error_event_becomes_a_readable_line():
    run = oc.OpenCodeRun(model="m")
    oc.handle_event(run, {"type": "error", "error": {"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}}})
    assert run.errors == ["APIError: Rate limit exceeded (HTTP 429)"]


def test_log_markers_round_trip_and_problem_reuses_the_shared_wording():
    from app.agent import llm_problem
    run = oc.OpenCodeRun(model="google/x", texts=["I changed nothing because the rate limit logic is fine."], errors=[])
    log = oc.render_log(run)
    assert oc.opencode_reply(log).startswith("I changed nothing")
    # words like "rate limit" in the agent's own explanation are NOT an LLM problem
    assert oc.opencode_problem(log, llm_problem) is None
    run.errors.append("APIError: Rate limit exceeded: tokens per minute (HTTP 429)")
    assert "rate or usage limit" in oc.opencode_problem(oc.render_log(run), llm_problem)
    run.errors = ["WeirdError: something odd"]
    assert oc.opencode_problem(oc.render_log(run), llm_problem).startswith("OpenCode reported an error: WeirdError")


def test_log_is_redacted_and_caps_the_tool_list():
    run = oc.OpenCodeRun(model="m", tools=[f"read f{i}" for i in range(100)], texts=["key is sk-secret"])
    log = oc.render_log(run, lambda t: t.replace("sk-secret", "***"))
    assert "sk-secret" not in log and "and 20 more" in log


def test_prompt_tells_the_agent_not_to_touch_git_and_lists_files():
    p = oc.build_prompt("Fix it.", ["a.py", "b/c.py"])
    assert p.startswith("Fix it.") and "`a.py`, `b/c.py`" in p and "Do NOT run git commands that change anything" in p
    assert "Files most likely" not in oc.build_prompt("Fix it.", [])
    assert "cannot run shell commands" in p and "existing tests" not in p  # shell access is off by default
    assert "existing tests" in oc.build_prompt("Fix it.", [], allow_bash=True)


# --- the process ----------------------------------------------------------------------------------------------------

def test_a_normal_run_reports_every_model_request(fake_bin, repo, tmp_path):
    run = _run(fake_bin, repo, tmp_path)
    assert run.exit_code == 0 and not run.errors and not run.stopped
    assert len(run.steps) == 3
    assert run.steps[0] == {"sent": 1000, "received": 50, "cache_hit": 200, "cache_write": 0, "cost": 0.002}
    assert run.steps[1]["sent"] == 1800 and run.steps[1]["received"] == 100  # 1500 + 300 cache write; 60 + 40 reasoning
    assert run.tools == ["write hello.txt", "bash python -m pytest -q"]
    assert run.final_text == "Created hello.txt as asked." and (repo / "hello.txt").exists()
    assert round(run.reported_cost, 4) == 0.006


def test_stdin_is_closed_so_the_agent_never_waits_for_input(fake_bin, repo, tmp_path):
    run = _run(fake_bin, repo, tmp_path, prompt="FAKE_STDIN", timeout=20)
    assert run.stopped == "" and (repo / "stdin_len.txt").read_text() == "0"


def test_the_agent_sees_a_scrubbed_environment_and_the_config_inline(fake_bin, repo, tmp_path):
    _run(fake_bin, repo, tmp_path, prompt="FAKE_DUMP")
    seen = json.loads((repo / "dump.json").read_text())
    assert "GEMINI_API_KEY" in seen["env"] and "GITHUB_TOKEN" not in seen["env"]
    assert seen["args"][:5] == ["run", "--model", "google/gemini-2.5-flash", "--format", "json"]
    assert seen["config"]["permission"]["bash"]["git push*"] == "deny" and seen["config"]["share"] == "disabled"
    assert "OPENCODE_CONFIG_CONTENT" in seen["env"]


def test_rate_limit_error_event_is_captured(fake_bin, repo, tmp_path):
    run = _run(fake_bin, repo, tmp_path, prompt="FAKE_RATELIMIT")
    assert run.exit_code == 1 and run.errors == ["APIError: Rate limit exceeded: tokens per minute (HTTP 429)"]


def test_a_crash_without_events_surfaces_stderr(fake_bin, repo, tmp_path):
    run = _run(fake_bin, repo, tmp_path, prompt="FAKE_BADCONFIG")
    assert run.errors and "Configuration is invalid" in run.errors[0] and "code 1" in run.errors[0]


def test_timeout_kills_the_agent_and_everything_it_started(fake_bin, repo, tmp_path):
    t = time.monotonic()
    run = _run(fake_bin, repo, tmp_path, prompt="FAKE_HANG", timeout=2)
    assert run.stopped == "timeout" and time.monotonic() - t < 15
    pid = int((repo / "child.pid").read_text())
    time.sleep(0.3)
    try:  # the `sleep 300` the agent spawned is gone too (a killed, not yet reaped process shows up as state Z)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        state = "gone"
    assert state in ("gone", "Z")


def test_step_cap_stops_a_runaway_agent(fake_bin, repo, tmp_path):
    run = _run(fake_bin, repo, tmp_path, prompt="FAKE_MANY", max_steps=3)
    assert run.stopped == "max_steps" and len(run.steps) == 3 and run.texts == []


def test_missing_executable_is_reported_clearly(repo, tmp_path):
    with pytest.raises(oc.OpenCodeNotInstalled):
        _run(Path("/nonexistent/opencode"), repo, tmp_path)


# --- memory ---------------------------------------------------------------------------------------------------------

def test_container_memory_limit_is_read_from_cgroup_files(tmp_path):
    v2 = tmp_path / "memory.max"
    v2.write_text("536870912\n")
    assert oc.container_memory_mb((str(v2),)) == 512
    v2.write_text("max\n")
    assert oc.container_memory_mb((str(v2),)) is None  # no limit
    assert oc.container_memory_mb((str(tmp_path / "missing"),)) is None


def test_health_warns_when_the_instance_is_too_small_for_opencode(monkeypatch):
    from app import config

    monkeypatch.setenv("CODING_BACKEND", "opencode")
    monkeypatch.setattr(oc, "container_memory_mb", lambda *a, **k: 512)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/opencode")
    config.get_settings.cache_clear()
    problems = config.get_settings().config_problems()
    assert any("512 MB" in p and "out of memory" in p for p in problems)
    monkeypatch.setattr(oc, "container_memory_mb", lambda *a, **k: 2048)
    assert not any("MB of memory" in p for p in config.get_settings().config_problems())
    monkeypatch.setenv("CODING_BACKEND", "aider")
    config.get_settings.cache_clear()
    monkeypatch.setattr(oc, "container_memory_mb", lambda *a, **k: 512)
    assert not any("MB of memory" in p for p in config.get_settings().config_problems())  # Aider setups are not nagged


def test_read_only_config_cannot_be_reopened_by_extra_config():
    cfg = oc.build_config(allow_bash=True, read_only=True, extra_json='{"permission": {"edit": "allow", "bash": "allow"}}')
    assert cfg["permission"]["edit"] == "deny" and cfg["permission"]["bash"] == "deny"
    assert oc.build_config(allow_bash=False)["permission"]["edit"] == "allow"  # coding runs still edit
