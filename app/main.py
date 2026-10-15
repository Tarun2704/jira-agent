"""FastAPI webhook receiver: Jira -> queue -> coding agent."""
import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import redis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from app.agent import CodingAgent
from app.costs import summarize
from app.config import get_settings
from app.jira_client import JiraClient
from app.jobs import Job, current_job, setup_logging, store
from app.selfping import SelfPinger
from app.slack_client import SlackClient, verify_signature
from app.store import SessionStore
from app.workflow import EXECUTE_JOB, PLAN_JOB, Workflow

setup_logging()
log = logging.getLogger("jira-agent")

# One coding job at a time: free instances have little RAM, and LLM free tiers are rate-limited.
_executor = ThreadPoolExecutor(max_workers=1)
# Planning and plan revisions get their own single worker (SEPARATE_PLAN_QUEUE), so a 15-minute
# coding run never leaves your Slack reply waiting. Jobs for the same ticket never overlap
# (_in_flight), and each pool runs one job at a time.
_MAX_PLAN_WORKERS = 4
_plan_executor = ThreadPoolExecutor(max_workers=_MAX_PLAN_WORKERS)  # threads; PLAN_WORKERS limits how many work at once
_plan_gate: threading.Semaphore | None = None
_in_flight: set[str] = set()
_lock = threading.Lock()


def _jira() -> JiraClient:
    return JiraClient(get_settings())


_wf: Workflow | None = None


def _workflow() -> Workflow:
    """The plan-first workflow (built on first use; needs Slack + Redis settings)."""
    global _wf
    if _wf is None:
        s = get_settings()
        _wf = Workflow(s, SessionStore.from_url(s.redis_url), SlackClient(s.slack_bot_token, s.slack_user_id), _submit)
        _wf.busy = lambda key: key in _in_flight
    return _wf


def _submit(kind: str, key: str) -> bool:
    """Queue a plan/execute job for the workflow. False if this ticket already has a job queued/running."""
    return _enqueue(key, f"workflow_{kind}", kind=kind) is not None


def _set_queue_label(key: str, present: bool) -> None:
    """Persist the queue in Jira: the label marks tickets queued or running.

    Best-effort: if Jira is unreachable the job still runs, it just won't survive a restart.
    """
    label = get_settings().queue_label
    if not label:
        return
    try:
        jira = _jira()
        (jira.add_label if present else jira.remove_label)(key, label)
    except Exception:
        log.warning("Could not %s label %r on %s", "add" if present else "remove", label, key, exc_info=True)


def recover_queued() -> list[str]:
    """Re-queue tickets that still carry the queue label (their job was cut off by a restart)."""
    label = get_settings().queue_label
    if not label:
        return []
    try:
        keys = _jira().search_keys(f'labels = "{label}" ORDER BY created ASC')
    except Exception:
        log.warning("Startup recovery: could not search Jira for label %r", label, exc_info=True)
        return []
    recovered = [k for k in keys if _enqueue(k, "recovered_after_restart")]
    log.info("Startup recovery: %d ticket(s) re-queued from Jira label %r: %s", len(recovered), label, recovered)
    return recovered


def _recover_after_delay() -> None:
    # During a deploy Render starts the new instance before stopping the old one (which
    # gets ~30s to finish). Waiting avoids re-queuing a ticket the old instance is still
    # working on; by then it has either finished (label removed) or been stopped (label kept).
    time.sleep(get_settings().recovery_delay_seconds)
    recover_queued()


def _startup_checks() -> None:
    """Say loudly, once, what is and isn't set up. Never raises."""
    settings = get_settings()
    for problem in settings.config_problems():
        log.warning("CONFIG: %s", problem)
    if not settings.planning_enabled:
        log.info("Plan-first mode is OFF: tickets with the label go straight to a pull request.")
        return
    try:
        who = _workflow().slack.auth_test()
        log.info("Slack OK: workspace=%s bot=%s. Plans will be DMed to %s.", who.get("team"), who.get("user"), settings.slack_user_id)
    except Exception as e:
        log.warning("CONFIG: Slack rejected SLACK_BOT_TOKEN (%s). No DMs can be sent.", e)
    try:
        _workflow().store.r.ping()
        log.info("Redis OK.")
    except Exception as e:
        log.warning("CONFIG: cannot reach Redis (%s: %s). Check REDIS_URL.", type(e).__name__, e)


# When /tick last ran (startup, an outside pinger or our own self-ping), shown by /health.
_last_tick: dict = {"at": 0.0, "source": ""}


def _note_tick(source: str) -> None:
    _last_tick["at"], _last_tick["source"] = time.time(), source


def _startup_tick() -> None:
    _startup_checks()
    try:
        log.info("Startup tick: %s", _workflow().tick())
        _note_tick("startup")
    except Exception:
        log.warning("Startup tick failed", exc_info=True)


_watchdog_stop = threading.Event()


def _watchdog_loop(every: int) -> None:
    """Re-queue crashed jobs and re-send held-back Slack messages, independently of /tick (see Workflow.watchdog)."""
    while not _watchdog_stop.wait(every):
        try:
            out = _workflow().watchdog()
            if out["requeued"] or out["slack_sent"]:
                log.info("Watchdog: %s", out)
        except Exception:
            log.warning("Watchdog run failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_settings().planning_enabled:
        # Leases make a delay unnecessary: a job still running on the old instance keeps its
        # lease alive, so only genuinely orphaned sessions get re-queued.
        threading.Thread(target=_startup_tick, name="startup-tick", daemon=True).start()
        if get_settings().watchdog_seconds > 0:
            _watchdog_stop.clear()
            threading.Thread(target=_watchdog_loop, args=(get_settings().watchdog_seconds,), name="watchdog", daemon=True).start()
    else:
        threading.Thread(target=_startup_checks, name="startup-checks", daemon=True).start()
        threading.Thread(target=_recover_after_delay, name="recover-queued", daemon=True).start()
    pinger = None
    base = get_settings().self_ping_base
    if base:
        s = get_settings()
        pinger = SelfPinger(base, s.tick_token, s.self_ping_interval_seconds, alert_after=s.self_ping_alert_after,
                            notify=lambda text: _workflow().slack.post(text))
        pinger.start()
        log.info("Self-ping is ON: this app calls its own /tick every %ss (keeps a free Render service awake).", pinger.interval)
    elif get_settings().planning_enabled:
        log.info("Self-ping is OFF (no public URL: RENDER_EXTERNAL_URL / SELF_PING_URL, or disabled). Use an outside pinger for /tick.")
    yield
    _watchdog_stop.set()
    if pinger:
        pinger.stop()


app = FastAPI(title="Jira Coding Agent", lifespan=lifespan)


def _verify(request: Request, body: bytes, secret: str) -> bool:
    sig = request.headers.get("x-hub-signature", "")
    if sig.startswith("sha256="):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.removeprefix("sha256="), expected)
    token = request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token, secret)


def _description_changed(payload: dict) -> bool:
    return payload.get("webhookEvent") == "jira:issue_updated" and any(
        item.get("field") == "description" for item in (payload.get("changelog") or {}).get("items") or []
    )


_deferred_triggers: set[str] = set()  # kept here if even the workflow couldn't be built (storage down)


def _defer_trigger(key: str) -> None:
    try:
        _workflow().defer_trigger(key)
    except Exception:
        _deferred_triggers.add(key)


def _tell_user(channel: str, text: str, thread_ts: str = "") -> None:
    """Last resort when handling a Slack message or click failed: say so instead of staying silent."""
    try:
        s = get_settings()
        SlackClient(s.slack_bot_token, s.slack_user_id).post(text, channel=channel, thread_ts=thread_ts)
    except Exception:
        log.warning("Could not tell the user about the failure either", exc_info=True)


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


def _plan_slot() -> threading.Semaphore:
    """Limits concurrent planning jobs to PLAN_WORKERS (read once, on first use)."""
    global _plan_gate
    with _lock:
        if _plan_gate is None:
            try:
                n = get_settings().plan_workers
            except Exception:
                n = 1
            _plan_gate = threading.Semaphore(max(1, min(int(n), _MAX_PLAN_WORKERS)))
        return _plan_gate


def _run_job(job: Job) -> None:
    ctx = current_job.set(job.label)
    use_gate = job.kind == PLAN_JOB and _uses_plan_pool()
    if use_gate:
        _plan_slot().acquire()  # queued jobs wait here until a planning slot frees up
    job.start()
    log.info("Job started (event=%s)", job.event)
    try:
        if job.kind == PLAN_JOB:
            _workflow().run_plan_job(job)
        elif job.kind == EXECUTE_JOB:
            _workflow().run_execute_job(job)
        else:
            CodingAgent(get_settings(), job).handle_issue(job.issue_key)
    except Exception as e:  # handle_issue never raises; this covers the workflow (e.g. Redis down)
        log.exception("Job crashed")
        job.finish("failed", str(e)[-500:])
    finally:
        if job.kind == "code":
            _set_queue_label(job.issue_key, present=False)
        log.info("Job finished: %s", job.summary())
        current_job.reset(ctx)
        if use_gate:
            _plan_slot().release()
        with _lock:
            _in_flight.discard(job.issue_key)
        if job.kind == PLAN_JOB:
            try:  # you may have written again while it was planning
                _workflow().resume_if_needed(job.issue_key)
            except Exception:
                log.warning("resume_if_needed failed", exc_info=True)


def _uses_plan_pool() -> bool:
    try:
        return get_settings().separate_plan_queue
    except Exception:
        return True


def _enqueue(key: str, event: str, kind: str = "code") -> Job | None:
    """Queue a job for `key` unless one is already queued/running."""
    with _lock:
        if key in _in_flight:
            return None
        _in_flight.add(key)
    job = Job(issue_key=key, event=event, kind=kind)
    store.add_job(job)
    pool = _plan_executor if kind == PLAN_JOB and _uses_plan_pool() else _executor
    log.info("Queued %s (event=%s, queue=%s)", job.label, event, "plan" if pool is _plan_executor else "coding")
    pool.submit(_run_job, job)
    return job


def _ignore(event: str, key: str | None, reason: str) -> dict:
    log.info("Webhook ignored: event=%s issue=%s reason=%s", event, key, reason)
    store.record_webhook(event=event, issue=key, outcome="ignored", reason=reason)
    return {"accepted": False, "reason": reason}


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    out = {
        "status": "ok",
        "in_flight": sorted(_in_flight),
        "plan_mode": settings.planning_enabled,
        "problems": settings.config_problems(),  # names only, never secret values
    }
    if settings.planning_enabled:
        at = _last_tick["at"]
        age = round(time.time() - at) if at else None
        out["tick"] = {
            "last_at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds") if at else None,
            "age_seconds": age,
            "source": _last_tick["source"] or None,
            "self_ping": bool(settings.self_ping_base),
            # no tick for more than two intervals: the pinger/self-ping is not reaching us
            "overdue": age is None or age > 2 * settings.self_ping_interval_seconds,
        }
    return out


@app.get("/jobs")
def jobs(request: Request, issue: str | None = None) -> dict:
    """Recent jobs and webhook deliveries (newest first). Auth: ?token=<WEBHOOK_SECRET>
    or 'Authorization: Bearer <WEBHOOK_SECRET>'. Resets on every restart/deploy."""
    secret = get_settings().webhook_secret
    token = request.query_params.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="invalid token")
    return {"in_flight": sorted(_in_flight), **store.snapshot(issue)}


@app.get("/runs/{key}")
def runs(key: str, request: Request) -> dict:
    """What each Aider run for a ticket was asked and answered (planning, coding, fixes), oldest first.
    Kept in Redis for 30 days. Auth: ?token=<WEBHOOK_SECRET> or 'Authorization: Bearer <WEBHOOK_SECRET>'."""
    settings = get_settings()
    token = request.query_params.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid token")
    if not settings.planning_enabled:
        raise HTTPException(status_code=503, detail="run logs need plan-first mode (Redis)")
    return {"issue": key, "runs": _workflow().store.get_runs(key)}


@app.get("/usage/{key}")
def usage(key: str, request: Request) -> dict:
    """What a ticket's model calls used and cost, per round and in total (tokens, requests, USD if MODEL_PRICES
    lists the models). Kept in Redis without expiry. Auth: ?token=<WEBHOOK_SECRET> or 'Authorization: Bearer ...'."""
    settings = get_settings()
    token = request.query_params.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid token")
    if not settings.planning_enabled:
        raise HTTPException(status_code=503, detail="cost records need plan-first mode (Redis)")
    return {"issue": key, **summarize(_workflow().store.get_costs(key))}


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
    if settings.planning_enabled and _description_changed(payload):
        # A delivered ticket whose description was edited: ask in Slack whether to plan a follow-up.
        try:
            asked = await asyncio.to_thread(_workflow().on_description_changed, key)
        except Exception:
            log.exception("Description-change handling failed for %s", key)
            return _ignore(event, key, "description change could not be handled (storage unavailable)")
        if asked == "asked":
            store.record_webhook(event=event, issue=key, outcome="accepted", reason="asked about a follow-up")
            return {"accepted": True, "issue": key, "mode": "followup-prompt"}
        return _ignore(event, key, "description changed (no delivered ticket to follow up)")
    if not _is_trigger(payload, settings.trigger_label):
        return _ignore(event, key, f"ignored event {event!r} (only ticket created or label just added)")

    if settings.planning_enabled:
        # Plan first: the session lives in Redis, so no queue label is needed.
        try:
            started = await asyncio.to_thread(_workflow().on_trigger, key)
        except Exception as e:  # storage down: say so on the ticket and retry on the next tick, never drop it
            log.exception("Could not start planning for %s", key)
            _defer_trigger(key)
            try:
                await asyncio.to_thread(
                    JiraClient(settings).add_comment, key,
                    "🤖 I couldn't start planning right now (I couldn't save my progress). I'll retry automatically; nothing was lost.",
                )
            except Exception:
                log.warning("Could not comment about the deferred start on %s", key, exc_info=True)
            store.record_webhook(event=event, issue=key, outcome="deferred", reason=type(e).__name__)
            return {"accepted": False, "issue": key, "reason": "storage unavailable; will retry"}
        if not started:
            return _ignore(event, key, "a plan for this ticket is already in progress")
        store.record_webhook(event=event, issue=key, outcome="accepted", reason="planning started")
        log.info("Webhook accepted: planning started for %s", key)
        return {"accepted": True, "issue": key, "mode": "plan"}

    # Persist before acknowledging, so a restart right after this can still recover the ticket.
    await asyncio.to_thread(_set_queue_label, key, True)
    job = _enqueue(key, event)
    if job is None:
        return _ignore(event, key, "already queued")
    store.record_webhook(event=event, issue=key, outcome="accepted", reason=f"job {job.id}")
    log.info("Webhook accepted: %s", job.label)
    return {"accepted": True, "issue": key, "job_id": job.id}


# ---- plan-first workflow endpoints ---------------------------------------------------------


def _check_token(request: Request, expected: str) -> bool:
    token = request.query_params.get("token") or request.headers.get("authorization", "").removeprefix("Bearer ")
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


@app.get("/tick")
def tick(request: Request) -> dict:
    """Called every ~10 min by an external pinger (it also keeps a free Render instance awake).

    Re-queues jobs whose process died, and sends the "no reply yet" reminder. Auth: ?token=<TICK_TOKEN>.
    """
    settings = get_settings()
    if not _check_token(request, settings.tick_token):
        raise HTTPException(status_code=401, detail="invalid token")
    if not settings.planning_enabled:
        return {"planning_enabled": False}
    try:
        wf = _workflow()
        while _deferred_triggers:
            wf.defer_trigger(_deferred_triggers.pop())
        result = wf.tick()
        _note_tick("request")
        return {"planning_enabled": True, **result}
    except Exception as e:  # e.g. Redis unreachable: a clear failure for the pinger to show
        log.exception("Tick failed")
        raise HTTPException(status_code=503, detail=f"tick failed: {type(e).__name__}") from e


async def _verified_slack_body(request: Request) -> bytes:
    settings = get_settings()
    if not settings.planning_enabled or not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="plan-first mode is not configured")
    body = await request.body()
    ok = verify_signature(
        settings.slack_signing_secret,
        request.headers.get("x-slack-request-timestamp", ""),
        body,
        request.headers.get("x-slack-signature", ""),
    )
    if not ok:
        log.warning("Slack request rejected: bad signature (check SLACK_SIGNING_SECRET)")
        raise HTTPException(status_code=401, detail="invalid signature")
    return body


def _with_one_retry(fn):
    """Run fn; if storage dropped the connection before anything happened, wait a second and run it once more."""
    try:
        return fn()
    except (redis.ConnectionError, redis.TimeoutError):
        log.warning("Storage connection failed; retrying once", exc_info=True)
        time.sleep(1)
        return fn()


def _handle_slack_message(event: dict) -> None:
    if event.get("files"):  # the text is read; attachments (screenshots, logs) aren't, so don't let that pass silently
        _tell_user(event.get("channel", ""), ":paperclip: I can't open attached files yet, only the text of your message. "
                   "Please paste what matters from it as text.", event.get("thread_ts") or "")
        if not (event.get("text") or "").strip():
            return
    try:
        outcome = _with_one_retry(lambda: _workflow().on_user_message(
            event.get("channel", ""), event.get("thread_ts", ""), event.get("user", ""), event.get("text", ""),
            event.get("ts", ""),
        ))
        log.info("Slack message handled: %s", outcome)
    except Exception as e:
        log.exception("Slack message handling failed")
        _tell_user(event.get("channel", ""), ":x: Something went wrong handling your message. Please send it again in a minute.", event.get("thread_ts") or "")


def _handle_slack_action(action_id: str, key: str, user: str, channel: str, message_ts: str) -> None:
    try:
        outcome = _with_one_retry(lambda: _workflow().on_action(action_id, key, user, channel, message_ts))
        log.info("Slack action %s on %s handled: %s", action_id, key, outcome)
    except Exception as e:
        log.exception("Slack action handling failed")
        _tell_user(channel, ":x: That button didn't work. Please press it again in a minute.")


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    """Slack Events API: your DM replies. Must answer within 3 s, so the work runs in the background."""
    body = await _verified_slack_body(request)
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        log.info("Slack URL verification request answered")
        return {"challenge": payload.get("challenge", "")}
    event = payload.get("event") or {}
    log.info(
        "Slack event received: type=%s channel_type=%s subtype=%s bot=%s user=%s thread=%s",
        event.get("type"), event.get("channel_type"), event.get("subtype"), bool(event.get("bot_id")),
        event.get("user"), event.get("thread_ts") or "-",
    )
    is_dm = event.get("type") == "message" and event.get("channel_type") == "im"
    # Skip bot messages (our own posts) and edits/deletions/etc. Kept: a thread reply also sent to the chat
    # (thread_broadcast) and a message with a file (file_share); both are things you wrote.
    written = event.get("subtype") in (None, "thread_broadcast", "file_share")
    if payload.get("type") == "event_callback" and is_dm and written and not event.get("bot_id"):
        event_id = payload.get("event_id", "")
        if event_id and not await asyncio.to_thread(_workflow().store.first_seen, event_id):
            return {"ok": True, "duplicate": True}  # Slack redelivery
        background.add_task(_handle_slack_message, event)
    else:
        log.info("Slack event ignored (not a message from a person in a DM)")
    return {"ok": True}


@app.post("/slack/interactive")
async def slack_interactive(request: Request, background: BackgroundTasks) -> Response:
    """Slack button clicks (Proceed / Yes, start now / Go back)."""
    body = await _verified_slack_body(request)
    form = urllib.parse.parse_qs(body.decode())
    payload = json.loads((form.get("payload") or ["{}"])[0])
    if payload.get("type") == "block_actions" and payload.get("actions"):
        a = payload["actions"][0]
        background.add_task(
            _handle_slack_action,
            a.get("action_id", ""),
            a.get("value", ""),
            (payload.get("user") or {}).get("id", ""),
            (payload.get("channel") or {}).get("id", ""),
            (payload.get("message") or {}).get("ts", ""),
        )
    return Response(status_code=200)
