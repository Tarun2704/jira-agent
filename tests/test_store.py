import os

import fakeredis
import pytest

from app.store import AWAITING_REPLY, CONFIRMING, DONE, EXECUTING, PLANNING, REVISING, SessionStore


@pytest.fixture
def st():
    # Set TEST_REDIS_URL (e.g. redis://localhost:6379/15) to run these against a real Redis.
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        yield SessionStore(fakeredis.FakeRedis(decode_responses=True))
        return
    store = SessionStore.from_url(url)
    store.r.flushdb()
    yield store
    store.r.flushdb()


def test_create_and_get_roundtrip(st):
    s = st.create("CA-1")
    assert s.status == PLANNING
    got = st.get("CA-1")
    assert got.issue_key == "CA-1" and got.messages == []
    assert st.active_keys() == ["CA-1"]


def test_create_refused_while_active_but_allowed_after_terminal(st):
    assert st.create("CA-1")
    assert st.create("CA-1") is None  # a plan is already in progress
    st.transition("CA-1", PLANNING, DONE)
    assert st.active_keys() == []
    again = st.create("CA-1")  # label re-added after the run ended: fresh session
    assert again and again.status == PLANNING and again.plan == ""


def test_transition_only_from_allowed_states(st):
    st.create("CA-1")
    assert st.transition("CA-1", AWAITING_REPLY, CONFIRMING) is None  # wrong state: nothing changes
    assert st.get("CA-1").status == PLANNING
    moved = st.transition("CA-1", {PLANNING, REVISING}, AWAITING_REPLY, plan="p")
    assert moved.status == AWAITING_REPLY and st.get("CA-1").plan == "p"


def test_double_click_only_wins_once(st):
    st.create("CA-1")
    st.transition("CA-1", PLANNING, CONFIRMING)
    assert st.transition("CA-1", CONFIRMING, EXECUTING)
    assert st.transition("CA-1", CONFIRMING, EXECUTING) is None  # second click is a no-op


def test_update_can_decline_to_write(st):
    st.create("CA-1")
    s = st.update("CA-1", lambda x: False)
    assert s.status == PLANNING
    assert st.update("NOPE-1", lambda x: None) is None


def test_terminal_sessions_expire_and_leave_active_set(st):
    st.create("CA-1")
    st.transition("CA-1", PLANNING, DONE)
    assert 0 < st.r.ttl("jiraagent:session:CA-1") <= 30 * 24 * 3600


def test_lease_lifecycle(st):
    assert st.acquire_lease("CA-1", "a", 60)
    assert not st.acquire_lease("CA-1", "b", 60)  # taken
    assert st.has_lease("CA-1")
    assert not st.refresh_lease("CA-1", "b", 60)  # not the owner
    assert st.refresh_lease("CA-1", "a", 60)
    st.release_lease("CA-1", "b")  # not the owner: ignored
    assert st.has_lease("CA-1")
    st.release_lease("CA-1", "a")
    assert not st.has_lease("CA-1")


def test_thread_mapping_and_event_dedupe(st):
    st.map_thread("D1", "1.1", "CA-1")
    assert st.key_for_thread("D1", "1.1") == "CA-1"
    assert st.key_for_thread("D1", "9.9") is None
    assert st.first_seen("Ev1") is True
    assert st.first_seen("Ev1") is False


def test_run_log_keeps_the_newest_runs_only(st):
    store = st
    from app.store import MAX_RUNS_PER_TICKET

    store.append_runs("CA-1", [{"n": i} for i in range(MAX_RUNS_PER_TICKET + 5)])
    runs = store.get_runs("CA-1")
    assert len(runs) == MAX_RUNS_PER_TICKET and runs[-1]["n"] == MAX_RUNS_PER_TICKET + 4
    assert store.get_runs("CA-2") == [] and store.append_runs("CA-2", []) is None
