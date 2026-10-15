"""Cost tracking: per-call records on the job, per-round entries in Redis, totals, Slack line, /usage."""
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.costs import DEFAULT_PRICES, call_cost, make_entry, model_prices, money, parse_prices, summarize
from app.jobs import Job
from app.store import SessionStore
from tests.test_workflow import PR_URL, RecordingAgent, approve, env, plan_job  # noqa: F401 (env fixture)
import app.workflow as wf_mod
from app.planner import PlanResult

PRICES = parse_prices('{"m-main": [1.0, 4.0], "m-free": [0, 0]}')


def test_parse_prices_is_forgiving():
    assert PRICES == {"m-main": (1.0, 4.0), "m-free": (0.0, 0.0)}
    assert parse_prices("") == {} and parse_prices("not json") == {} and parse_prices('{"m": 5}') == {}
    assert parse_prices('{"m": [1, 2, 3]}') == {}  # two or four numbers only
    assert parse_prices('{"m": [1, 2, 3, 4]}') == {"m": (1.0, 2.0, 3.0, 4.0)}



def test_claude_prices_are_built_in_and_model_prices_overrides_them():
    prices = model_prices("")
    assert prices == DEFAULT_PRICES
    # both spellings of the same model: Aider/litellm and OpenCode
    assert prices["claude-opus-5-5"] == prices["anthropic/claude-opus-5-5"] == (4.0, 20.0, 5.0, 0.2)
    cost, source = call_cost({"model": "claude-opus-5-5", "sent": 1_000_000, "received": 100_000}, prices)
    assert source == "configured" and cost == pytest.approx(6.0)
    free = model_prices('{"claude-opus-5-5": [0, 0], "my/model": [1, 2]}')
    assert free["claude-opus-5-5"] == (0.0, 0.0) and free["my/model"] == (1.0, 2.0)
    assert free["anthropic/claude-opus-5-5"] == (4.0, 20.0, 5.0, 0.2)  # untouched entries stay
    assert model_prices("not json") == DEFAULT_PRICES


def test_cached_calls_without_cache_rates_use_the_reported_cost():
    # CA-11 via OpenCode: 89k of the prompt tokens were cache hits (about a tenth of the input price at Google)
    call = {"model": "m-main", "sent": 3909, "received": 74, "cache_hit": 6098, "cache_write": 0, "reported_cost": 0.001541}
    assert call_cost(call, PRICES) == (0.001541, "reported")
    assert call_cost(dict(call, cache_hit=0), PRICES)[1] == "configured"  # no cache use: the configured price as before
    four = parse_prices('{"m-main": [1.0, 4.0, 1.25, 0.1]}')
    assert call_cost(call, four)[1] == "configured"  # cache rates given: they win
    assert call_cost(dict(call, reported_cost=None), PRICES)[1] == "configured"  # nothing reported: best estimate

def test_job_records_each_call():
    job = Job("CA-1", "x")
    job.add_tokens(1000, 200, model="m-main", step="plan")
    job.add_tokens(0, 0, model="m-free", step="code")
    assert job.tokens == {"sent": 1000, "received": 200}
    assert [c["step"] for c in job.calls] == ["plan", "code"]
    assert job.to_dict()["calls"] == 2  # /jobs shows the count only


def test_call_cost_uses_the_price_table():
    cost, source = call_cost({"model": "m-main", "sent": 1_000_000, "received": 500_000}, PRICES)
    assert cost == pytest.approx(3.0) and source == "configured"
    assert call_cost({"model": "m-free", "sent": 5, "received": 5}, PRICES) == (0.0, "configured")
    assert call_cost({"model": "other", "sent": 5, "received": 5}, PRICES) == (None, None)


def test_reported_cost_is_used_when_no_price_is_configured():
    call = {"model": "gemini/gemini-2.5-flash", "sent": 7200, "received": 157, "reported_cost": 0.0021}
    assert call_cost(call, {}) == (0.0021, "reported")
    assert call_cost(call, PRICES) == (0.0021, "reported")  # model not in the table
    assert call_cost(dict(call, model="m-main"), PRICES)[1] == "configured"  # your own price wins


def test_a_call_with_no_usage_does_not_make_the_round_unknown():
    assert call_cost({"model": "x", "sent": 0, "received": 0}, {}) == (0.0, "no usage")
    calls = [{"step": "code", "model": "x", "sent": 0, "received": 0},
             {"step": "pr", "model": "x", "sent": 10, "received": 5, "reported_cost": 0.5}]
    assert make_entry("j", "execute", 1, calls, {})["cost_usd"] == pytest.approx(0.5)


def test_cache_tokens_are_priced():
    prices = parse_prices('{"c": [3.0, 15.0, 3.75, 0.30]}')
    call = {"model": "c", "sent": 2_000_000, "received": 100_000, "cache_write": 1_000_000, "cache_hit": 4_000_000}
    # 1M fresh input + 1M cache write + 4M cache hits + 0.1M output
    assert call_cost(call, prices)[0] == pytest.approx(3.0 + 3.75 + 1.2 + 1.5)
    two = parse_prices('{"c": [3.0, 15.0]}')
    assert call_cost(call, two)[0] == pytest.approx(3.0 + 3.0 + 12.0 + 1.5)  # cache billed at the input price


def test_entry_cost_is_unknown_if_any_model_is_unpriced():
    calls = [{"step": "plan", "model": "m-main", "sent": 1_000_000, "received": 0},
             {"step": "code", "model": "mystery", "sent": 10, "received": 10}]
    e = make_entry("j1", "plan", 1, calls, PRICES)
    assert e["cost_usd"] is None and e["requests"] == 2 and e["sent"] == 1_000_010
    known = make_entry("j1", "plan", 1, calls[:1], PRICES)
    assert known["cost_usd"] == pytest.approx(1.0)
    assert known["calls"][0]["cost_source"] == "configured"


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


# ---- reading Aider's usage lines ----------------------------------------------------------------

from app.jobs import parse_aider_usage  # noqa: E402


def test_aider_usage_plain_line_and_cost():
    out = "Tokens: 7.2k sent, 157 received. Cost: $0.0027 message, $0.0027 session."
    assert parse_aider_usage(out) == {"sent": 7200, "received": 157, "cache_write": 0, "cache_hit": 0, "cost": pytest.approx(0.0027)}


def test_aider_usage_with_cache_fields_is_not_missed():
    # with prompt caching Aider adds ", N cache write" / ", N cache hit" between sent and received
    out = ("Tokens: 4.4k sent, 3.2k cache write, 1.1k cache hit, 157 received.\n"
           "Cost: $0.02 message, $0.05 session.")
    u = parse_aider_usage(out)
    assert (u["sent"], u["cache_write"], u["cache_hit"], u["received"]) == (4400, 3200, 1100, 157)
    assert u["cost"] == pytest.approx(0.02)  # the "message" cost, not the running session total
    assert parse_aider_usage("Tokens: 4k sent, 900 cache hit, 10 received.")["cache_hit"] == 900


def test_aider_usage_adds_up_calls_and_handles_no_cost():
    out = "Tokens: 1k sent, 100 received. Cost: $0.01 message, $0.01 session.\nTokens: 2k sent, 300 received. Cost: $0.02 message, $0.03 session."
    u = parse_aider_usage(out)
    assert (u["sent"], u["received"]) == (3000, 400) and u["cost"] == pytest.approx(0.03)
    assert parse_aider_usage("Tokens: 1k sent, 100 received.")["cost"] is None
    assert parse_aider_usage("nothing here") == {"sent": 0, "received": 0, "cache_write": 0, "cache_hit": 0, "cost": None}


def test_add_aider_records_the_reported_cost_and_cache():
    job = Job("CA-1", "x")
    job.add_aider("Tokens: 7.2k sent, 157 received. Cost: $0.0027 message, $0.0027 session.", model="gemini/gemini-2.5-flash", step="code")
    c = job.calls[0]
    assert c["reported_cost"] == pytest.approx(0.0027) and c["sent"] == 7200 and c["cache_hit"] == 0
    entry = make_entry("j", "execute", 2, job.calls, {})  # no MODEL_PRICES at all
    assert entry["cost_usd"] == pytest.approx(0.0027) and entry["calls"][0]["cost_source"] == "reported"


def test_direct_calls_report_litellm_cost(monkeypatch):
    import sys
    import types

    class Msg:
        content = "hi"

    class Resp:
        usage = types.SimpleNamespace(prompt_tokens=305, completion_tokens=1319)
        choices = [types.SimpleNamespace(message=Msg())]

    fake = types.SimpleNamespace(completion=lambda **k: Resp(), completion_cost=lambda completion_response: 0.0034)
    monkeypatch.setitem(sys.modules, "litellm", fake)
    from app.pr_writer import _complete
    text, usage = _complete("m", "p")
    assert usage == {"sent": 305, "received": 1319, "cost": 0.0034}
    fake.completion_cost = lambda completion_response: (_ for _ in ()).throw(ValueError("unknown model"))
    assert _complete("m", "p")[1]["cost"] is None  # unknown model: no cost, no crash
