"""Cost tracking: per-call records on the job, per-round entries in Redis, totals, Slack line, /usage."""
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.costs import call_cost, make_entry, money, parse_prices, summarize
from app.jobs import Job
from app.store import SessionStore
from tests.test_workflow import PR_URL, RecordingAgent, approve, env, plan_job  # noqa: F401 (env fixture)
import app.workflow as wf_mod
from app.planner import PlanResult

PRICES = parse_prices('{"m-main": [1.0, 4.0], "m-free": [0, 0]}')


def test_parse_prices_is_forgiving():
    assert PRICES == {"m-main": (1.0, 4.0), "m-free": (0.0, 0.0)}
    assert parse_prices("") == {} and parse_prices("not json") == {} and parse_prices('{"m": 5}') == {}


def test_job_records_each_call():
    job = Job("CA-1", "x")
    job.add_tokens(1000, 200, model="m-main", step="plan")
    job.add_tokens(0, 0, model="m-free", step="code")
    assert job.tokens == {"sent": 1000, "received": 200}
    assert [c["step"] for c in job.calls] == ["plan", "code"]
    assert job.to_dict()["calls"] == 2  # /jobs shows the count only


def test_call_cost_uses_the_price_table():
    assert call_cost({"model": "m-main", "sent": 1_000_000, "received": 500_000}, PRICES) == pytest.approx(3.0)
    assert call_cost({"model": "m-free", "sent": 5, "received": 5}, PRICES) == 0
    assert call_cost({"model": "other", "sent": 5, "received": 5}, PRICES) is None


def test_entry_cost_is_unknown_if_any_model_is_unpriced():
    calls = [{"step": "plan", "model": "m-main", "sent": 1_000_000, "received": 0},
             {"step": "code", "model": "mystery", "sent": 10, "received": 10}]
    e = make_entry("j1", "plan", 1, calls, PRICES)
    assert e["cost_usd"] is None and e["requests"] == 2 and e["sent"] == 1_000_010
    known = make_entry("j1", "plan", 1, calls[:1], PRICES)
    assert known["cost_usd"] == pytest.approx(1.0)


def test_summarize_groups_by_round_and_totals():
    entries = [
        make_entry("a", "plan", 1, [{"step": "plan", "model": "m-main", "sent": 1_000_000, "received": 0}], PRICES),
        make_entry("b", "execute", 1, [{"step": "code", "model": "m-main", "sent": 0, "received": 250_000}], PRICES),
        make_entry("c", "plan", 2, [{"step": "plan", "model": "m-main", "sent": 500_000, "received": 0}], PRICES),
    ]
    s = summarize(entries)
    assert [r["round"] for r in s["rounds"]] == [1, 2]
    assert s["rounds"][0]["cost_usd"] == pytest.approx(2.0) and s["rounds"][0]["requests"] == 2
    assert s["rounds"][1]["cost_usd"] == pytest.approx(0.5)
    assert s["total"]["cost_usd"] == pytest.approx(2.5) and s["total"]["requests"] == 3
    assert summarize([])["total"]["cost_usd"] is None


def test_money_format():
    assert money(None) == "unknown" and money(0.0004) == "$0.0004" and money(1.234) == "$1.23"


# ---- through the workflow -------------------------------------------------------------------

class CostAgent(RecordingAgent):
    def handle_issue(self, key):
        self.job.add_tokens(1_000_000, 250_000, model="m-main", step="code")
        super().handle_issue(key)


def test_plan_and_execute_jobs_save_cost_per_round(env, monkeypatch):
    env.settings.model_prices = '{"m-main": [1.0, 4.0]}'
    env.plans[:] = [PlanResult("", "## Plan\n1. fix `a.py`", files=["a.py"])]
    env.wf.on_trigger("CA-1")
    job = Job("CA-1", "x", kind="plan")
    job.start()
    job.add_tokens(500_000, 0, model="m-main", step="plan")  # what the planner would have recorded
    env.wf.run_plan_job(job)
    approve(env)
    monkeypatch.setattr(wf_mod, "CodingAgent", CostAgent)
    ex = Job("CA-1", "x", kind="execute")
    ex.start()
    env.wf.run_execute_job(ex)

    costs = env.store.get_costs("CA-1")
    assert [(c["kind"], c["round"]) for c in costs] == [("plan", 1), ("execute", 1)]
    s = summarize(costs)
    assert s["total"]["cost_usd"] == pytest.approx(0.5 + 2.0)
    # the delivery message carries a short cost line
    assert "Cost this round: about $2.50" in env.slack.posts[-1]["text"]


def test_cost_line_can_be_turned_off_and_unknown_cost_shows_tokens(env, monkeypatch):
    env.wf.store.append_cost("CA-9", make_entry("j", "execute", 1, [{"step": "code", "model": "x", "sent": 12_000, "received": 3_000}], {}))
    assert env.wf._cost_line("CA-9", 1) == "_AI use this round: 1 requests, 15k tokens._"
    env.settings.show_cost_in_slack = False
    assert env.wf._cost_line("CA-9", 1) == ""


def test_saving_cost_never_breaks_a_job(env, monkeypatch):
    job = Job("CA-1", "x")
    job.add_tokens(1, 1, model="m", step="plan")
    monkeypatch.setattr(env.store, "append_cost", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down")))
    env.wf._save_cost(job, "plan")  # must not raise


def test_costs_survive_a_new_session_for_the_same_ticket(env):
    env.store.append_cost("CA-2", make_entry("j", "plan", 1, [{"step": "plan", "model": "x", "sent": 1, "received": 1}], {}))
    env.store.create("CA-2")
    assert len(env.store.get_costs("CA-2")) == 1


# ---- /usage endpoint ----------------------------------------------------------------------

def test_usage_endpoint_needs_the_token_and_returns_rounds(env, monkeypatch):
    env.store.append_cost("CA-3", make_entry("j", "plan", 1, [{"step": "plan", "model": "m-main", "sent": 1_000_000, "received": 0}], PRICES))
    monkeypatch.setattr(main_mod, "get_settings", lambda: env.settings)
    monkeypatch.setattr(main_mod, "_workflow", lambda: env.wf)
    client = TestClient(main_mod.app)
    assert client.get("/usage/CA-3").status_code == 401
    r = client.get("/usage/CA-3?token=w")
    assert r.status_code == 200
    body = r.json()
    assert body["issue"] == "CA-3" and body["rounds"][0]["round"] == 1 and body["total"]["requests"] == 1
