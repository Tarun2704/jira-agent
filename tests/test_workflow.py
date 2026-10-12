"""The plan-first state machine, with Slack, Jira, the planner and the coding agent faked."""
import fakeredis
import pytest

from app import workflow as wf_mod
from app.config import Settings
from app.jira_client import JiraIssue
from app.jobs import Job
from app.planner import PlanResult
from app.slack_client import ACTION_BACK, ACTION_CONFIRM, ACTION_PROCEED
from app.store import (
    AWAITING_REPLY, CONFIRMING, DELIVERED, DONE, EXECUTING, PLANNING, REVISING, SessionStore,
)

ME = "U_ME"


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.n = [], [], 0

    def post(self, text, *, channel="", blocks=None, thread_ts=""):
        self.n += 1
        ref = {"channel": channel or "D1", "ts": f"1.{self.n}"}
        self.posts.append({"text": text, "blocks": blocks, "thread_ts": thread_ts, **ref})
        return ref

    def update(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})


class FakeGitHub:
    def __init__(self):
        self.prs, self.comments, self.fail = {}, {}, False

    def get_pr(self, number):
        if self.fail:
            raise RuntimeError("github down")
        return self.prs[number]

    def review_comments(self, number):
        if self.fail:
            raise RuntimeError("github down")
        return self.comments.get(number, [])


class FakeJira:
    def __init__(self, labels=("ai-agent",)):
        self.comments, self.labels = [], list(labels)

    def get_issue(self, key):
        return JiraIssue(key, "Fix login", "crashes", self.labels, "Bug", reporter_account_id="acc-1")

    def add_comment(self, key, body):
        if getattr(self, "down", False):
            raise RuntimeError("jira down")
        self.comments.append((key, body))

    def human_comments_since(self, key, since_ts=0.0):
        return list(getattr(self, "human", []))


@pytest.fixture
def env(monkeypatch):
    settings = Settings(
        jira_base_url="https://x.atlassian.net", jira_email="a@b.c", jira_api_token="jt",
        github_token="gh-secret-token", github_repo="o/r", webhook_secret="w",
        slack_bot_token="xoxb-test", slack_user_id=ME, redis_url="redis://fake", plan_reminder_hours=48,
    )
    store = SessionStore(fakeredis.FakeRedis(decode_responses=True))
    slack, jira, gh, submitted = FakeSlack(), FakeJira(), FakeGitHub(), []
    wf = wf_mod.Workflow(settings, store, slack, lambda kind, key: submitted.append((kind, key)) or True, lambda: jira, lambda: gh)
    wf._sleep = lambda s: None  # no real waiting in retries

    class Env:
        pass

    e = Env()
    e.wf, e.store, e.slack, e.jira, e.submitted, e.settings, e.gh = wf, store, slack, jira, submitted, settings, gh
    e.plans = [PlanResult("- mobile too?", "## Questions\n- mobile too?\n\n## Plan\n1. fix `a.py`", files=["a.py"])]

    class FakePlanner:
        def __init__(self, settings, job):
            pass

        def run(self, issue, session, context="", branch=None):
            e.seen_messages = [m["text"] for m in session.messages]
            plan = e.plans.pop(0)
            if isinstance(plan, Exception):
                raise plan
            return plan

    monkeypatch.setattr(wf_mod, "Planner", FakePlanner)
    return e


def plan_job(e, key="CA-1"):
    job = Job(key, "x", kind="plan")
    job.start()
    e.wf.run_plan_job(job)
    return job


def test_trigger_creates_session_and_queues_plan_job(env):
    assert env.wf.on_trigger("CA-1")
    assert env.submitted == [("plan", "CA-1")]
    assert env.store.get("CA-1").status == PLANNING
    assert not env.wf.on_trigger("CA-1")  # second trigger while active: ignored


def test_first_plan_goes_to_dm_with_questions_and_waits(env):
    env.wf.on_trigger("CA-1")
    job = plan_job(env)
    s = env.store.get("CA-1")
    assert job.status == "succeeded"
    assert s.status == AWAITING_REPLY and s.plan_version == 1 and s.title == "Fix login"
    assert s.files == ["a.py"]  # remembered for revisions and for the coding run
    post = env.slack.posts[0]
    assert post["thread_ts"] == "" and post["channel"] == "D1"  # a new DM thread
    assert s.slack_thread_ts == post["ts"] and env.store.key_for_thread("D1", post["ts"]) == "CA-1"
    assert s.reporter_account_id == "acc-1"
    assert [m["role"] for m in s.messages] == ["agent"]  # the questions are in the history
    assert any("sent it to Slack" in c[1] for c in env.jira.comments)
    # nothing has been executed
    assert ("execute", "CA-1") not in env.submitted


def test_change_request_posts_new_version_keeps_old_one_visible_and_answers_under_your_message(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.plans.append(PlanResult("", "## Plan\n1. fix `a.py` and `m.py`", reply="Added mobile."))

    # a plain DM message (no thread): answered in the main DM, like a normal chat
    assert env.wf.on_user_message("D1", "", ME, "yes, mobile too", "9.9") == "revise:awaiting_reply"
    ack = env.slack.posts[-1]
    assert ack["thread_ts"] == "" and "Looking into it" in ack["text"]
    plan_job(env)

    s = env.store.get("CA-1")
    assert env.seen_messages == ["- mobile too?", "yes, mobile too"]
    assert s.status == AWAITING_REPLY and s.plan_version == 2 and s.questions == "" and s.pending_acks == []
    new = [p for p in env.slack.posts if p["blocks"] and "Plan for" in p["text"]][-1]
    assert new["thread_ts"] == ""  # right below your message in the main DM, not in the old thread
    old = [u for u in env.slack.updates if u["ts"] == root][-1]  # v1 is re-rendered, not replaced by a stub
    assert "a.py" in str(old["blocks"]) and "Superseded by v2" in str(old["blocks"])
    assert not any(b["type"] == "actions" for b in old["blocks"])
    answer = [u for u in env.slack.updates if u["ts"] == ack["ts"]][-1]  # the ack turned into the answer
    assert "Added mobile." in answer["text"] and "is below" in answer["text"]
    assert s.plan_msg_ts == new["ts"]
    assert [m["text"] for m in s.messages if m["role"] == "agent"][-1] == "Added mobile."


def test_a_question_is_answered_without_a_new_plan_version(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    before = len(env.slack.posts)
    env.plans.append(PlanResult("", "", reply="Yes, only a.py.", changed=False))

    env.wf.on_user_message("D1", "", ME, "only one file is it?", "9.9")
    plan_job(env)

    s = env.store.get("CA-1")
    assert s.plan_version == 1 and s.status == AWAITING_REPLY and s.plan_msg_ts == root
    assert len(env.slack.posts) == before + 1  # only the "Looking into it" message; no new plan posted
    assert not [u for u in env.slack.updates if u["ts"] == root]  # old plan untouched, button still there
    assert "Yes, only a.py." in env.slack.updates[-1]["text"] and env.slack.updates[-1]["ts"] == env.slack.posts[-1]["ts"]
    assert s.files == ["a.py"]


def test_only_the_owner_can_chat_or_press_buttons(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    assert env.wf.on_user_message("D1", root, "U_OTHER", "do it") == "ignored"
    assert env.wf.on_action(ACTION_PROCEED, "CA-1", "U_OTHER", "D1", root) == "ignored"
    assert env.store.get("CA-1").status == AWAITING_REPLY


def test_messages_while_planning_trigger_another_round(env):
    env.wf.on_trigger("CA-1")
    # you write while the planner is still working on the first plan
    s = env.store.update("CA-1", lambda x: x.add_message("user", "also handle logout"))
    env.plans[:] = [PlanResult("", "## Plan\n1. x")]
    plan_job(env)
    assert env.store.get("CA-1").status == AWAITING_REPLY  # the message was handled by this very round
    assert env.seen_messages == ["also handle logout"]

    # now one that lands mid-run: simulate by adding it from inside the planner
    env.store.update("CA-1", lambda x: setattr(x, "status", REVISING))
    original = wf_mod.Planner

    class Racing(original):
        def run(self, issue, session, context="", branch=None):
            env.store.update("CA-1", lambda x: x.add_message("user", "wait, also X"))
            return PlanResult("", "## Plan\n1. y")

    wf_mod.Planner = Racing
    try:
        plan_job(env)
    finally:
        wf_mod.Planner = original
    s = env.store.get("CA-1")
    assert s.status == REVISING and s.unhandled  # needs another round
    env.submitted.clear()
    env.wf.resume_if_needed("CA-1")
    assert env.submitted == [("plan", "CA-1")]


def test_proceed_then_confirm_then_execute_with_plan(env, monkeypatch):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    s = env.store.get("CA-1")
    root = s.slack_thread_ts

    assert env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root) == "confirming"
    assert env.store.get("CA-1").status == CONFIRMING
    shown = env.slack.updates[-1]  # the question replaces the button on the clicked message itself
    assert shown["ts"] == root and "can't be stopped" in str(shown["blocks"]) and "Yes, start now" in str(shown["blocks"])
    assert "a.py" in str(shown["blocks"])  # the plan stays on screen
    confirm = {"ts": root}
    assert ("execute", "CA-1") not in env.submitted  # NOT started by the first click
    assert env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root) == "ignored:confirming"  # double click

    assert env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", confirm["ts"]) == "executing"
    assert env.store.get("CA-1").status == EXECUTING
    assert env.submitted[-1] == ("execute", "CA-1")
    assert "Started CA-1" in env.slack.posts[-1]["text"] and env.slack.posts[-1]["thread_ts"] == ""  # new message in the main chat
    assert "approved" in env.slack.updates[-1]["text"] and not any(b["type"] == "actions" for b in env.slack.updates[-1]["blocks"])
    assert env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", confirm["ts"]).startswith("ignored")  # no 2nd run

    given = {}

    class FakeAgent:
        def __init__(self, settings, job, plan="", plan_files=None, followup=None, deletions=None):
            given["plan"], given["files"], self.job = plan, plan_files, job

        def handle_issue(self, key):
            self.job.pr_url = "https://github.com/o/r/pull/9"
            self.job.finish("succeeded")

    monkeypatch.setattr(wf_mod, "CodingAgent", FakeAgent)
    job = Job("CA-1", "x", kind="execute")
    job.start()
    env.wf.run_execute_job(job)
    assert "fix `a.py`" in given["plan"] and "mobile too?" not in given["plan"].split("## Notes")[0]
    assert given["files"] == ["a.py"]  # the plan's files go to the coding agent
    s = env.store.get("CA-1")
    assert s.status == DELIVERED and s.result.endswith("/pull/9")  # waits for follow-ups now
    assert s.rounds[-1]["outcome"] == "delivered" and s.rounds[-1]["n"] == 1
    assert "pull request opened" in env.slack.posts[-1]["text"] and "follow-ups for 14 days" in env.slack.posts[-1]["text"]
    assert env.store.has_lease("CA-1") is False  # lease released


def test_go_back_returns_to_waiting(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root)
    assert env.wf.on_action(ACTION_BACK, "CA-1", ME, "D1", root) == "back"
    assert any(b["type"] == "actions" for b in env.slack.updates[-1]["blocks"])  # Proceed button is back
    assert env.store.get("CA-1").status == AWAITING_REPLY


def test_typing_during_confirmation_cancels_it_and_revises(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root)
    assert env.wf.on_user_message("D1", root, ME, "actually, skip tests") == "revise:confirming"
    assert env.store.get("CA-1").status == REVISING


def test_cannot_change_plan_once_executing(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root)
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", root)
    assert env.wf.on_user_message("D1", root, ME, "stop!") == "rejected:executing"
    assert "can't be changed" in env.slack.posts[-1]["text"]
    assert env.store.get("CA-1").status == EXECUTING


def test_planner_failure_reports_and_waits_for_a_reply(env):
    env.plans[:] = [RuntimeError("boom with gh-secret-token inside")]
    env.wf.on_trigger("CA-1")
    job = plan_job(env)
    s = env.store.get("CA-1")
    assert job.status == "failed" and s.status == AWAITING_REPLY and s.plan == ""
    said = env.slack.posts[-1]["text"]
    assert "couldn't produce the plan" in said and "gh-secret-token" not in said
    assert env.store.key_for_thread("D1", s.slack_thread_ts) == "CA-1"
    assert any("Planner failed" in c[1] and "gh-secret-token" not in c[1] for c in env.jira.comments)
    # Proceed is refused while there is no plan; a reply re-plans
    assert env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", s.slack_thread_ts) == "no_plan"
    env.plans[:] = [PlanResult("", "## Plan\n1. ok")]
    env.wf.on_user_message("D1", s.slack_thread_ts, ME, "try again")
    plan_job(env)
    assert env.store.get("CA-1").status == AWAITING_REPLY and env.store.get("CA-1").plan


@pytest.mark.parametrize("job_status,expected", [("no_changes", "no changes"), ("failed", "failed"), ("skipped", "skipped")])
def test_execution_outcomes_are_reported(env, monkeypatch, job_status, expected):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", root)
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", root)

    class FakeAgent:
        def __init__(self, settings, job, plan="", plan_files=None, followup=None, deletions=None):
            self.job = job

        def handle_issue(self, key):
            self.job.finish(job_status, "reason with gh-secret-token")

    monkeypatch.setattr(wf_mod, "CodingAgent", FakeAgent)
    job = Job("CA-1", "x", kind="execute")
    job.start()
    env.wf.run_execute_job(job)
    s = env.store.get("CA-1")
    assert s.status == DELIVERED and s.rounds[-1]["outcome"] == expected  # always open for another try
    assert "gh-secret-token" not in env.slack.posts[-1]["text"]


def test_label_removed_cancels_planning(env):
    env.jira.labels = []
    env.wf.on_trigger("CA-1")
    job = plan_job(env)
    assert env.store.get("CA-1").status == "cancelled" and job.status == "skipped"
    assert env.slack.posts == []


def test_lease_blocks_a_second_worker(env):
    env.wf.on_trigger("CA-1")
    assert env.store.acquire_lease("CA-1", "other-instance", 60)
    job = plan_job(env)
    assert job.status == "skipped" and env.slack.posts == []
    assert env.store.has_lease("CA-1")  # the other worker's lease is untouched


def test_tick_requeues_orphaned_jobs_only(env):
    env.wf.on_trigger("CA-1")  # planning, no lease: its worker died
    env.wf.on_trigger("CA-2")
    env.store.acquire_lease("CA-2", "alive", 60)  # CA-2 is genuinely running
    env.submitted.clear()
    out = env.wf.tick()
    assert out["requeued"] == ["CA-1"] and env.submitted == [("plan", "CA-1")]

    env.store.transition("CA-1", PLANNING, EXECUTING)
    env.submitted.clear()
    env.wf.tick()
    assert env.submitted == [("execute", "CA-1")]  # an interrupted run restarts from scratch


def test_tick_reminds_once_after_48h_and_tags_the_reporter(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    env.jira.comments.clear()
    assert env.wf.tick()["reminded"] == []  # too early

    env.store.update("CA-1", lambda x: setattr(x, "last_activity_at", x.last_activity_at - 49 * 3600))
    assert env.wf.tick()["reminded"] == ["CA-1"]
    key, body = env.jira.comments[-1]
    assert key == "CA-1" and "[~accountid:acc-1]" in body and "waiting for your reply" in body
    assert "Reminder" in env.slack.posts[-1]["text"]
    assert env.wf.tick()["reminded"] == []  # only once
    assert len(env.jira.comments) == 1

    # a reply resets the clock and the reminder flag
    env.wf.on_user_message("D1", env.store.get("CA-1").slack_thread_ts, ME, "still here")
    assert env.store.get("CA-1").reminded is False


def test_loose_message_goes_to_the_only_waiting_plan(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    assert env.wf.on_user_message("D1", "", ME, "mobile too") == "revise:awaiting_reply"
    env.wf.on_trigger("CA-2")
    env.plans.append(PlanResult("", "## Plan\n1. z"))
    plan_job(env, "CA-2")
    plan_job(env, "CA-1")  # (queued re-plan for CA-1 runs; both end up waiting)
    assert env.wf.on_user_message("D1", "", ME, "which one?") == "ambiguous"


def test_show_me_the_plan_again_reposts_it_below_your_message(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.plans.append(PlanResult("", "", reply="Here it is.", changed=False, show=True))
    env.wf.on_user_message("D1", "", ME, "show me the plan again", "9.9")
    plan_job(env)
    s = env.store.get("CA-1")
    again = env.slack.posts[-1]
    assert "Plan for" in again["text"] and again["thread_ts"] == "" and s.plan_version == 1
    assert any(b["type"] == "actions" for b in again["blocks"]) and s.plan_msg_ts == again["ts"]
    old = [u for u in env.slack.updates if u["ts"] == root][-1]
    assert "Posted again below" in str(old["blocks"]) and not any(b["type"] == "actions" for b in old["blocks"])


def test_non_fatal_planner_warnings_are_posted_in_the_main_chat(env):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", warnings=["Couldn't search the repo (rate limit)."])]
    env.wf.on_trigger("CA-1")
    plan_job(env)
    last = env.slack.posts[-1]
    assert ":warning:" in last["text"] and "rate limit" in last["text"] and last["thread_ts"] == ""


def test_llm_failure_during_chat_is_reported_and_the_ack_is_closed(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    env.plans[:] = [RuntimeError("The LLM call failed. The LLM provider's rate or usage limit was hit.")]
    env.wf.on_user_message("D1", "", ME, "add tests too", "9.9")
    ack = env.slack.posts[-1]
    job = plan_job(env)
    assert job.status == "failed" and "rate or usage limit" in env.slack.posts[-1]["text"]
    assert any(u["ts"] == ack["ts"] and "Couldn't process" in u["text"] for u in env.slack.updates)
    assert env.store.get("CA-1").status == AWAITING_REPLY and env.store.get("CA-1").plan  # old plan kept


def test_aider_runs_are_saved_for_observability_and_scrubbed(env, monkeypatch):
    env.wf.on_trigger("CA-1")

    class RecordingPlanner:
        def __init__(self, settings, job):
            self.job = job

        def run(self, issue, session, context="", branch=None):
            self.job.record_run("plan", "m1", "Plan CA-1 with gh-secret-token", "PLAN_START ... gh-secret-token")
            return PlanResult("", "## Plan\n1. x", files=["a.py"])

    monkeypatch.setattr(wf_mod, "Planner", RecordingPlanner)
    job = plan_job(env)
    runs = env.store.get_runs("CA-1")
    assert len(runs) == 1 and runs[0]["step"] == "plan" and runs[0]["job"] == job.id and runs[0]["kind"] == "plan"
    assert "gh-secret-token" not in runs[0]["prompt"] + runs[0]["output"] and "PLAN_START" in runs[0]["output"]

    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", env.store.get("CA-1").slack_thread_ts)
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", env.store.get("CA-1").slack_thread_ts)

    class FakeAgent:
        def __init__(self, settings, job, plan="", plan_files=None, followup=None, deletions=None):
            self.job = job

        def handle_issue(self, key):
            self.job.record_run("code", "m2", "do it", "Applied edit to a.py\nTokens: 1k sent, 50 received.")
            self.job.record_run("fix", "m2", "fix syntax", "fixed")
            self.job.finish("succeeded")

    monkeypatch.setattr(wf_mod, "CodingAgent", FakeAgent)
    ex = Job("CA-1", "x", kind="execute")
    ex.start()
    env.wf.run_execute_job(ex)
    assert [(r["kind"], r["step"]) for r in env.store.get_runs("CA-1")] == [("plan", "plan"), ("execute", "code"), ("execute", "fix")]


# ---- several tickets at once ------------------------------------------------------------------


def two_waiting(env):
    env.plans[:] = [PlanResult("", "## Plan\n1. a", files=["a.py"]), PlanResult("", "## Plan\n1. b", files=["b.py"])]
    env.wf.on_trigger("CA-1")
    plan_job(env, "CA-1")
    env.wf.on_trigger("CA-2")
    plan_job(env, "CA-2")


def test_ticket_prefix_routes_the_message_and_is_stripped(env):
    two_waiting(env)
    env.plans.append(PlanResult("", "", reply="ok", changed=False))
    assert env.wf.on_user_message("D1", "", ME, "ca-2: also add tests", "7.7") == "revise:awaiting_reply"
    assert env.store.get("CA-2").status == REVISING and env.store.get("CA-1").status == AWAITING_REPLY
    assert env.store.get("CA-2").messages[-1]["text"] == "also add tests"
    assert env.wf.on_user_message("D1", "", ME, "CA-1", "7.8") == "empty_after_key"


def test_ambiguous_message_asks_which_ticket_and_delivers_after_the_click(env):
    two_waiting(env)
    assert env.wf.on_user_message("D1", "", ME, "also handle logout", "7.7") == "ambiguous"
    ask = env.slack.posts[-1]
    buttons = [b for blk in ask["blocks"] if blk["type"] == "actions" for b in blk["elements"]]
    assert [b["value"] for b in buttons] == ["CA-1|7.7", "CA-2|7.7"]
    ids = [b["action_id"] for b in buttons]
    assert len(set(ids)) == len(ids)  # Slack rejects a block whose buttons share an action_id
    assert env.store.get("CA-1").messages == [] and env.store.get("CA-2").messages == []  # nothing applied yet

    assert env.wf.on_action(buttons[1]["action_id"], "CA-2|7.7", ME, "D1", ask["ts"]) == "revise:awaiting_reply"
    assert env.store.get("CA-2").messages[-1]["text"] == "also handle logout"
    assert env.store.get("CA-1").status == AWAITING_REPLY
    assert env.wf.on_action(buttons[1]["action_id"], "CA-2|7.7", ME, "D1", ask["ts"]) == "expired"  # a second click does nothing


def sc_pick():
    from app.slack_client import ACTION_PICK
    return ACTION_PICK


def test_replies_under_any_plan_version_find_their_ticket(env):
    two_waiting(env)
    v1_ts = env.store.get("CA-1").plan_msg_ts
    env.plans.append(PlanResult("", "", reply="ok", changed=False))
    assert env.wf.on_user_message("D1", v1_ts, ME, "and mobile") == "revise:awaiting_reply"
    assert env.store.get("CA-1").status == REVISING and env.store.get("CA-2").status == AWAITING_REPLY


def test_tickets_command_lists_open_tickets_with_show_plan_buttons(env):
    assert env.wf.on_user_message("D1", "", ME, "tickets") == "tickets"
    assert "No open tickets" in env.slack.posts[-1]["text"]
    two_waiting(env)
    assert env.wf.on_user_message("D1", "", ME, "Tickets?") == "tickets"
    post = env.slack.posts[-1]
    text = str(post["blocks"])
    assert "CA-1" in text and "CA-2" in text and "waiting for your approval" in text and "Show plan" in text
    assert "CA-7: your message" in text  # the hint appears when several are open
    before = len(env.slack.posts)
    shows = [blk["accessory"]["action_id"] for blk in post["blocks"] if blk.get("accessory")]
    assert shows == ["show_plan:CA-1", "show_plan:CA-2"]
    assert env.wf.on_action("show_plan:CA-1", "CA-1", ME, "D1", post["ts"]) == "shown"
    shown = env.slack.posts[-1]
    assert len(env.slack.posts) == before + 1 and "Plan for CA-1" in shown["text"]
    assert env.store.get("CA-1").plan_msg_ts == shown["ts"]
    assert any("Posted again below" in str(u["blocks"]) for u in env.slack.updates)


def test_messages_carry_the_ticket_key_only_when_several_tickets_are_open(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    env.wf.on_user_message("D1", "", ME, "hi", "7.1")
    assert env.slack.posts[-1]["text"].startswith(":hourglass")  # one ticket: no tag
    two_waiting_env = env
    env.wf.on_trigger("CA-2")
    env.plans[:] = [PlanResult("", "## Plan\n1. b")]
    plan_job(env, "CA-2")
    env.wf.on_user_message("D1", "", ME, "CA-1: more", "7.2")
    assert env.slack.posts[-1]["text"].startswith("*CA-1* · :hourglass")


def test_a_refused_rich_message_falls_back_to_plain_text(env):
    two_waiting(env)
    real_post = env.slack.post

    def picky(text, *, channel="", blocks=None, thread_ts=""):
        if blocks:
            raise RuntimeError("chat.postMessage: invalid_blocks")
        return real_post(text, channel=channel, blocks=blocks, thread_ts=thread_ts)

    env.slack.post = picky
    assert env.wf.on_user_message("D1", "", ME, "also logout", "7.7") == "ambiguous"
    last = env.slack.posts[-1]
    assert "Which ticket is this about?" in last["text"] and "CA-1: your message" in last["text"] and not last["blocks"]


# ---- follow-ups after delivery ------------------------------------------------------------------

PR_URL = "https://github.com/o/r/pull/12"


class RecordingAgent:
    """Stands in for the coding agent: records how it was told to deliver, then 'delivers'."""

    calls: list = []
    delivery = {"branch": "ai/CA-1-fix", "pr_number": 12, "pr_url": PR_URL, "diff": "+new line", "summary": "Fixed it"}

    def __init__(self, settings, job, plan="", plan_files=None, followup=None, deletions=None):
        self.job, self.followup, self.plan, self.deletions = job, followup, plan, deletions
        RecordingAgent.calls.append(self)

    def handle_issue(self, key):
        d = dict(RecordingAgent.delivery)
        if self.followup and self.followup["mode"] == "continue":
            d.update(pr_url=self.followup["pr_url"], pr_number=self.followup["pr_number"], branch=self.followup["branch"])
        elif self.followup:
            d.update(branch=self.followup["branch"], pr_number=13, pr_url="https://github.com/o/r/pull/13")
        self.job.delivery, self.job.pr_url = d, d["pr_url"]
        self.job.finish("succeeded")


def run_execute(env, monkeypatch, key="CA-1"):
    monkeypatch.setattr(wf_mod, "CodingAgent", RecordingAgent)
    job = Job(key, "x", kind="execute")
    job.start()
    env.wf.run_execute_job(job)
    return job


def approve(env, key="CA-1"):
    s = env.store.get(key)
    ts = s.plan_msg_ts
    env.wf.on_action(ACTION_PROCEED, key, ME, "D1", ts)
    env.wf.on_action(ACTION_CONFIRM, key, ME, "D1", ts)


def delivered(env, monkeypatch, key="CA-1"):
    RecordingAgent.calls = []
    env.wf.on_trigger(key)
    plan_job(env, key)
    approve(env, key)
    run_execute(env, monkeypatch, key)
    assert env.store.get(key).status == DELIVERED


def test_delivery_keeps_the_ticket_open_and_archives_the_round(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    s = env.store.get("CA-1")
    r = s.rounds[0]
    assert (r["n"], r["outcome"], r["pr_number"], r["summary"], r["diff"]) == (1, "delivered", 12, "Fixed it", "+new line")
    assert s.pr_number == 12 and s.pr_url == PR_URL and s.branch == "ai/CA-1-fix"
    assert "CA-1" in env.store.active_keys()
    result_msg = env.slack.posts[-1]
    assert env.store.key_for_thread("D1", result_msg["ts"]) == "CA-1"  # replying under the result finds the ticket
    assert RecordingAgent.calls[0].followup is None  # round 1 behaves exactly as before


def test_a_question_after_delivery_is_answered_and_starts_no_round(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.plans.append(PlanResult("", "", reply="I changed a.py only.", changed=False))
    assert env.wf.on_user_message("D1", "", ME, "what did you change?", "8.1") == "followup"
    assert env.store.get("CA-1").status == REVISING
    plan_job(env)
    s = env.store.get("CA-1")
    assert s.status == DELIVERED and s.round_no == 1 and len(s.rounds) == 1 and not s.followup
    assert any("I changed a.py only." in u["text"] for u in env.slack.updates)


def test_a_thank_you_after_delivery_starts_nothing(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    env.submitted.clear()
    assert env.wf.on_user_message("D1", "", ME, "Thanks!", "8.2") == "thanks"
    assert env.submitted == [] and env.store.get("CA-1").status == DELIVERED


def test_a_change_request_after_delivery_becomes_round_two_on_the_open_pr(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "open", "draft": True, "branch": "ai/CA-1-fix"}
    seen = {}
    original = wf_mod.Planner

    class Spy(original):
        def run(self, issue, session, context="", branch=None):
            seen.update(followup=session.followup, branch=branch, context=context)
            return PlanResult("", "## Plan\n1. add tests", reply="Added a test step.")

    monkeypatch.setattr(wf_mod, "Planner", Spy)
    env.jira.human = [{"author": "Dana", "body": "please also cover logout", "at": ""}]
    env.wf.on_user_message("D1", "", ME, "also add tests", "8.3")
    plan_job(env)
    s = env.store.get("CA-1")
    assert seen["followup"] is True and seen["branch"] == "ai/CA-1-fix" and "please also cover logout" in seen["context"]
    assert s.status == AWAITING_REPLY and s.round_no == 2 and s.plan_version == 1 and not s.followup
    plan_msg = [p for p in env.slack.posts if "Follow-up plan" in p["text"]][-1]
    assert "round 2" in str(plan_msg["blocks"]) and plan_msg["thread_ts"] == ""
    assert any("Follow-up round 2" in c[1] and "also add tests" in c[1] for c in env.jira.comments)

    ts = s.plan_msg_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", ts)
    assert "adds a commit to the open PR #12" in str(env.slack.updates[-1]["blocks"])
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", ts)
    run_execute(env, monkeypatch)
    fu = RecordingAgent.calls[-1].followup
    assert (fu["mode"], fu["branch"], fu["pr_number"], fu["round"], fu["require_label"]) == ("continue", "ai/CA-1-fix", 12, 2, False)
    assert "Fixed it" in fu["history"] and "+new line" in fu["history"]  # the agent knows what round 1 did
    s = env.store.get("CA-1")
    assert s.status == DELIVERED and [r["n"] for r in s.rounds] == [1, 2]
    assert "pushed to the pull request" in env.slack.posts[-1]["text"] and "(round 2)" in env.slack.posts[-1]["text"]


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_a_follow_up_after_a_closed_or_merged_pr_starts_fresh(env, monkeypatch, state):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": state, "draft": False, "branch": "ai/CA-1-fix"}
    env.plans.append(PlanResult("", "## Plan\n1. more", reply="ok"))
    env.wf.on_user_message("D1", "", ME, "one more thing", "8.4")
    plan_job(env)
    ts = env.store.get("CA-1").plan_msg_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", ts)
    assert "starts from the main branch" in str(env.slack.updates[-1]["blocks"])
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", ts)
    run_execute(env, monkeypatch)
    fu = RecordingAgent.calls[-1].followup
    assert (fu["mode"], fu["branch"], fu["previous_pr"]) == ("fresh", "ai/CA-1-r2", PR_URL)
    assert "opened a new pull request" in env.slack.posts[-1]["text"]
    assert env.store.get("CA-1").pr_number == 13


def test_github_down_at_execute_time_is_reported_and_nothing_runs(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.plans.append(PlanResult("", "## Plan\n1. more"))
    env.wf.on_user_message("D1", "", ME, "more please", "8.5")
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "open", "draft": False, "branch": "ai/CA-1-fix"}
    plan_job(env)
    approve(env)
    env.gh.fail = True
    calls_before = len(RecordingAgent.calls)
    job = run_execute(env, monkeypatch)
    assert job.status == "failed" and len(RecordingAgent.calls) == calls_before  # the agent never ran
    s = env.store.get("CA-1")
    assert s.status == DELIVERED and s.rounds[-1]["outcome"] == "failed"
    assert "Couldn't check the pull request's state" in env.slack.posts[-1]["text"]


def test_jira_retrigger_of_a_delivered_ticket_forces_a_plan_and_needs_the_label(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.submitted.clear()
    assert env.wf.on_trigger("CA-1") is True and env.submitted == [("plan", "CA-1")]
    s = env.store.get("CA-1")
    assert (s.status, s.followup, s.force_plan, s.round_source) == (REVISING, True, True, "jira")
    env.jira.labels = []  # the label is gone again: back to waiting, nothing cancelled
    job = plan_job(env)
    assert job.status == "skipped" and env.store.get("CA-1").status == DELIVERED and env.store.get("CA-1").rounds

    env.jira.labels = ["ai-agent"]
    env.wf.on_trigger("CA-1")
    env.plans.append(PlanResult("", "## Plan\n1. new work"))
    plan_job(env)
    s = env.store.get("CA-1")
    assert s.round_no == 2 and s.status == AWAITING_REPLY


def test_slack_follow_ups_do_not_need_the_jira_label(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.jira.labels = []  # the label was taken off after delivery
    env.plans.append(PlanResult("", "## Plan\n1. more"))
    env.wf.on_user_message("D1", "", ME, "add logging", "8.6")
    assert plan_job(env).status == "succeeded" and env.store.get("CA-1").round_no == 2


def test_a_completed_ticket_that_is_retriggered_keeps_its_history(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "merged", "draft": False, "branch": "b"}
    assert env.wf.tick()["merged"] == ["CA-1"]
    assert env.store.get("CA-1").status == DONE
    assert env.wf.on_trigger("CA-1") is True  # a fresh session, seeded with the earlier rounds
    s = env.store.get("CA-1")
    assert s.status == PLANNING and s.round_no == 2 and s.rounds[0]["pr_url"] == PR_URL and s.pr_number == 0


def test_a_merged_pr_completes_the_ticket_and_later_messages_get_a_clear_answer(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    result_ts = env.slack.posts[-1]["ts"]
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "merged", "draft": False, "branch": "b"}
    assert env.wf.tick()["merged"] == ["CA-1"]
    assert "was merged" in env.slack.posts[-1]["text"] and any("merged" in c[1] for c in env.jira.comments)
    assert env.store.get("CA-1").status == DONE and "CA-1" not in env.store.active_keys()
    # under the old result message, or with the ticket key in front, you're told it is completed
    assert env.wf.on_user_message("D1", result_ts, ME, "also add logging") == "rejected:done"
    assert "is completed" in env.slack.posts[-1]["text"] and "new Jira ticket" in env.slack.posts[-1]["text"]
    assert env.wf.on_user_message("D1", "", ME, "CA-1: also add logging") == "completed"


def test_closed_pr_is_announced_once(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "closed", "draft": False, "branch": "b"}
    env.wf.tick()
    env.wf.tick()
    assert len([p for p in env.slack.posts if "closed without merging" in p["text"]]) == 1
    assert env.store.get("CA-1").status == DELIVERED


def review(id_, body="please rename this", path="a.py"):
    return {"id": id_, "author": "dana", "path": path, "line": 4, "hunk": "@@ -1 +1 @@\n-x\n+y", "body": body, "at": "2026-10-11T10:00:00Z"}


def test_new_review_comments_are_offered_once_and_can_start_a_round(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "open", "draft": True, "branch": "ai/CA-1-fix"}
    env.gh.comments[12] = [review("inline:1"), review("talk:2", "and add a test", "")]
    assert env.wf.tick()["reviews"] == ["CA-1"]
    ask = env.slack.posts[-1]
    assert "PR #12" in str(ask["blocks"]) and "Plan changes from these" in str(ask["blocks"]) and "please rename this" in str(ask["blocks"])
    assert env.wf.tick()["reviews"] == []  # not offered again while one is pending, nor after
    buttons = [b for blk in ask["blocks"] if blk["type"] == "actions" for b in blk["elements"]]
    assert env.wf.on_action(buttons[0]["action_id"], "CA-1", ME, "D1", ask["ts"]) == "followup"
    s = env.store.get("CA-1")
    assert (s.status, s.round_source, s.force_plan, s.review_pending) == (REVISING, "review", True, {})
    assert "please rename this" in s.messages[-1]["text"] and "and add a test" in s.messages[-1]["text"]
    assert env.wf.on_action(buttons[0]["action_id"], "CA-1", ME, "D1", ask["ts"]) == "ignored_review"  # a second click
    env.gh.comments[12].append(review("inline:3", "one more nit"))
    env.store.transition("CA-1", REVISING, DELIVERED)
    assert env.wf.tick()["reviews"] == ["CA-1"]  # only the new comment is offered
    assert "one more nit" in str(env.slack.posts[-1]["blocks"]) and "please rename this" not in str(env.slack.posts[-1]["blocks"])


def test_ignoring_review_comments_remembers_them(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    env.gh.prs[12] = {"number": 12, "url": PR_URL, "state": "open", "draft": True, "branch": "b"}
    env.gh.comments[12] = [review("inline:1")]
    env.wf.tick()
    ask = env.slack.posts[-1]
    buttons = [b for blk in ask["blocks"] if blk["type"] == "actions" for b in blk["elements"]]
    assert env.wf.on_action(buttons[1]["action_id"], "CA-1", ME, "D1", ask["ts"]) == "ignored_review"
    assert env.wf.tick()["reviews"] == [] and env.store.get("CA-1").status == DELIVERED


def test_a_description_edit_after_delivery_asks_before_planning(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    assert env.wf.on_description_changed("CA-1") == "asked"
    ask = env.slack.posts[-1]
    assert "description" in ask["text"] and "Plan a follow-up" in str(ask["blocks"])
    assert env.wf.on_description_changed("CA-1") == "ignored"  # not asked again straight away
    buttons = [b for blk in ask["blocks"] if blk["type"] == "actions" for b in blk["elements"]]
    env.submitted.clear()
    assert env.wf.on_action(buttons[0]["action_id"], "CA-1", ME, "D1", ask["ts"]) == "followup"
    assert env.submitted == [("plan", "CA-1")] and env.store.get("CA-1").force_plan
    assert env.wf.on_description_changed("CA-9") == "ignored"  # unknown ticket


def test_delivered_tickets_expire_after_the_follow_up_window(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    env.store.update("CA-1", lambda x: setattr(x, "last_activity_at", x.last_activity_at - 15 * 86400))
    assert env.wf.tick()["expired"] == ["CA-1"]
    s = env.store.get("CA-1")
    assert s.status == DONE and "14 days" in s.result


def test_tickets_list_shows_delivered_tickets(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    delivered(env, monkeypatch)
    env.wf.on_user_message("D1", "", ME, "tickets")
    assert "delivered (round 1): PR #12" in str(env.slack.posts[-1]["blocks"])


# ---- errors are communicated, with fallbacks -----------------------------------------------------


def test_slack_failures_are_retried_then_sent_as_plain_text_then_written_to_jira(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    s = env.store.get("CA-1")
    real_post, attempts = env.slack.post, []

    def flaky(text, *, channel="", blocks=None, thread_ts=""):
        attempts.append(bool(blocks))
        if len(attempts) < 3:
            raise RuntimeError("slack 500")
        return real_post(text, channel=channel, blocks=blocks, thread_ts=thread_ts)

    env.slack.post = flaky
    assert env.wf._say(s, "hello", [{"type": "divider"}], important=True) is not None and attempts == [True, True, True]

    def blocked(text, *, channel="", blocks=None, thread_ts=""):
        if blocks:
            raise RuntimeError("invalid_blocks")
        return real_post(text, channel=channel, blocks=None, thread_ts=thread_ts)

    env.slack.post = blocked
    assert env.wf._say(s, "plain fallback", [{"type": "divider"}]) is not None and env.slack.posts[-1]["text"] == "plain fallback"

    def down(*a, **k):
        raise RuntimeError("slack is down")

    env.slack.post = down
    assert env.wf._say(s, "*CA-1* result: `PR`", important=True) is None
    assert "couldn't reach Slack" in env.jira.comments[-1][1] and "result: PR" in env.jira.comments[-1][1]


def test_failed_jira_comments_are_queued_announced_once_and_retried(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    env.jira.down = True
    env.slack.posts.clear()
    env.wf._jira_comment("CA-1", "first")
    env.wf._jira_comment("CA-1", "second")
    assert len([p for p in env.slack.posts if "couldn't comment on the Jira ticket" in p["text"]]) == 2  # once per fresh failure
    env.wf.tick()  # still down: they go back in the queue
    env.jira.down = False
    env.jira.comments.clear()
    env.wf.tick()
    assert [c[1] for c in env.jira.comments] == ["first", "second"]
    env.jira.comments.clear()
    env.wf.tick()
    assert env.jira.comments == []  # nothing left to retry


def test_stale_looking_into_it_messages_are_closed_on_tick(env):
    env.wf.on_trigger("CA-1")
    plan_job(env)
    env.wf.on_user_message("D1", "", ME, "hello", "8.9")
    ack = env.slack.posts[-1]
    env.store.update("CA-1", lambda x: [a.update(at=a["at"] - 7200) for a in x.pending_acks])
    env.submitted.clear()
    assert env.wf.tick()["stale"] == ["CA-1"]
    assert any(u["ts"] == ack["ts"] and "Lost track" in u["text"] for u in env.slack.updates)
    assert env.store.get("CA-1").pending_acks == []


def test_a_start_that_failed_is_retried_on_the_next_tick(env):
    env.wf.defer_trigger("CA-7")
    assert env.wf.tick()["retried"] == ["CA-7"] and ("plan", "CA-7") in env.submitted
    assert env.wf.deferred_triggers == set()


def test_a_planning_error_goes_where_you_wrote(env):
    env.plans[:] = [PlanResult("", "## Plan\n1. x", files=["a.py"])]
    env.wf.on_trigger("CA-1")
    plan_job(env)
    root = env.store.get("CA-1").slack_thread_ts
    env.plans[:] = [RuntimeError("boom")]
    env.wf.on_user_message("D1", root, ME, "in the thread", "9.1")  # written inside the plan's thread
    plan_job(env)
    err = env.slack.posts[-1]
    assert "couldn't produce the plan" in err["text"] and err["thread_ts"] == root

    env.plans[:] = [RuntimeError("boom again")]
    env.wf.on_user_message("D1", "", ME, "in the main chat", "9.2")
    plan_job(env)
    err = env.slack.posts[-1]
    assert "couldn't produce the plan" in err["text"] and err["thread_ts"] == ""


def test_planned_deletions_are_shown_confirmed_run_first_and_reported(env, monkeypatch):
    env.plans[:] = [PlanResult("", "## Plan\n1. Remove the file.\n\n## Delete\n- `old.bin`\n\n## Files\n- `a.py`", files=["a.py"], delete_files=["old.bin"])]
    RecordingAgent.calls = []
    env.wf.on_trigger("CA-1")
    plan_job(env, "CA-1")
    s = env.store.get("CA-1")
    plan_msg = [p for p in env.slack.posts if "Plan for" in p["text"]][-1]
    assert s.delete_files == ["old.bin"] and "old.bin" in str(plan_msg["blocks"])
    ts = s.plan_msg_ts
    env.wf.on_action(ACTION_PROCEED, "CA-1", ME, "D1", ts)
    assert "These files will be deleted first" in str(env.slack.updates[-1]["blocks"]) and "old.bin" in str(env.slack.updates[-1]["blocks"])
    env.wf.on_action(ACTION_CONFIRM, "CA-1", ME, "D1", ts)
    RecordingAgent.delivery = {**RecordingAgent.delivery, "deleted": ["old.bin"]}
    try:
        run_execute(env, monkeypatch)
    finally:
        RecordingAgent.delivery.pop("deleted")
    assert RecordingAgent.calls[-1].deletions == ["old.bin"]
    s = env.store.get("CA-1")
    assert s.rounds[-1]["deleted"] == ["old.bin"]
    assert "Deleted: `old.bin`" in env.slack.posts[-1]["text"]


def test_an_answer_only_reply_keeps_the_planned_deletions(env):
    env.plans[:] = [PlanResult("", "## Plan\n1. x\n\n## Delete\n- `old.bin`", files=["a.py"], delete_files=["old.bin"])]
    env.wf.on_trigger("CA-1")
    plan_job(env, "CA-1")
    env.plans.append(PlanResult("", "", reply="Yes, only that file.", changed=False, delete_files=["old.bin"]))
    env.wf.on_user_message("D1", "", ME, "what gets deleted?", "10.1")
    plan_job(env)
    assert env.store.get("CA-1").delete_files == ["old.bin"]
