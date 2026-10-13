"""Self-ping: the app calls its own public /tick, so ticks run and a free Render service stays awake."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import Settings
import httpx
from app.selfping import SelfPinger, explain_failure, tick_url


def settings(**kw) -> Settings:
    base = dict(
        jira_base_url="https://x.atlassian.net", jira_email="a@b.c", jira_api_token="jt",
        github_token="gh", github_repo="o/r", webhook_secret="w",
        slack_bot_token="xoxb", slack_user_id="U1", redis_url="redis://fake", tick_token="tk en",
        render_external_url="https://my-app.onrender.com",
    )
    base.update(kw)
    return Settings(**base)


class Resp:
    def __init__(self, code):
        self.status_code = code


def test_tick_url_has_no_token_and_trims_slashes():
    assert tick_url("https://a.onrender.com/") == "https://a.onrender.com/tick"


def test_ping_once_success_failure_and_exception():
    seen = []
    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: seen.append((url, timeout, headers)) or Resp(200))
    assert p.ping_once() == 200 and p.failures == 0
    assert seen[0][0] == "https://a.onrender.com/tick" and seen[0][2] == {"Authorization": "Bearer t"}  # token in a header, not the URL
    p._get = lambda url, timeout, headers: Resp(401)
    assert p.ping_once() == 401 and p.failures == 1
    p._get = lambda url, timeout, headers: (_ for _ in ()).throw(ConnectionError("down"))
    assert p.ping_once() is None and p.failures == 2 and p.last_status is None  # never raises


def test_the_token_never_reaches_the_log(caplog):
    caplog.set_level("INFO")
    p = SelfPinger("https://a.onrender.com", "supersecret", 600, get=lambda url, timeout, headers: (_ for _ in ()).throw(ConnectionError(url)))
    p.ping_once()
    p._get = lambda url, timeout, headers: Resp(401)
    p.ping_once()
    assert "supersecret" not in caplog.text


def test_a_slow_tick_is_not_a_failure():
    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    assert p.ping_once() is None and p.failures == 0


def test_alert_once_after_repeated_failures_and_once_on_recovery():
    told = []
    status = {"v": 401}
    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: Resp(status["v"]), alert_after=3, notify=told.append)
    p.ping_once(); p.ping_once()
    assert told == []
    p.ping_once()
    assert len(told) == 1 and "TICK_TOKEN" in told[0] and "3 times" in told[0]
    p.ping_once(); p.ping_once()
    assert len(told) == 1  # one message per bad streak, not one per ping
    status["v"] = 200
    p.ping_once()
    assert len(told) == 2 and "running again" in told[1] and p.failures == 0
    status["v"] = 401
    for _ in range(3):
        p.ping_once()
    assert len(told) == 3  # a new streak alerts again


def test_a_failing_alert_never_breaks_the_loop():
    def boom(text):
        raise RuntimeError("slack down")

    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: Resp(500), alert_after=1, notify=boom)
    assert p.ping_once() == 500  # no exception


def test_failure_explanations_are_plain():
    assert "TICK_TOKEN" in explain_failure(401) and "no /tick" in explain_failure(404)
    assert "could not reach" in explain_failure(None) and "Redis" in explain_failure(503)


def test_interval_has_a_floor():
    assert SelfPinger("https://a", "t", 1).interval == 30


def test_loop_pings_repeatedly_until_stopped(monkeypatch):
    calls = []
    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: calls.append(1) or Resp(200))
    waits = iter([False, False, True])  # two intervals pass, then stop
    monkeypatch.setattr(p._stop, "wait", lambda interval: next(waits))
    p.run()
    assert len(calls) == 2


def test_start_and_stop_a_real_thread():
    p = SelfPinger("https://a.onrender.com", "t", 600, get=lambda url, timeout, headers: Resp(200))
    t = p.start()
    assert t.is_alive()
    p.stop()
    t.join(timeout=2)
    assert not t.is_alive()


# ---- when it is on ---------------------------------------------------------------------

def test_self_ping_base_uses_render_url_or_override():
    assert settings().self_ping_base == "https://my-app.onrender.com"
    assert settings(self_ping_url="https://custom.example.com").self_ping_base == "https://custom.example.com"


def test_self_ping_is_off_without_url_token_plan_mode_or_when_disabled():
    assert settings(render_external_url="").self_ping_base == ""  # e.g. your own machine
    assert settings(tick_token="").self_ping_base == ""
    assert settings(redis_url="").self_ping_base == ""
    assert settings(self_ping_enabled=False).self_ping_base == ""


# ---- /health and /tick --------------------------------------------------------------------

def test_health_reports_last_tick_and_overdue(monkeypatch):
    s = settings()
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "_last_tick", {"at": 0.0, "source": ""})
    c = TestClient(main_mod.app)
    h = c.get("/health").json()["tick"]
    assert h["last_at"] is None and h["overdue"] is True and h["self_ping"] is True
    main_mod._note_tick("request")
    h = c.get("/health").json()["tick"]
    assert h["source"] == "request" and h["age_seconds"] < 5 and h["overdue"] is False
    main_mod._last_tick["at"] = time.time() - 3 * s.self_ping_interval_seconds
    assert c.get("/health").json()["tick"]["overdue"] is True


def test_tick_endpoint_records_the_time(monkeypatch):
    s = settings()

    class FakeWf:
        def tick(self):
            return {"checked": 0}

    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "_workflow", lambda: FakeWf())
    monkeypatch.setattr(main_mod, "_last_tick", {"at": 0.0, "source": ""})
    c = TestClient(main_mod.app)
    assert c.get("/tick?token=wrong").status_code == 401 and main_mod._last_tick["at"] == 0.0
    assert c.get("/tick?token=tk en").status_code == 200
    assert main_mod._last_tick["source"] == "request" and main_mod._last_tick["at"] > 0


def test_health_has_no_tick_block_without_plan_mode(monkeypatch):
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings(redis_url=""))
    assert "tick" not in TestClient(main_mod.app).get("/health").json()


def test_app_starts_and_stops_the_pinger(monkeypatch):
    events = []

    class FakePinger:
        interval = 600

        def __init__(self, base, token, interval, **kw):
            events.append(("init", base, token, interval))

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(main_mod, "SelfPinger", FakePinger)
    monkeypatch.setattr(main_mod, "_startup_tick", lambda: None)
    with TestClient(main_mod.app):
        assert events == [("init", "https://my-app.onrender.com", "tk en", 600), "start"]
    assert events[-1] == "stop"


def test_app_does_not_start_the_pinger_locally(monkeypatch):
    started = []
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings(render_external_url=""))
    monkeypatch.setattr(main_mod, "SelfPinger", lambda *a, **k: started.append(1))
    monkeypatch.setattr(main_mod, "_startup_tick", lambda: None)
    with TestClient(main_mod.app):
        pass
    assert not started


# ---- tick lock ----------------------------------------------------------------------------------

import fakeredis
import app.workflow as wf_mod
from app.store import SessionStore
from tests.test_workflow import env  # noqa: F401,E402 (fixture)


def test_second_tick_is_skipped_while_one_runs_here(env):
    started, release, results = threading.Event(), threading.Event(), []
    real = env.wf._tick_body

    def slow():
        started.set()
        release.wait(5)
        return real()

    env.wf._tick_body = slow
    t = threading.Thread(target=lambda: results.append(env.wf.tick()))
    t.start()
    assert started.wait(5)
    assert env.wf.tick() == {"skipped": "a tick is already running"}
    release.set()
    t.join(5)
    assert "checked" in results[0]
    assert "checked" in env.wf.tick()  # free again afterwards


def test_tick_is_skipped_while_another_instance_holds_the_redis_lock(env):
    env.store.acquire_tick_lock("other-instance", 300)
    assert env.wf.tick() == {"skipped": "a tick is already running on another instance"}
    env.store.release_tick_lock("other-instance")
    assert "checked" in env.wf.tick()


def test_tick_releases_the_lock_even_when_it_fails(env, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(env.wf, "_tick_body", broken)
    with pytest.raises(RuntimeError):
        env.wf.tick()
    assert env.store.acquire_tick_lock("next", 300)  # the lock was released
    env.store.release_tick_lock("next")
    assert env.wf._tick_guard.acquire(blocking=False)  # and so was the in-process guard
    env.wf._tick_guard.release()


def test_release_only_removes_your_own_lock(env):
    env.store.acquire_tick_lock("a", 300)
    env.store.release_tick_lock("b")
    assert not env.store.acquire_tick_lock("c", 300)
    env.store.release_tick_lock("a")
    assert env.store.acquire_tick_lock("c", 300)


def test_tick_endpoint_reports_a_skip(monkeypatch):
    s = settings()

    class FakeWf:
        def tick(self):
            return {"skipped": "a tick is already running"}

    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "_workflow", lambda: FakeWf())
    r = TestClient(main_mod.app).get("/tick", headers={"Authorization": "Bearer tk en"})  # header auth works too
    assert r.status_code == 200 and r.json()["skipped"] == "a tick is already running"
