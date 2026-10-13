"""Cost tracking: what each model call used, and what it cost, per ticket and round.

Prices are NOT built in (they change, and free tiers cost nothing): set MODEL_PRICES to a JSON object of
USD per 1 million tokens, [input, output], keyed by the exact model name, e.g.
  MODEL_PRICES={"gemini/gemini-2.5-flash": [0.30, 2.50]}
A model with no price listed has an unknown cost (tokens and request counts are still kept).
Token counts for Aider come from the "Tokens:" lines it prints, so costs are estimates.
"""
import json
import logging
import time

log = logging.getLogger(__name__)


def parse_prices(raw: str) -> dict[str, tuple[float, float]]:
    """MODEL_PRICES text -> {model: (input_per_million, output_per_million)}. Bad input gives {} (never raises)."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return {str(m): (float(p[0]), float(p[1])) for m, p in data.items()}
    except (ValueError, TypeError, IndexError, KeyError, AttributeError):
        log.warning("MODEL_PRICES is not valid JSON like {\"model\": [input, output]}; costs will show as unknown")
        return {}


def call_cost(call: dict, prices: dict[str, tuple[float, float]]) -> float | None:
    p = prices.get(call.get("model", ""))
    if not p:
        return None
    return (call.get("sent", 0) * p[0] + call.get("received", 0) * p[1]) / 1_000_000


def make_entry(job_id: str, kind: str, round_no: int, calls: list[dict], prices: dict[str, tuple[float, float]]) -> dict:
    """One saved record per job: its calls, totals and cost (None if any call's model has no price)."""
    priced = [dict(c, cost_usd=call_cost(c, prices)) for c in calls]
    costs = [c["cost_usd"] for c in priced]
    known = bool(priced) and all(c is not None for c in costs)
    return {
        "at": time.time(), "job": job_id, "kind": kind, "round": round_no,
        "requests": len(priced),
        "sent": sum(c.get("sent", 0) for c in priced),
        "received": sum(c.get("received", 0) for c in priced),
        "cost_usd": round(sum(costs), 6) if known else None,
        "calls": priced,
    }


def summarize(entries: list[dict]) -> dict:
    """Per-round and whole-ticket totals from the saved entries (oldest first)."""
    rounds: dict[int, dict] = {}
    for e in entries:
        r = rounds.setdefault(e.get("round", 0), {"round": e.get("round", 0), "requests": 0, "sent": 0, "received": 0,
                                                  "cost_usd": 0.0, "cost_known": True, "jobs": []})
        r["requests"] += e.get("requests", 0)
        r["sent"] += e.get("sent", 0)
        r["received"] += e.get("received", 0)
        if e.get("cost_usd") is None:
            if e.get("requests"):
                r["cost_known"] = False
        else:
            r["cost_usd"] += e["cost_usd"]
        r["jobs"].append({"job": e.get("job"), "kind": e.get("kind"), "requests": e.get("requests", 0),
                          "sent": e.get("sent", 0), "received": e.get("received", 0), "cost_usd": e.get("cost_usd"),
                          "calls": e.get("calls", [])})
    per_round = []
    for n in sorted(rounds):
        r = rounds[n]
        r["cost_usd"] = round(r["cost_usd"], 6) if r["cost_known"] else None
        r.pop("cost_known")
        per_round.append(r)
    total = {
        "requests": sum(r["requests"] for r in per_round),
        "sent": sum(r["sent"] for r in per_round),
        "received": sum(r["received"] for r in per_round),
        "cost_usd": None if any(r["cost_usd"] is None for r in per_round) or not per_round
        else round(sum(r["cost_usd"] for r in per_round), 6),
    }
    return {"rounds": per_round, "total": total}


def money(x: float | None) -> str:
    if x is None:
        return "unknown"
    return f"${x:.4f}" if x < 0.01 else f"${x:.2f}"
