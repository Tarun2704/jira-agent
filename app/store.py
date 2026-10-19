"""Redis-backed state for the plan-first workflow.

Everything that must survive a Render restart lives here: each ticket's planning session
(chat history, current plan, Slack thread), a lease per running job, and Slack event ids
already handled. Works with any Redis, including Upstash's free tier (rediss:// URL).

Command budget: Upstash's free plan allows 500k commands/month, so nothing here polls.
"""
import json
import logging
import time
from dataclasses import asdict, dataclass, field

import redis

log = logging.getLogger(__name__)

PREFIX = "jiraagent:"

# A session moves: planning -> awaiting_reply <-> revising -> confirming -> executing -> delivered.
# `delivered` waits for follow-ups (each one is a new "round"); it ends as `done` (completed) when the
# PR is merged or the follow-up window passes. FAILED / NO_CHANGES only exist in sessions saved by
# older versions: a failed or empty round now also ends as `delivered`, so you can just ask again.
PLANNING, REVISING, AWAITING_REPLY, CONFIRMING, EXECUTING, DELIVERED = (
    "planning", "revising", "awaiting_reply", "confirming", "executing", "delivered",
)
DONE, FAILED, NO_CHANGES, CANCELLED = "done", "failed", "no_changes", "cancelled"
TERMINAL = {DONE, FAILED, NO_CHANGES, CANCELLED}
# States where a job should be running; a missing lease here means the job crashed.
RUNNING = {PLANNING, REVISING, EXECUTING}
# States where we're waiting for the person.
WAITING = {AWAITING_REPLY, CONFIRMING}

KEEP_FINISHED_SECONDS = 30 * 24 * 3600
MAX_RUNS_PER_TICKET = 40  # newest kept; each run is at most ~40 KB
MAX_COST_ENTRIES = 200  # per ticket; each entry is a few hundred bytes


@dataclass
class Session:
    issue_key: str
    status: str = PLANNING
    title: str = ""  # the ticket's summary, for Slack messages
    plan: str = ""  # latest plan, Markdown (the "Plan / Files / Risks" part)
    questions: str = ""  # open questions from the latest planning round ("" = none)
    files: list[str] = field(default_factory=list)  # repo files the plan is about; reused for revisions and coding
    delete_files: list[str] = field(default_factory=list)  # files the plan deletes (checked at planning time; the agent removes them first)
    messages: list[dict] = field(default_factory=list)  # {"role": "user"|"agent", "text", "at"}
    handled_upto: int = 0  # messages[:handled_upto] were seen by the planner already
    plan_version: int = 0
    slack_channel: str = ""
    slack_thread_ts: str = ""  # the first plan message; replies in this thread are the chat
    plan_msg_ts: str = ""  # message currently carrying the buttons
    reply_to: str = ""  # thread to answer in: where you last wrote ("" = the main DM, not a thread)
    pending_acks: list[dict] = field(default_factory=list)  # "looking into it" messages to turn into the answer
    reporter_account_id: str = ""
    # ---- rounds: the first request is round 1; every follow-up after delivery is another round ----
    round_no: int = 1
    rounds: list[dict] = field(default_factory=list)  # finished rounds: request, plan, files, summary, outcome, PR, diff
    round_request: str = ""  # what this round was asked to do ("" for round 1: the ticket itself)
    round_source: str = "jira"  # who started this round: "jira" (label), "slack" (a message) or "review" (PR comments)
    followup: bool = False  # delivered ticket got a message: planner decides "just an answer" or "a new round"
    force_plan: bool = False  # the new round must produce a plan (Jira re-trigger, review comments)
    pr_number: int = 0  # the PR of the latest delivered round (0 = none)
    pr_url: str = ""
    branch: str = ""
    delivered_at: float = 0.0
    review_seen: list[str] = field(default_factory=list)  # PR comment ids already offered to you
    review_pending: dict = field(default_factory=dict)  # {"text", "ids"}: comments waiting for your yes/no
    pr_notified: str = ""  # "closed" once you were told the PR was closed unmerged
    desc_prompt_at: float = 0.0  # when you were last asked "the description changed: plan a follow-up?"
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)  # last time the ball changed court
    reminded: bool = False
    result: str = ""  # PR link or the reason it ended
    # Set each time a run is approved or retried, and stamped into that run's commit ("Agent-Delivery: <id>"): a job
    # restarted after a crash checks the branch for it, so work that was already pushed is not done a second time.
    delivery_id: str = ""
    # The current plan or run was already started once more by itself after getting stuck (only once per attempt).
    stuck_retried: bool = False

    def touch(self) -> None:
        self.last_activity_at = time.time()

    def add_message(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text, "at": time.time()})

    @property
    def unhandled(self) -> bool:
        return len(self.messages) > self.handled_upto


class SessionStore:
    def __init__(self, client: "redis.Redis"):
        self.r = client

    @classmethod
    def from_url(cls, url: str) -> "SessionStore":
        return cls(
            redis.Redis.from_url(
                url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10, retry_on_timeout=True
            )
        )

    # ---- sessions ----------------------------------------------------------------

    @staticmethod
    def _k(key: str) -> str:
        return f"{PREFIX}session:{key}"

    def get(self, key: str) -> Session | None:
        raw = self.r.get(self._k(key))
        return Session(**json.loads(raw)) if raw else None

    def _save(self, pipe_or_r, s: Session) -> None:
        pipe_or_r.set(self._k(s.issue_key), json.dumps(asdict(s)))
        if s.status in TERMINAL:
            pipe_or_r.srem(f"{PREFIX}active", s.issue_key)
            pipe_or_r.expire(self._k(s.issue_key), KEEP_FINISHED_SECONDS)
        else:
            pipe_or_r.sadd(f"{PREFIX}active", s.issue_key)
            pipe_or_r.persist(self._k(s.issue_key))

    def create(self, key: str) -> Session | None:
        """Start a fresh session. None if one is already active for this ticket."""
        for _ in range(5):
            with self.r.pipeline() as pipe:
                try:
                    pipe.watch(self._k(key))
                    raw = pipe.get(self._k(key))
                    prior = Session(**json.loads(raw)) if raw else None
                    if prior and prior.status not in TERMINAL:
                        pipe.unwatch()
                        return None
                    s = Session(issue_key=key)
                    if prior and prior.rounds:  # a completed ticket is picked up again: keep its history
                        s.rounds = prior.rounds
                        s.round_no = max(r.get("n", 0) for r in prior.rounds) + 1
                        s.files = prior.files  # the earlier PR is remembered in rounds[-1]; this round starts fresh
                    pipe.multi()
                    self._save(pipe, s)
                    pipe.execute()
                    return s
                except redis.WatchError:
                    continue
        return None

    def update(self, key: str, fn) -> Session | None:
        """Atomically apply fn(session) -> bool|None. Skip the write if fn returns False.

        Returns the session as saved (or as read, if fn declined), None if there is no session.
        """
        for _ in range(8):
            with self.r.pipeline() as pipe:
                try:
                    pipe.watch(self._k(key))
                    raw = pipe.get(self._k(key))
                    if not raw:
                        pipe.unwatch()
                        return None
                    s = Session(**json.loads(raw))
                    if fn(s) is False:
                        pipe.unwatch()
                        return s
                    pipe.multi()
                    self._save(pipe, s)
                    pipe.execute()
                    return s
                except redis.WatchError:
                    continue
        raise RuntimeError(f"could not update session {key}: too much contention")

    def transition(self, key: str, from_statuses: set[str] | str, to: str, **changes) -> Session | None:
        """Move to `to` only if the status is currently one of `from_statuses`. None if not."""
        allowed = {from_statuses} if isinstance(from_statuses, str) else set(from_statuses)
        applied = []

        def apply(s: Session):
            if s.status not in allowed:
                return False
            s.status = to
            for k, v in changes.items():
                setattr(s, k, v)
            s.touch()
            applied.append(True)

        s = self.update(key, apply)
        return s if applied else None

    def active_keys(self) -> list[str]:
        return sorted(self.r.smembers(f"{PREFIX}active"))

    # ---- leases: "a job is running this ticket right now" -----------------------------

    @staticmethod
    def _lease(key: str) -> str:
        return f"{PREFIX}lease:{key}"

    def acquire_lease(self, key: str, owner: str, ttl: int) -> bool:
        return bool(self.r.set(self._lease(key), owner, nx=True, ex=ttl))

    def refresh_lease(self, key: str, owner: str, ttl: int) -> bool:
        if self.r.get(self._lease(key)) != owner:
            return False
        return bool(self.r.expire(self._lease(key), ttl))

    def release_lease(self, key: str, owner: str) -> None:
        if self.r.get(self._lease(key)) == owner:
            self.r.delete(self._lease(key))

    def has_lease(self, key: str) -> bool:
        return bool(self.r.exists(self._lease(key)))

    def lease_ttl(self, key: str) -> int:
        """Seconds until the lease lapses (<= 0 when there is none)."""
        return max(int(self.r.ttl(self._lease(key))), 0)

    # ---- tick lock: only one tick runs at a time, across instances (deploy overlap, local + prod) -------

    def acquire_tick_lock(self, owner: str, ttl: int) -> bool:
        return bool(self.r.set(f"{PREFIX}ticklock", owner, nx=True, ex=ttl))

    def release_tick_lock(self, owner: str) -> None:
        if self.r.get(f"{PREFIX}ticklock") == owner:
            self.r.delete(f"{PREFIX}ticklock")

    # ---- Slack bookkeeping ---------------------------------------------------------------

    def map_thread(self, channel: str, thread_ts: str, key: str) -> None:
        self.r.set(f"{PREFIX}thread:{channel}:{thread_ts}", key, ex=KEEP_FINISHED_SECONDS)

    def key_for_thread(self, channel: str, thread_ts: str) -> str | None:
        return self.r.get(f"{PREFIX}thread:{channel}:{thread_ts}")

    # ---- run log: what each Aider run was asked and answered (observability) ---------------------

    def append_runs(self, key: str, runs: list[dict]) -> None:
        if not runs:
            return
        k = f"{PREFIX}runs:{key}"
        pipe = self.r.pipeline()
        pipe.rpush(k, *[json.dumps(r) for r in runs])
        pipe.ltrim(k, -MAX_RUNS_PER_TICKET, -1)
        pipe.expire(k, KEEP_FINISHED_SECONDS)
        pipe.execute()

    def get_runs(self, key: str) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange(f"{PREFIX}runs:{key}", 0, -1)]

    # ---- cost: one entry per finished job (tokens, requests, cost). No expiry: totals outlive the session ------

    def append_cost(self, key: str, entry: dict) -> None:
        k = f"{PREFIX}cost:{key}"
        pipe = self.r.pipeline()
        pipe.rpush(k, json.dumps(entry))
        pipe.ltrim(k, -MAX_COST_ENTRIES, -1)
        pipe.execute()

    def get_costs(self, key: str) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange(f"{PREFIX}cost:{key}", 0, -1)]

    # ---- Jira comments that couldn't be posted (Jira down): retried on the next tick --------------

    def queue_jira_comment(self, key: str, body: str, tries: int = 0) -> None:
        self.r.rpush(f"{PREFIX}jirapending", json.dumps({"key": key, "body": body, "tries": tries}))
        self.r.expire(f"{PREFIX}jirapending", KEEP_FINISHED_SECONDS)

    def take_jira_comments(self) -> list[dict]:
        """Remove and return every queued comment (the caller re-queues those that fail again)."""
        k = f"{PREFIX}jirapending"
        pipe = self.r.pipeline()
        pipe.lrange(k, 0, -1)
        pipe.delete(k)
        items, _ = pipe.execute()
        return [json.loads(x) for x in items]

    # ---- Slack messages that couldn't be delivered (Slack down, token revoked...) -----------------------

    def queue_slack(self, msg: dict) -> None:
        self.r.rpush(f"{PREFIX}slackpending", json.dumps(msg))
        self.r.expire(f"{PREFIX}slackpending", KEEP_FINISHED_SECONDS)

    def take_slack(self) -> list[dict]:
        """Remove and return every queued Slack message, oldest first."""
        k = f"{PREFIX}slackpending"
        pipe = self.r.pipeline()
        pipe.lrange(k, 0, -1)
        pipe.delete(k)
        items, _ = pipe.execute()
        return [json.loads(x) for x in items]

    def requeue_slack_first(self, msgs: list[dict]) -> None:
        """Put messages back at the front (in order), ahead of anything queued meanwhile."""
        for m in reversed(msgs):
            self.r.lpush(f"{PREFIX}slackpending", json.dumps(m))
        self.r.expire(f"{PREFIX}slackpending", KEEP_FINISHED_SECONDS)

    # ---- a message waiting for you to say which ticket it is about --------------------------------

    def stash(self, channel: str, ts: str, payload: dict) -> None:
        self.r.set(f"{PREFIX}stash:{channel}:{ts}", json.dumps(payload), ex=3600)

    def unstash(self, channel: str, ts: str) -> dict | None:
        """Take (and remove) a stashed message; None if it expired or was already used."""
        k = f"{PREFIX}stash:{channel}:{ts}"
        pipe = self.r.pipeline()
        pipe.get(k)
        pipe.delete(k)
        raw, _ = pipe.execute()
        return json.loads(raw) if raw else None

    def first_seen(self, event_id: str) -> bool:
        """True the first time an id is seen (Slack redelivers events)."""
        return bool(self.r.set(f"{PREFIX}event:{event_id}", "1", nx=True, ex=3600))
