from app.agent import aider_reply



def test_aider_reply_drops_startup_and_scrape_noise():
    log = ("Analytics have been permanently disabled.\nAider v0.86.2\nModel: x\nAdded a.py to the chat.\n"
           "Scraping https://github.com/o/r/pull/32)...\nUnable to install pandoc: [Errno 2]\n"
           "I need you to add the .deb file to the chat.\nTokens: 23k sent")
    assert aider_reply(log) == "I need you to add the .deb file to the chat."


def test_chat_reply_without_reply_start_is_still_understood():
    from app.planner import parse_chat_output, repo_listing
    out = ("Repo-map: using 1024 tokens, auto refresh\nAdded a.py to the chat.\n\nrequirements.txt\n"
           "That file is not in the list I was given.\n\nREPLY_END\nPLAN_CHANGED: no\n\nTokens: 6k")
    r = parse_chat_output(out)
    assert r and not r.changed and r.reply == "That file is not in the list I was given."
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "a.py").write_text("x = 1")
    (d / "x.deb").write_bytes(b"0" * 300_000)
    listing = repo_listing(["a.py", "x.deb"], d)
    assert "x.deb" in listing and "a.py" not in listing  # small files are never named: Aider would load them all
    assert repo_listing(["a.py"], d) == ""


def test_edit_failures_are_not_reported_as_rate_limits():
    from app.agent import llm_problem
    out = ("The SEARCH block failed to match any lines in the file.\n"
           "Only 3 reflections allowed, stopping at the limit.\n"
           "https://aider.chat/docs/troubleshooting/edit-errors.html\n")
    p = llm_problem(out)
    assert "couldn't apply the model's edit" in p and "rate" not in p
    assert llm_problem("litellm.RateLimitError: TPM limit") and "rate or usage limit" in llm_problem("litellm.RateLimitError: TPM limit")
    assert llm_problem("see https://aider.chat/docs/x-errors.html for help") is None


def test_gemini_quota_error_is_a_rate_limit_even_with_help_links_around_it():
    from app.agent import is_rate_limit, llm_problem, retryable_llm_problem
    out = ('litellm.RateLimitError: ... "@type": "type.googleapis.com/google.rpc.QuotaFailure" ...\n'
           "The API provider has rate limited you. Try again later or check your quotas.\n"
           "https://aider.chat/docs/troubleshooting/edit-errors.html\nhttps://ai.google.dev/gemini-api/docs/rate-limits\n")
    p = llm_problem(out)
    assert is_rate_limit(p) and retryable_llm_problem(p) and "couldn't apply" not in p


def test_too_much_context_is_not_called_a_rate_limit():
    from app.agent import llm_problem
    out = ("litellm.BadRequestError: Your estimated chat context of 1,720,926 tokens exceeds the 1,048,576 token\n"
           "limit for gemini-2.5-flash.\nhttps://ai.google.dev/gemini-api/docs/rate-limits\n")
    assert "too large for the model" in llm_problem(out)



def test_follow_up_prompt_tells_the_model_not_to_redo_earlier_rounds():
    from app.agent import build_task_prompt
    from app.jira_client import JiraIssue
    issue = JiraIssue("CA-1", "t", "d", [], "Task")
    assert "FOLLOW-UP round" in build_task_prompt(issue, "plan", "history")
    assert "FOLLOW-UP" not in build_task_prompt(issue, "plan", "")


# ---- the plan's "## Delete" section ------------------------------------------------------------------

import subprocess  # noqa: E402

from app.planner import (  # noqa: E402
    MAX_DELETIONS, PlanResult, check_deletions, parse_delete_section, parse_planner_output, with_delete_section,
)

PLAN_WITH_DELETE = """PLAN_START
## Questions
None
## Plan
1. Delete the listed file.
2. Add a note to the README.
## Delete
- `pandoc-3.12.1-1-arm64.deb`
## Files
- `README.md`: add a note
## Risks
None
PLAN_END"""


def test_delete_section_is_parsed_from_the_plan():
    r = parse_planner_output(PLAN_WITH_DELETE)
    assert r.delete_files == ["pandoc-3.12.1-1-arm64.deb"]
    assert "## Delete" not in r.body  # the checked section is added back by the planner, not copied from the model
    assert parse_planner_output(PLAN_WITH_DELETE.replace("- `pandoc-3.12.1-1-arm64.deb`", "None")).delete_files == []
    assert parse_planner_output(PLAN_WITH_DELETE.replace("## Delete\n- `pandoc-3.12.1-1-arm64.deb`\n", "")).delete_files == []  # old-style plans


def test_parse_delete_section_accepts_bullets_backticks_and_bare_paths():
    assert parse_delete_section("- `a.txt`\n* `dir/b.bin`\n3. c.txt\nNone") == ["a.txt", "dir/b.bin", "c.txt"]
    assert parse_delete_section("None") == [] and parse_delete_section("") == []


def test_only_tracked_safe_paths_survive_the_check():
    tracked = {"a.txt", "dir/b.bin", ".git/config"}
    valid, dropped = check_deletions(["a.txt", "nope.txt", ".git/config", "../etc/passwd", "dir/b.bin"], tracked)
    assert valid == ["a.txt", "dir/b.bin"] and dropped == ["nope.txt", ".git/config", "../etc/passwd"]
    many = {f"f{i}.txt" for i in range(30)}
    valid, dropped = check_deletions(sorted(many), many)
    assert len(valid) == MAX_DELETIONS and len(dropped) == 30 - MAX_DELETIONS


def test_the_plan_text_shows_exactly_the_checked_deletions():
    body = "## Plan\n1. x\n\n## Files\n- `a.py`"
    assert with_delete_section(body, ["old.bin"]) == "## Plan\n1. x\n\n## Delete\n- `old.bin`\n\n## Files\n- `a.py`"
    assert with_delete_section("## Plan\n1. x\n\n## Delete\n- `gone`\n\n## Files\n- `a.py`", []) == body


def _agent_in(tmp_path):
    from app.agent import CodingAgent
    from app.jobs import Job
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("a.txt", "keep.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "big.bin").write_bytes(b"0" * 1000)
    subprocess.run("git add -A && git -c user.name=t -c user.email=t@t commit -qm init", shell=True, cwd=tmp_path, check=True)
    a = CodingAgent.__new__(CodingAgent)  # no network clients needed for this
    a.job = Job("CA-1", "x")
    a.deletions, a.deleted = ["big.bin", "missing.txt", "../outside"], []
    a._git = lambda *args, cwd, timeout=300: subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    return a


def test_the_agent_removes_only_real_tracked_files_and_reports_the_rest(tmp_path):
    a = _agent_in(tmp_path)
    a._apply_deletions(tmp_path)
    assert a.deleted == ["big.bin"] and not (tmp_path / "big.bin").exists() and (tmp_path / "keep.txt").exists()
    assert any("missing.txt" in w for w in a.job.warnings) and any("../outside" in w for w in a.job.warnings)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert status.strip() == "D  big.bin"


def test_the_coder_is_told_which_files_are_already_deleted():
    from app.agent import build_task_prompt
    from app.jira_client import JiraIssue
    issue = JiraIssue("CA-1", "t", "d", [], "Task")
    p = build_task_prompt(issue, "plan", "", ["x.deb"])
    assert "ALREADY DELETED" in p and "`x.deb`" in p and "rest of the plan" in p
    assert "ALREADY DELETED" not in build_task_prompt(issue, "plan", "")
