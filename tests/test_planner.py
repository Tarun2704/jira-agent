from app.jira_client import JiraIssue
from app.planner import approved_plan_text, build_planner_prompt, parse_planner_output
from app.store import Session

ISSUE = JiraIssue("CA-1", "Fix login", "Button crashes on Safari", ["ai-agent"], "Bug")

GOOD = """
PLAN_START
## Questions
- Should the fix also cover the mobile page?
## Plan
1. Guard the null session in `auth.py`.
2. Add a regression test.
## Files
- `auth.py`: null check
## Risks
None
PLAN_END
Tokens: 3.1k sent, 400 received.
"""


def test_parse_full_plan():
    r = parse_planner_output(GOOD)
    assert r.questions == "- Should the fix also cover the mobile page?"
    assert "## Plan\n1. Guard the null session" in r.body
    assert "## Files" in r.body and "## Risks" not in r.body  # "None" sections are dropped
    assert r.body.startswith("## Questions")


def test_parse_no_questions_drops_the_section():
    r = parse_planner_output(GOOD.replace("- Should the fix also cover the mobile page?", "None"))
    assert r.questions == "" and not r.body.startswith("## Questions")


def test_parse_uses_last_block_when_prompt_is_echoed():
    echoed = "PLAN_START\n## Questions\n(example)\n## Plan\n(example)\nPLAN_END\n\nmore noise\n" + GOOD
    r = parse_planner_output(echoed)
    assert "Guard the null session" in r.body and "(example)" not in r.body


def test_parse_tolerates_markdown_around_markers_and_missing_end():
    text = "**PLAN_START**\n## Plan\n1. Do it\nTokens: 1k sent, 1k received.\n"
    r = parse_planner_output(text)
    assert r.body == "## Plan\n1. Do it"


def test_parse_rejects_replies_without_markers_or_plan():
    assert parse_planner_output("Sure! Here is what I'd do: ...") is None
    assert parse_planner_output("PLAN_START\n## Questions\n- why?\nPLAN_END") is None


def test_prompt_first_round_vs_revision():
    s = Session("CA-1")
    first = build_planner_prompt(ISSUE, s)
    assert "Investigate the repository" in first and "Your current plan" not in first
    assert "Fix login" in first and "Button crashes on Safari" in first

    s.plan = "## Plan\n1. old step"
    s.add_message("agent", "- mobile too?")
    s.add_message("user", "yes, mobile too")
    second = build_planner_prompt(ISSUE, s)
    assert "## Plan\n1. old step" in second and "version 0" in second
    assert "[You] - mobile too?" in second and "[Ticket owner] yes, mobile too" in second
    assert "Do NOT change the plan for that" in second and "REPLY_START" in second


def test_prompt_keeps_history_small():
    s = Session("CA-1")
    for i in range(30):
        s.add_message("user", f"msg{i} " + "x" * 5000)
    p = build_planner_prompt(ISSUE, s)
    assert "msg29" in p and "msg0 " not in p  # only the latest messages
    assert len(p) < 12 * 1600 + 3000


def test_approved_plan_text_hides_questions_and_adds_owner_notes():
    s = Session("CA-1")
    s.plan = "## Questions\n- mobile too?\n\n## Plan\n1. step\n\n## Files\n- `a.py`"
    s.add_message("agent", "- mobile too?")
    s.add_message("user", "yes, include mobile")
    text = approved_plan_text(s)
    assert "mobile too?" not in text.split("## Notes")[0]
    assert "## Plan\n1. step" in text and "## Files" in text
    assert "- yes, include mobile" in text


def test_coding_prompt_includes_approved_plan_only_when_given():
    from app.agent import build_task_prompt

    plain = build_task_prompt(ISSUE)
    assert "Approved plan" not in plain and "Fix login" in plain
    with_plan = build_task_prompt(ISSUE, "## Plan\n1. guard null session")
    assert "Approved plan:\n## Plan\n1. guard null session" in with_plan
    assert "the plan wins" in with_plan and with_plan.endswith("Do not change unrelated code.")


# ---- finding files when the ticket names none -------------------------------------------------


def _repo(tmp_path, files: dict[str, int]):
    for name, size in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
    return list(files)


def test_candidate_paths_rank_by_ticket_words_and_skip_junk(tmp_path):
    from app.planner import candidate_paths

    tracked = _repo(tmp_path, {
        "zeta.py": 10, "auth/login_view.py": 10, "logo.png": 10, "data/big.py": 300_000, "docs/notes.md": 10,
    })
    ranked = candidate_paths(tracked, tmp_path, "Login page crashes in the auth view")
    assert ranked[0] == "auth/login_view.py"  # shares 'auth', 'login' and 'view' with the ticket
    assert "logo.png" not in ranked and "data/big.py" not in ranked  # binary / too big for the model
    assert set(ranked) == {"auth/login_view.py", "zeta.py", "docs/notes.md"}


def test_candidate_paths_are_capped(tmp_path):
    from app.planner import candidate_paths

    tracked = _repo(tmp_path, {f"m{i}.py": 5 for i in range(50)})
    assert len(candidate_paths(tracked, tmp_path, "x", limit=10)) == 10


def test_parse_paths_accepts_bullets_and_drops_invented_files():
    from app.planner import parse_paths

    allowed = {"auth.py", "app/views.py", "a b.py"}
    reply = "Sure:\n1. auth.py\n- `app/views.py`\n* \"a b.py\"\nnot_in_repo.py\nauth.py\n"
    assert parse_paths(reply, allowed) == ["auth.py", "app/views.py", "a b.py"]
    assert parse_paths("NONE", allowed) == []


def test_discover_files_uses_the_llm_and_survives_its_failure(tmp_path, monkeypatch):
    from app import planner

    tracked = _repo(tmp_path, {"auth.py": 10, "views.py": 10})
    prompts = []

    def fake_complete(model, prompt):
        prompts.append((model, prompt))
        return "views.py\nmade_up.py\n", {"sent": 500, "received": 20}

    monkeypatch.setattr(planner, "_complete", fake_complete)
    usage = {}
    found = planner.discover_files("m1", ISSUE, "also check the views", tracked, tmp_path, usage)
    assert found == ["views.py"] and usage == {"sent": 500, "received": 20}
    model, prompt = prompts[0]
    assert model == "m1" and "auth.py\nviews.py" in prompt or "views.py\nauth.py" in prompt
    assert "also check the views" in prompt and "Fix login" in prompt

    def boom(model, prompt):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(planner, "_complete", boom)
    assert planner.discover_files("m1", ISSUE, "", tracked, tmp_path) == []  # planning carries on without it


def test_plan_files_from_text_only_returns_real_repo_files():
    from app.planner import plan_files_from_text

    tracked = {"auth.py", "tests/test_auth.py", "views.py"}
    plan = (
        "## Plan\n1. Guard the session in `auth.py` and call it from `views.py`.\n2. Mention `ghost.py`.\n\n"
        "## Files\n- tests/test_auth.py: new regression test\n- auth.py - null check\n- notes.txt: nothing\n"
    )
    assert plan_files_from_text(plan, tracked) == ["auth.py", "views.py", "tests/test_auth.py"]


def test_planner_files_prefer_ticket_and_earlier_rounds_before_asking_the_llm(tmp_path, monkeypatch):
    from app import planner
    from app.config import Settings
    from app.jobs import Job

    tracked = _repo(tmp_path, {"auth.py": 10, "views.py": 10, "other.py": 10})
    monkeypatch.setattr(planner, "CodingAgent", lambda settings, job: object())
    calls = []
    monkeypatch.setattr(planner, "discover_files", lambda *a, **k: calls.append(1) or ["other.py"])
    settings = Settings(
        jira_base_url="https://x", jira_email="a", jira_api_token="t", github_token="g", github_repo="o/r", webhook_secret="w"
    )
    p = planner.Planner(settings, Job("CA-1", "x"))

    named = JiraIssue("CA-1", "Fix", "crash in auth.py", [], "Bug")
    assert p._files_for_plan(named, Session("CA-1"), tracked, tmp_path) == ["auth.py"] and not calls

    vague = JiraIssue("CA-1", "Fix login", "it crashes", [], "Bug")
    s = Session("CA-1")
    s.files = ["views.py"]  # found in an earlier round
    assert p._files_for_plan(vague, s, tracked, tmp_path) == ["views.py"] and not calls

    s2 = Session("CA-1")
    s2.add_message("user", "I think it is in auth.py")  # the owner names a file in chat
    assert p._files_for_plan(vague, s2, tracked, tmp_path) == ["auth.py"] and not calls

    assert p._files_for_plan(vague, Session("CA-1"), tracked, tmp_path) == ["other.py"] and calls == [1]


def test_merge_files_drops_untracked_big_and_duplicate_paths(tmp_path):
    from app.agent import merge_files

    tracked = _repo(tmp_path, {"a.py": 10, "b.py": 10, "big.py": 300_000})
    (tmp_path / "untracked.py").write_text("x")
    got = merge_files(["a.py"], ["a.py", "b.py", "big.py", "untracked.py", "./b.py", "ghost.py"], tracked, tmp_path)
    assert got == ["a.py", "b.py"]
    many = _repo(tmp_path, {f"f{i}.py": 5 for i in range(20)})
    assert len(merge_files([], many, many, tmp_path)) == 8  # capped to protect the token limit


CHAT_ANSWER = "REPLY_START\nYes, only `a.py` changes.\nREPLY_END\nPLAN_CHANGED: no\nTokens: 1k sent, 20 received."
CHAT_CHANGE = (
    "REPLY_START\nAdded the mobile page.\nREPLY_END\nPLAN_CHANGED: yes\nPLAN_START\n## Questions\nNone\n"
    "## Plan\n1. fix `a.py` and `m.py`\nPLAN_END"
)


def test_chat_question_gets_an_answer_and_no_new_plan():
    from app.planner import parse_chat_output

    r = parse_chat_output(CHAT_ANSWER)
    assert r.changed is False and r.reply == "Yes, only `a.py` changes." and r.body == ""


def test_chat_change_returns_reply_and_new_plan():
    from app.planner import parse_chat_output

    r = parse_chat_output(CHAT_CHANGE)
    assert r.changed and r.reply == "Added the mobile page." and "m.py" in r.body


def test_chat_says_yes_but_plan_missing_keeps_old_plan():
    from app.planner import parse_chat_output

    r = parse_chat_output("REPLY_START\nok\nREPLY_END\nPLAN_CHANGED: yes\n")
    assert r.changed is False and "unchanged" in r.reply
    assert parse_chat_output("nonsense") is None


def test_chat_show_flag_means_repost_the_plan():
    from app.planner import parse_chat_output

    r = parse_chat_output("REPLY_START\nSure.\nREPLY_END\nPLAN_CHANGED: show\n")
    assert r.show and not r.changed and r.reply == "Sure."


# ---- LLM failures are named, not hidden ---------------------------------------------------------


def test_llm_problem_names_the_cause_and_quotes_the_provider():
    from app.agent import empty_reply, llm_problem

    rate = "litellm.RateLimitError: GroqException - Rate limit reached for model, tokens per minute (TPM): Limit 8000\nTokens: 1k sent"
    msg = llm_problem(rate)
    assert "rate or usage limit" in msg and "Provider said: litellm.RateLimitError" in msg
    assert "too large" in llm_problem("litellm.ContextWindowExceededError: input is 12000 tokens, over the limit")
    assert "content filter" in llm_problem("litellm.ContentPolicyViolationError: blocked by content_filter")
    assert "API key was rejected" in llm_problem("litellm.AuthenticationError: Invalid API key")
    assert llm_problem("Here is my plan. All good.\nTokens: 1k sent, 200 received.") is None
    assert empty_reply("Aider v0.86\nModel: x\nTokens: 1k sent, 0 received.") and not empty_reply("x" * 100)


def _planner_with_output(monkeypatch, tmp_path, output):
    from app import planner
    from app.config import Settings
    from app.jobs import Job

    class FakeAgent:
        class gh:
            default_branch = staticmethod(lambda: "main")

        def __init__(self, settings, job): pass
        def _run(self, cmd, **k):  # the "git clone": just make the folder layout the planner expects
            import pathlib
            pathlib.Path(cmd[-1], ".git", "info").mkdir(parents=True, exist_ok=True)
            return ""
        def _git(self, *a, **k): return "a.py\0"
        def _run_aider(self, *a, **k): return output

    monkeypatch.setattr(planner, "CodingAgent", FakeAgent)
    monkeypatch.setattr(planner, "discover_files", lambda *a, **k: [])
    s = Settings(jira_base_url="https://x", jira_email="a", jira_api_token="t", github_token="g", github_repo="o/r",
                 webhook_secret="w", workdir=str(tmp_path))
    return planner.Planner(s, Job("CA-1", "x"))


def test_planner_reports_an_llm_error_instead_of_a_format_error(monkeypatch, tmp_path):
    import pytest

    from app import planner
    from app.agent import AgentError

    waits = []
    monkeypatch.setattr(planner.time, "sleep", waits.append)
    p = _planner_with_output(monkeypatch, tmp_path, "litellm.RateLimitError: tokens per minute (TPM) limit reached\n")
    with pytest.raises(AgentError, match="LLM call failed.*rate or usage limit"):
        p.run(ISSUE, Session("CA-1"))
    assert waits == [p.s.rate_limit_wait_seconds]  # waited once, tried again, then reported it
    p = _planner_with_output(monkeypatch, tmp_path, "Aider v0.86\nTokens: 900 sent, 0 received.\n")
    with pytest.raises(AgentError, match="empty reply"):
        p.run(ISSUE, Session("CA-1"))


def test_failed_file_discovery_becomes_a_warning(tmp_path, monkeypatch):
    from app import planner

    tracked = _repo(tmp_path, {"auth.py": 10})
    monkeypatch.setattr(planner, "_complete", lambda m, p: (_ for _ in ()).throw(RuntimeError("litellm.RateLimitError: TPM limit")))
    warnings = []
    assert planner.discover_files("m", ISSUE, "", tracked, tmp_path, None, warnings) == []
    assert len(warnings) == 1 and "rate or usage limit" in warnings[0] and "planned without" in warnings[0]


def test_run_aider_records_prompt_and_output_even_on_failure(monkeypatch, tmp_path):
    import pytest

    from app import agent as agent_mod
    from app.config import Settings
    from app.jobs import Job

    s = Settings(jira_base_url="https://x", jira_email="a", jira_api_token="t", github_token="g-secret-token", github_repo="o/r",
                 webhook_secret="w", workdir=str(tmp_path))
    job = Job("CA-1", "x")
    a = agent_mod.CodingAgent(s, job)
    monkeypatch.setattr(a, "_run", lambda cmd, **k: "model said hi g-secret-token\nTokens: 1k sent, 5 received.")
    out = a._run_aider(ISSUE, tmp_path, message="plan this", files=[], chat_mode="ask", model="m1")
    assert job.runs[0]["step"] == "plan" and job.runs[0]["model"] == "m1" and job.runs[0]["prompt"] == "plan this"
    assert "g-secret-token" not in job.runs[0]["output"] and "model said hi" in out

    def boom(cmd, **k):
        raise RuntimeError("aider exploded with g-secret-token")

    monkeypatch.setattr(a, "_run", boom)
    with pytest.raises(RuntimeError):
        a._run_aider(ISSUE, tmp_path, message="fix", files=["a.py"])
    assert job.runs[1]["step"] == "fix" and job.runs[1]["ok"] is False and "g-secret-token" not in job.runs[1]["output"]


def test_planner_checks_deletions_against_the_repo(monkeypatch, tmp_path):
    out = ("PLAN_START\n## Questions\nNone\n## Plan\n1. Remove the listed files.\n## Delete\n- `a.py`\n- `ghost.bin`\n"
           "## Files\n- `a.py`\n## Risks\nNone\nPLAN_END\n")
    r = _planner_with_output(monkeypatch, tmp_path, out).run(ISSUE, Session("CA-1"))
    assert r.delete_files == ["a.py"]
    assert "## Delete\n- `a.py`" in r.body and "ghost.bin" not in r.body
    assert any("ghost.bin" in w for w in r.warnings)
    assert "a.py" not in r.files  # a file that is deleted is not one to edit


def test_a_failing_fallback_model_is_named_in_the_error(monkeypatch, tmp_path):
    import pytest

    from app import planner
    from app.agent import AgentError

    monkeypatch.setattr(planner.time, "sleep", lambda s: None)
    p = _planner_with_output(monkeypatch, tmp_path, "litellm.RateLimitError: PerDay quota ... model gemini-2.5-flash\n")
    p.s.planner_fallback_model = "gemini/old-model"
    with pytest.raises(AgentError, match="fallback model gemini/old-model was tried"):
        p.run(ISSUE, Session("CA-1"))
