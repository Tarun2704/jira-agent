"""Observability: in-memory history of recent jobs and webhooks, plus job-tagged logging.

Everything here resets on restart/deploy (Render's free disk isn't persistent);
the logs themselves live in Render's Logs tab.
"""
import contextvars
import logging
import re
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# The job the current thread is working on, shown as [CA-3#1a2b3c4d] in log lines.
current_job: contextvars.ContextVar[str] = contextvars.ContextVar("current_job", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(job)s] %(name)s: %(message)s"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job = current_job.get()
        return True


class RedactTokenFilter(logging.Filter):
    """Hide ?token=... in uvicorn access logs (used by /jobs and the webhook fallback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                re.sub(r"token=[^&\s]+", "token=***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(JobContextFilter())
    logging.getLogger("uvicorn.access").addFilter(RedactTokenFilter())


_TOKENS_RE = re.compile(r"Tokens: ([\d.]+)([kKmM]?) sent, ([\d.]+)([kKmM]?) received")


def _to_int(num: str, suffix: str) -> int:
    return int(float(num) * {"": 1, "k": 1_000, "m": 1_000_000}[suffix.lower()])


def parse_aider_tokens(output: str) -> tuple[int, int]:
    """Sum the 'Tokens: 6.8k sent, 1.1k received' lines Aider prints."""
    sent = received = 0
    for s, s_suf, r, r_suf in _TOKENS_RE.findall(output):
        sent += _to_int(s, s_suf)
        received += _to_int(r, r_suf)
    return sent, received


@dataclass
class Job:
    issue_key: str
    event: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # queued -> running -> succeeded | no_changes | skipped | failed
    status: str = "queued"
    queued_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    steps: dict[str, float] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"sent": 0, "received": 0})
    pr_url: str | None = None
    jira_status: str | None = None  # last status the agent moved the ticket to
    detail: str | None = None  # skip reason or error message
    _t0: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.issue_key}#{self.id}"

    def start(self) -> None:
        self.status, self.started_at, self._t0 = "running", _now(), time.monotonic()

    def finish(self, status: str, detail: str | None = None) -> None:
        self.status, self.detail, self.finished_at = status, detail, _now()
        self.duration_s = round(time.monotonic() - self._t0, 1) if self._t0 else None

    @contextmanager
    def step(self, name: str):
        t = time.monotonic()
        try:
            yield
        finally:
            self.steps[name] = round(time.monotonic() - t, 1)

    def add_tokens(self, sent: int, received: int) -> None:
        self.tokens["sent"] += sent
        self.tokens["received"] += received

    def summary(self) -> str:
        steps = " ".join(f"{k}={v}s" for k, v in self.steps.items()) or "-"
        parts = [
            f"status={self.status}",
            f"total={self.duration_s}s",
            f"steps: {steps}",
            f"tokens: {self.tokens['sent']} sent / {self.tokens['received']} received",
        ]
        if self.pr_url:
            parts.append(f"pr={self.pr_url}")
        if self.detail:
            parts.append(f"detail={self.detail[:200]!r}")
        return " | ".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_t0")
        return d


class JobStore:
    def __init__(self, max_jobs: int = 50, max_webhooks: int = 100):
        self._jobs: deque[Job] = deque(maxlen=max_jobs)
        self._webhooks: deque[dict] = deque(maxlen=max_webhooks)
        self._lock = threading.Lock()

    def add_job(self, job: Job) -> None:
        with self._lock:
            self._jobs.append(job)

    def record_webhook(self, *, event: str, issue: str | None, outcome: str, reason: str = "") -> None:
        with self._lock:
            self._webhooks.append(
                {"at": _now(), "event": event, "issue": issue, "outcome": outcome, "reason": reason}
            )

    def snapshot(self, issue: str | None = None) -> dict:
        with self._lock:
            jobs = [j.to_dict() for j in reversed(self._jobs) if not issue or j.issue_key == issue]
            hooks = [w for w in reversed(self._webhooks) if not issue or w["issue"] == issue]
        return {"jobs": jobs, "webhooks": hooks}


store = JobStore()
