"""FastAPI webhook receiver: Jira -> queue -> coding agent."""
import hashlib
import hmac
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request

from app.agent import CodingAgent
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jira-agent")

app = FastAPI(title="Jira Coding Agent")

# One job at a time: free instances have little RAM, and LLM free tiers are rate-limited.
_executor = ThreadPoolExecutor(max_workers=1)
_in_flight: set[str] = set()
_lock = threading.Lock()


def _verify(request: Request, body: bytes, secret: str) -> bool:
    sig = request.headers.get("x-hub-signature", "")
    if sig.startswith("sha256="):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.removeprefix("sha256="), expected)
    token = request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token, secret)


def _run_job(issue_key: str) -> None:
    try:
        CodingAgent(get_settings()).handle_issue(issue_key)
    finally:
        with _lock:
            _in_flight.discard(issue_key)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "in_flight": sorted(_in_flight)}


@app.post("/jira-webhook", status_code=202)
async def jira_webhook(request: Request) -> dict:
    settings = get_settings()
    body = await request.body()
    if not _verify(request, body, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    event = payload.get("webhookEvent", "")
    issue = payload.get("issue") or {}
    key = issue.get("key")
    labels = (issue.get("fields") or {}).get("labels") or []
    if not key:
        return {"accepted": False, "reason": "no issue in payload"}
    if settings.trigger_label and settings.trigger_label not in labels:
        return {"accepted": False, "reason": f"missing label {settings.trigger_label!r}"}

    with _lock:
        if key in _in_flight:
            return {"accepted": False, "reason": "already queued"}
        _in_flight.add(key)

    log.info("Queued %s (event=%s)", key, event)
    _executor.submit(_run_job, key)
    return {"accepted": True, "issue": key}
