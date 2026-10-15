"""HTTP layer: Slack signatures, event filtering, button payloads, /tick auth."""
import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

SIGNING = "slack-signing-secret"
ME = "U_ME"


class FakeStore:
    def __init__(self):
        self.seen = set()

    def first_seen(self, event_id):
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True


class FakeWorkflow:
    def __init__(self):
        self.store = FakeStore()
        self.messages, self.actions, self.started = [], [], []

    def on_user_message(self, channel, thread_ts, user, text, message_ts=""):
        self.messages.append((channel, thread_ts, user, text))
        self.last_ts = message_ts
        return "ok"

    def on_action(self, action_id, key, user, channel, ts):
        self.actions.append((action_id, key, user, channel, ts))
        return "ok"

    def on_trigger(self, key):
        self.started.append(key)
        return True

    def tick(self):
        return {"checked": 0, "requeued": [], "reminded": []}


@pytest.fixture
def api(monkeypatch):
    for k, v in {
        "JIRA_BASE_URL": "https://example.atlassian.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "x",
        "GITHUB_TOKEN": "x", "GITHUB_REPO": "o/r", "WEBHOOK_SECRET": "w", "TRIGGER_LABEL": "ai-agent",
        "SLACK_BOT_TOKEN": "xoxb-1", "SLACK_SIGNING_SECRET": SIGNING, "SLACK_USER_ID": ME,
        "REDIS_URL": "redis://fake", "TICK_TOKEN": "tick-secret",
    }.items():
        monkeypatch.setenv(k, v)
    from app import config, main

    config.get_settings.cache_clear()
    wf = FakeWorkflow()
    monkeypatch.setattr(main, "_wf", wf)
    c = TestClient(main.app)
    c.wf = wf
    return c


def signed(body: bytes, secret=SIGNING, ts=None):
    ts = str(ts or int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def dm_event(event_id="Ev1", **over):
    ev = {"type": "message", "channel_type": "im", "channel": "D1", "user": ME, "text": "hello", "thread_ts": "1.1", "ts": "5.5"}
    ev.update(over)
    return {"type": "event_callback", "event_id": event_id, "event": ev}


def post_event(api, payload, **kw):
    body = json.dumps(payload).encode()
    return api.post("/slack/events", content=body, headers={"Content-Type": "application/json", **signed(body, **kw)})


def test_url_verification_challenge(api):
    r = post_event(api, {"type": "url_verification", "challenge": "abc123"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_bad_or_stale_signature_is_rejected(api):
    assert post_event(api, dm_event(), secret="wrong").status_code == 401
    assert post_event(api, dm_event(), ts=int(time.time()) - 4000).status_code == 401
    assert api.post("/slack/events", json=dm_event()).status_code == 401
    assert api.wf.messages == []


def test_dm_message_reaches_workflow(api):
    assert post_event(api, dm_event()).status_code == 200
    assert api.wf.messages == [("D1", "1.1", ME, "hello")] and api.wf.last_ts == "5.5"


def test_slack_redelivery_is_ignored(api):
    post_event(api, dm_event("EvX"))
    r = post_event(api, dm_event("EvX"))
    assert r.json().get("duplicate") is True
    assert len(api.wf.messages) == 1


@pytest.mark.parametrize("over", [{"bot_id": "B1"}, {"subtype": "message_changed"}, {"channel_type": "channel"}])
def test_bot_edits_and_non_dm_messages_are_ignored(api, over):
    post_event(api, dm_event(**over))
    assert api.wf.messages == []


def test_button_click_reaches_workflow(api):
    payload = {
        "type": "block_actions",
        "user": {"id": ME},
        "channel": {"id": "D1"},
        "message": {"ts": "2.2"},
        "actions": [{"action_id": "plan_proceed", "value": "CA-1"}],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    r = api.post("/slack/interactive", content=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded", **signed(body)})
    assert r.status_code == 200
    assert api.wf.actions == [("plan_proceed", "CA-1", ME, "D1", "2.2")]

    bad = api.post("/slack/interactive", content=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded", **signed(body, secret="no")})
    assert bad.status_code == 401 and len(api.wf.actions) == 1


def test_tick_requires_token(api):
    assert api.get("/tick").status_code == 401
    assert api.get("/tick?token=wrong").status_code == 401
    r = api.get("/tick?token=tick-secret")
    assert r.status_code == 200 and r.json()["planning_enabled"] is True
    assert api.get("/tick", headers={"Authorization": "Bearer tick-secret"}).status_code == 200


def test_tick_reports_503_when_redis_is_down(api):
    def boom():
        raise ConnectionError("redis down")

    api.wf.tick = boom
    r = api.get("/tick?token=tick-secret")
    assert r.status_code == 503 and "ConnectionError" in r.json()["detail"]


def test_jira_webhook_starts_planning_instead_of_coding(api):
    body = json.dumps(
        {"webhookEvent": "jira:issue_created", "issue": {"key": "CA-9", "fields": {"labels": ["ai-agent"]}}}
    ).encode()
    sig = hmac.new(b"w", body, hashlib.sha256).hexdigest()
    r = api.post("/jira-webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature": f"sha256={sig}"})
    assert r.status_code == 202 and r.json() == {"accepted": True, "issue": "CA-9", "mode": "plan"}
    assert api.wf.started == ["CA-9"]


def test_endpoints_refuse_when_plan_mode_is_not_configured(api, monkeypatch):
    from app import config

    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    config.get_settings.cache_clear()
    assert post_event(api, {"type": "url_verification", "challenge": "x"}).status_code == 503
    assert api.get("/tick?token=tick-secret").json() == {"planning_enabled": False}


def test_run_job_dispatches_by_kind(monkeypatch):
    from app import main
    from app.jobs import Job

    calls = []

    class W:
        def run_plan_job(self, job): calls.append(("plan", job.issue_key)); job.finish("succeeded")
        def run_execute_job(self, job): calls.append(("execute", job.issue_key)); job.finish("succeeded")
        def resume_if_needed(self, key): calls.append(("resume", key))

    monkeypatch.setattr(main, "_wf", W())
    monkeypatch.setattr(main, "_set_queue_label", lambda k, present: calls.append(("label", k)))
    for kind in ("plan", "execute"):
        main._in_flight.add("CA-1")
        main._run_job(Job("CA-1", "e", kind=kind))
    assert calls == [("plan", "CA-1"), ("resume", "CA-1"), ("execute", "CA-1")]  # no queue labels in plan mode
    assert "CA-1" not in main._in_flight


def test_a_crashing_workflow_job_is_recorded_not_raised(monkeypatch):
    from app import main
    from app.jobs import Job

    class W:
        def run_plan_job(self, job): raise ConnectionError("redis down")
        def resume_if_needed(self, key): raise ConnectionError("redis down")

    monkeypatch.setattr(main, "_wf", W())
    job = Job("CA-2", "e", kind="plan")
    main._in_flight.add("CA-2")
    main._run_job(job)
    assert job.status == "failed" and "redis down" in job.detail
    assert "CA-2" not in main._in_flight


def test_plan_jobs_use_their_own_queue_so_coding_never_blocks_replies(monkeypatch):
    from app import main

    plan_q, code_q = [], []
    monkeypatch.setattr(main._plan_executor, "submit", lambda fn, job: plan_q.append(job.issue_key))
    monkeypatch.setattr(main._executor, "submit", lambda fn, job: code_q.append((job.kind, job.issue_key)))
    main._in_flight.clear()
    assert main._submit("plan", "CA-1") and main._submit("execute", "CA-2") and main._enqueue("CA-3", "jira:issue_created")
    assert plan_q == ["CA-1"]
    assert code_q == [("execute", "CA-2"), ("code", "CA-3")]
    assert not main._submit("plan", "CA-1")  # one job per ticket at a time, whichever queue
    main._in_flight.clear()


def test_plan_jobs_share_the_coding_queue_when_configured(monkeypatch):
    from app import config, main

    monkeypatch.setenv("SEPARATE_PLAN_QUEUE", "false")
    config.get_settings.cache_clear()
    plan_q, code_q = [], []
    monkeypatch.setattr(main._plan_executor, "submit", lambda fn, job: plan_q.append(job.issue_key))
    monkeypatch.setattr(main._executor, "submit", lambda fn, job: code_q.append(job.issue_key))
    main._in_flight.clear()
    main._submit("plan", "CA-1")
    assert plan_q == [] and code_q == ["CA-1"]
    main._in_flight.clear()


def test_health_and_startup_checks_name_misconfigurations(api, monkeypatch):
    from app import config

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    config.get_settings.cache_clear()
    h = api.get("/health").json()
    assert h["plan_mode"] is True
    assert any("SLACK_SIGNING_SECRET" in p for p in h["problems"])
    assert "xoxb" not in json.dumps(h)  # names only, never values
    s = config.get_settings()
    assert s.config_problems() == h["problems"]

    monkeypatch.setenv("REDIS_URL", "")
    config.get_settings.cache_clear()
    assert any("OFF because REDIS_URL" in p for p in config.get_settings().config_problems())


def test_runs_endpoint_needs_the_secret_and_returns_the_log(api):
    api.wf.store.get_runs = lambda key: [{"step": "plan", "output": "x"}]
    assert api.get("/runs/CA-1").status_code == 401
    assert api.get("/runs/CA-1?token=nope").status_code == 401
    r = api.get("/runs/CA-1?token=w")
    assert r.status_code == 200 and r.json() == {"issue": "CA-1", "runs": [{"step": "plan", "output": "x"}]}


def test_planning_slots_follow_plan_workers(monkeypatch):
    from app import config, main

    monkeypatch.setenv("PLAN_WORKERS", "2")
    config.get_settings.cache_clear()
    monkeypatch.setattr(main, "_plan_gate", None)
    gate = main._plan_slot()
    assert gate.acquire(blocking=False) and gate.acquire(blocking=False) and not gate.acquire(blocking=False)
    monkeypatch.setattr(main, "_plan_gate", None)
    monkeypatch.setenv("PLAN_WORKERS", "99")
    config.get_settings.cache_clear()
    gate = main._plan_slot()
    assert sum(gate.acquire(blocking=False) for _ in range(10)) == 4  # capped at the thread count


def test_the_watchdog_runs_with_the_app(monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from app import config, main

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_USER_ID", "U1")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("WATCHDOG_SECONDS", "1")
    config.get_settings.cache_clear()
    runs = threading.Event()

    class FakeWorkflow:
        def watchdog(self):
            runs.set()
            return {"requeued": [], "slack_sent": 0}

        def tick(self):
            return {}

    monkeypatch.setattr(main, "_workflow", lambda: FakeWorkflow())
    monkeypatch.setattr(main, "_startup_checks", lambda: None)
    with TestClient(main.app):
        assert any(t.name == "watchdog" for t in threading.enumerate())
        assert runs.wait(5)
    config.get_settings.cache_clear()


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share"])
def test_a_thread_reply_also_sent_to_the_chat_and_a_message_with_a_file_still_count(api, subtype):
    post_event(api, dm_event(subtype=subtype, text="also handle mobile"))
    assert api.wf.messages == [("D1", "1.1", ME, "also handle mobile")]


def test_an_attached_file_gets_a_note_that_only_text_is_read(api, monkeypatch):
    from app import main
    told = []
    monkeypatch.setattr(main, "_tell_user", lambda channel, text, thread_ts="": told.append((channel, text, thread_ts)))
    post_event(api, dm_event("EvF1", subtype="file_share", files=[{"name": "error.png"}], text=""))
    assert api.wf.messages == [] and "can't open attached files" in told[0][1] and told[0][2] == "1.1"
    post_event(api, dm_event("EvF2", subtype="file_share", files=[{"name": "error.png"}], text="see the error"))
    assert api.wf.messages == [("D1", "1.1", ME, "see the error")] and len(told) == 2
