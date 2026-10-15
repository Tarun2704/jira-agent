"""Cost tracking: what each model call used, and what it cost, per ticket and round.

Prices for the models this service normally uses are built in (DEFAULT_PRICES below, USD per 1 million tokens,
keyed by the exact model name as Aider/litellm and OpenCode spell it). MODEL_PRICES, a JSON object in the same
shape, adds models or overrides a built-in price (e.g. a free tier: [0, 0]), e.g.
  MODEL_PRICES={"gemini/gemini-2.5-flash": [0.30, 2.50]}
Where do costs come from, in order:
  1. MODEL_PRICES or DEFAULT_PRICES, if the model is listed (your own prices win). Prices are [input, output] or
     [input, output, cache_write, cache_hit]. With two numbers and a call that used the cache, the reported cost
     (step 2) is used instead when there is one; otherwise cache tokens are billed at the input price.
  2. The cost the provider layer reported for the call: Aider prints "Cost: $..." and litellm computes one for
     direct calls, both from list prices for the models it knows (free tiers still show list price).
  3. Otherwise unknown (tokens and request counts are still kept).
Token counts for Aider come from the "Tokens:" lines it prints (rounded, e.g. 7.2k), so configured-price costs are estimates.
"""
import json
import logging
import time

log = logging.getLogger(__name__)

# List prices, USD per 1M tokens: [input, output] or [input, output, cache_write (5-minute), cache_hit].
# Update these when providers change prices; MODEL_PRICES can override any of them without a deploy.
_CLAUDE_OPUS_5_5 = (4.00, 20.00, 5.00, 0.20)
_CLAUDE_SONNET_5_5 = (2.00, 10.00, 2.50, 0.20)
# Gemini (list prices, as in litellm's price table): implicit caching charges nothing extra to write the cache (the
# normal input price) and a tenth of the input price for cache hits.
_GEMINI_2_5_FLASH = (0.30, 2.50, 0.30, 0.03)
_GEMINI_3_5_FLASH_LITE = (0.30, 2.50, 0.30, 0.03)
_GEMINI_3_1_FLASH_LITE = (0.25, 1.50, 0.25, 0.025)
_GEMINI_3_5_FLASH = (1.50, 9.00, 1.50, 0.15)
DEFAULT_PRICES: dict[str, tuple[float, ...]] = {
    "claude-opus-5-5": _CLAUDE_OPUS_5_5,  # Aider / litellm name
    "anthropic/claude-opus-5-5": _CLAUDE_OPUS_5_5,  # OpenCode name
    "claude-sonnet-5-5": _CLAUDE_SONNET_5_5,
    "anthropic/claude-sonnet-5-5": _CLAUDE_SONNET_5_5,
}
for _name, _price in (("gemini-2.5-flash", _GEMINI_2_5_FLASH), ("gemini-3.5-flash-lite", _GEMINI_3_5_FLASH_LITE),
                      ("gemini-3.1-flash-lite", _GEMINI_3_1_FLASH_LITE), ("gemini-3.5-flash", _GEMINI_3_5_FLASH)):
    DEFAULT_PRICES[f"gemini/{_name}"] = DEFAULT_PRICES[f"google/{_name}"] = _price  # Aider and OpenCode spellings


def parse_prices(raw: str) -> dict[str, tuple[float, ...]]:
    """MODEL_PRICES text -> {model: (in, out) or (in, out, cache_write, cache_hit)} per 1M tokens. Bad input gives {}."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        out: dict[str, tuple[float, ...]] = {}
        for m, p in data.items():
            nums = tuple(float(x) for x in p)
            if len(nums) not in (2, 4):
                raise ValueError("need 2 or 4 numbers")
            out[str(m)] = nums
        return out
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("MODEL_PRICES is not valid JSON like {\"model\": [input, output]}; configured prices are ignored")
        return {}


def model_prices(raw: str) -> dict[str, tuple[float, ...]]:
    """Built-in prices with MODEL_PRICES applied on top."""
    return {**DEFAULT_PRICES, **parse_prices(raw)}


def call_cost(call: dict, prices: dict[str, tuple[float, ...]]) -> tuple[float | None, str | None]:
    """(cost in USD, where it came from: "configured" / "reported" / None)."""
    p = prices.get(call.get("model", ""))
    cw, ch = call.get("cache_write", 0) or 0, call.get("cache_hit", 0) or 0
    if p and len(p) == 2 and (cw or ch) and call.get("reported_cost") is not None:
        # Cached tokens but no cache rates for this model: billing them at the input price can double the estimate
        # (an OpenCode run re-reads its whole conversation from the cache on every step). The provider layer knows
        # the discount, so its figure is the better one here.
        return float(call["reported_cost"]), "reported"
    if p:
        sent, recv = call.get("sent", 0), call.get("received", 0)
        in_p, out_p = p[0], p[1]
        cw_p, ch_p = (p[2], p[3]) if len(p) == 4 else (in_p, in_p)
        # Aider's "sent" already includes the cache-write tokens; cache hits are counted separately
        cost = (max(sent - cw, 0) * in_p + cw * cw_p + ch * ch_p + recv * out_p) / 1_000_000
        return cost, "configured"
    if call.get("reported_cost") is not None:
        return float(call["reported_cost"]), "reported"
    if not call.get("sent") and not call.get("received"):
        return 0.0, "no usage"  # a call that printed no usage (it failed early) billed nothing we can see
    return None, None


def make_entry(job_id: str, kind: str, round_no: int, calls: list[dict], prices: dict[str, tuple[float, ...]]) -> dict:
    """One saved record per job: its calls, totals and cost (None if any call has no known price)."""
    priced = []
    for c in calls:
        cost, source = call_cost(c, prices)
        priced.append(dict(c, cost_usd=cost, cost_source=source))
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
