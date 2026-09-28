"""FastAPI webhook receiver: Jira -> queue -> coding agent."""
import hashlib
import hmac
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request

from app.agent import CodingAgent
from app.config import get_settings
from app.jobs import Job, current_job, setup_logging, store

setup_logging()
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


def _is_trigger(payload: dict, label: str) -> bool:
    """New ticket, or an update that just added the trigger label.

    Jira also sends jira:issue_updated for comments, so without this the agent's own
    comments would re-trigger it.
    """
    event = payload.get("webhookEvent", "")
    if event == "jira:issue_created":
        return True
    if event != "jira:issue_updated" or not label:
        return False
    for item in (payload.get("changelog") or {}).get("items") or []:
        if item.get("field") == "labels":
            before = (item.get("fromString") or "").split()
            after = (item.get("toString") or "").split()
            if label in after and label not in before:
                return True
    return False


def _run_job(job: Job) -> None:
    ctx = current_job.set(job.label)
    job.start()
    log.info("Job started (event=%s)", job.event)
    try:
        CodingAgent(get_settings(), job).handle_issue(job.issue_key)
    finally:
        log.info("Job finished: %s", job.summary())
        current_job.reset(ctx)
        with _lock:
            _in_flight.discard(job.issue_key)


def _ignore(event: str, key: str | None, reason: str) -> dict:
    log.info("Webhook ignored: event=%s issue=%s reason=%s", event, key, reason)
    store.record_webhook(event=event, issue=key, outcome="ignored", reason=reason)
    return {"accepted": False, "reason": reason}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "in_flight": sorted(_in_flight)}


@app.get("/jobs")
def jobs(request: Request, issue: str | None = None) -> dict:
    """Recent jobs and webhook deliveries (newest first). Auth: ?token=<WEBHOOK_SECRET>
    or 'Authorization: Bearer <WEBHOOK_SECRET>'. Resets on every restart/deploy."""
    secret = get_settings().webhook_secret
    token = request.query_params.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="invalid token")
    return {"in_flight": sorted(_in_flight), **store.snapshot(issue)}


@app.post("/jira-webhook", status_code=202)
async def jira_webhook(request: Request) -> dict:
    settings = get_settings()
    body = await request.body()
    if not _verify(request, body, settings.webhook_secret):
        log.warning("Webhook rejected: invalid signature/token (check the Secret in Jira matches WEBHOOK_SECRET)")
        store.record_webhook(event="?", issue=None, outcome="rejected", reason="invalid signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    event = payload.get("webhookEvent", "")
    issue = payload.get("issue") or {}
    key = issue.get("key")
    labels = (issue.get("fields") or {}).get("labels") or []
    if not key:
        return _ignore(event, key, "no issue in payload")
    if settings.trigger_label and settings.trigger_label not in labels:
        return _ignore(event, key, f"missing label {settings.trigger_label!r}")
    if not _is_trigger(payload, settings.trigger_label):
        return _ignore(event, key, f"ignored event {event!r} (only ticket created or label just added)")

    with _lock:
        if key in _in_flight:
            return _ignore(event, key, "already queued")
        _in_flight.add(key)

    job = Job(issue_key=key, event=event)
    store.add_job(job)
    store.record_webhook(event=event, issue=key, outcome="accepted", reason=f"job {job.id}")
    log.info("Webhook accepted: queued %s (event=%s)", job.label, event)
    _executor.submit(_run_job, job)
    return {"accepted": True, "issue": key, "job_id": job.id}
