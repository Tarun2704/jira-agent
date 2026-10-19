"""Observability: in-memory history of recent jobs and webhooks, plus job-tagged logging.

Everything here resets on restart/deploy (Render's free disk isn't persistent);
the logs themselves live in Render's Logs tab.
"""
import contextvars
import logging
import os
import re
import signal
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


# Secrets in URLs and headers that libraries put into their error messages (litellm logs Google's request URL, which
# carries the API key as ?key=...), plus GitHub's token-in-URL form.
_SECRET_PATTERNS = [
    (re.compile(r"([?&](?:key|api_key|token|access_token|sig)=)[^&\s'\"]+", re.I), r"\1***"),
    (re.compile(r"(x-access-token:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
]


def secret_values(environ: dict | None = None) -> list[str]:
    """Values of the app's secret settings (keys, tokens, secrets, passwords in URLs), longest first."""
    import os

    env = os.environ if environ is None else environ
    values = [v for k, v in env.items() if v and len(v) >= 8 and re.search(r"(_KEY|_TOKEN|_SECRET|PASSWORD)$", k)]
    values += [m.group(1) for k, v in env.items() if k.endswith("_URL") and v for m in [re.search(r"://[^:/@]+:([^@]{6,})@", v)] if m]
    return sorted(set(values), key=len, reverse=True)


def redact_secrets(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "***")
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's formatter and masks secrets in the finished line, tracebacks included."""

    def __init__(self, inner: logging.Formatter | None, secrets: list[str]):
        super().__init__()
        self.inner, self.secrets = inner or logging.Formatter(), secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self.inner.format(record), self.secrets)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    secrets = secret_values()
    for handler in logging.getLogger().handlers:
        handler.addFilter(JobContextFilter())
    logging.getLogger("uvicorn.access").addFilter(RedactTokenFilter())
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(handler.formatter, secrets))


# With prompt caching Aider adds ", N cache write" / ", N cache hit" between "sent" and "received".
_N = r"([\d.]+)([kKmM]?)"
_TOKENS_RE = re.compile(rf"Tokens: {_N} sent(?:, {_N} cache write)?(?:, {_N} cache hit)?, {_N} received")
_COST_RE = re.compile(r"Cost: \$([\d.]+) message")  # this call's cost; the "session" figure is a running total


def _to_int(num: str, suffix: str) -> int:
    return int(float(num) * {"": 1, "k": 1_000, "m": 1_000_000}[suffix.lower()])


def parse_aider_usage(output: str) -> dict:
    """Sum the 'Tokens: 6.8k sent, [3k cache write, 1k cache hit,] 1.1k received. Cost: $0.01 message' lines Aider prints.

    "sent" includes cache writes; cache hits are counted separately (that is how Aider reports them). "cost" is
    Aider's own figure from list prices, or None when it printed none (a model it has no price for)."""
    u = {"sent": 0, "received": 0, "cache_write": 0, "cache_hit": 0, "cost": None}
    for s, s_suf, w, w_suf, h, h_suf, r, r_suf in _TOKENS_RE.findall(output):
        u["sent"] += _to_int(s, s_suf)
        u["cache_write"] += _to_int(w, w_suf) if w else 0
        u["cache_hit"] += _to_int(h, h_suf) if h else 0
        u["received"] += _to_int(r, r_suf)
    costs = [float(c) for c in _COST_RE.findall(output)]
    if costs:
        u["cost"] = sum(costs)
    return u


def parse_aider_tokens(output: str) -> tuple[int, int]:
    u = parse_aider_usage(output)
    return u["sent"], u["received"]


MAX_RUN_PROMPT = 8000  # characters kept per run; Redis and Slack both have limits
MAX_RUN_OUTPUT = 30000


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
    checks: dict[str, str] = field(default_factory=dict)  # check name -> passed/failed/skipped
    detail: str | None = None  # skip reason or error message
    error_code: str | None = None  # for failures: the kind in app/errors.py (decides wording, Retry, admin alert)
    kind: str = "code"  # "code" = the original direct flow; "plan" / "execute" = plan-first workflow
    runs: list[dict] = field(default_factory=list)  # each Aider run: prompt in, output out (see record_run)
    warnings: list[str] = field(default_factory=list)  # non-fatal problems the person should hear about
    delivery: dict = field(default_factory=dict)  # branch, PR number/url, diff and summary of what was delivered
    calls: list[dict] = field(default_factory=list)  # each model call: step, model, tokens sent/received (see add_tokens)
    _t0: float = 0.0

    def __post_init__(self) -> None:
        # Runtime only (not fields, so never stored or shown): when the job last showed signs of life, and the child
        # processes it is running, so the watchdog can stop a job that is alive but stuck (see Job.stop_stuck).
        self.last_progress = time.monotonic()
        self.current_step = "start"  # the step running now (`steps` only holds finished ones)
        self.stuck = False
        self._procs: set = set()
        self._killed: set = set()  # processes stop_stuck killed (only those count as stopped; cleanup still runs)
        self._procs_lock = threading.Lock()

    def progress(self) -> None:
        """Something happened (a step started or ended, a child process printed a line, an agent event arrived)."""
        self.last_progress = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_progress

    def track(self, proc) -> None:
        with self._procs_lock:
            self._procs.add(proc)

    def untrack(self, proc) -> None:
        with self._procs_lock:
            self._procs.discard(proc)

    def stop_stuck(self) -> int:
        """Mark the job stuck and kill its child processes (each with its own children). The job then fails or falls
        back the normal way, seeing `stuck`. Returns how many processes were killed."""
        self.stuck = True
        with self._procs_lock:
            procs = list(self._procs)
            self._killed.update(procs)
        for proc in procs:
            kill_group(proc)
        return len(procs)

    def was_stopped(self, proc) -> bool:
        """Was this process killed because the job was stuck?"""
        with self._procs_lock:
            return proc in self._killed

    @property
    def label(self) -> str:
        return f"{self.issue_key}#{self.id}"

    def start(self) -> None:
        self.status, self.started_at, self._t0 = "running", _now(), time.monotonic()

    def finish(self, status: str, detail: str | None = None, code: str | None = None) -> None:
        self.status, self.detail, self.finished_at = status, detail, _now()
        self.error_code = code  # a kind in app/errors.py, for failures
        self.duration_s = round(time.monotonic() - self._t0, 1) if self._t0 else None

    @contextmanager
    def step(self, name: str):
        t = time.monotonic()
        self.progress()
        outer, self.current_step = self.current_step, name
        try:
            yield
        finally:
            self.steps[name] = round(time.monotonic() - t, 1)
            self.current_step = outer
            self.progress()

    def record_run(self, step: str, model: str, prompt: str, output: str, ok: bool = True) -> None:
        """Keep what was sent to the model and what came back, for later inspection (/runs/<key>)."""
        self.runs.append({
            "at": _now(), "step": step, "model": model, "ok": ok,
            "prompt": prompt[:MAX_RUN_PROMPT], "output": output[-MAX_RUN_OUTPUT:],
            "output_truncated": len(output) > MAX_RUN_OUTPUT,
        })

    def add_tokens(self, sent: int, received: int, model: str = "", step: str = "", **extra) -> None:
        self.tokens["sent"] += sent
        self.tokens["received"] += received
        # One entry per model call (failed and fallback calls too): the cost record is built from these.
        # `extra` carries optional detail, e.g. backend="opencode" or the backend's own reported cost.
        self.calls.append({"step": step, "model": model, "sent": sent, "received": received, **extra})

    def add_aider(self, out: str, model: str = "", step: str = "") -> None:
        u = parse_aider_usage(out)
        self.add_tokens(u["sent"], u["received"], model=model, step=step,
                        cache_write=u["cache_write"], cache_hit=u["cache_hit"], reported_cost=u["cost"])

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
        d["runs"] = len(d["runs"])  # /jobs shows only the count; the text is at /runs/<key>
        d["calls"] = len(d["calls"])  # per-call detail and cost are at /usage/<key>
        d.pop("delivery", None)
        return d


class JobStuck(Exception):
    """A child process was stopped because its job made no progress for too long (see Job.stop_stuck)."""


def kill_group(proc) -> None:
    """Kill a process started with start_new_session=True together with everything it started."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_process(cmd: list[str], *, cwd, timeout: float, job: "Job | None" = None, env: dict | None = None):
    """subprocess.run(capture_output=True, text=True) for a job: every line printed counts as progress, and the
    watchdog can kill it (then JobStuck is raised). Raises subprocess.TimeoutExpired after `timeout`, like run()."""
    import subprocess

    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    out: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def drain(stream, into: list[str]) -> None:
        for line in stream:
            into.append(line)
            if job:
                job.progress()

    readers = [threading.Thread(target=drain, args=(proc.stdout, out["stdout"]), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, out["stderr"]), daemon=True)]
    for r in readers:
        r.start()
    if job:
        job.track(proc)
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        kill_group(proc)

    timer = threading.Timer(timeout, on_timeout)  # (a timer, not wait(timeout), which polls with time.sleep)
    timer.daemon = True
    timer.start()
    try:
        proc.wait()
    finally:
        timer.cancel()
        if job:
            job.untrack(proc)
        for r in readers:
            r.join(timeout=5)
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout, "".join(out["stdout"]), "".join(out["stderr"]))
    if job and job.was_stopped(proc):
        raise JobStuck(" ".join(cmd[:2]))
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out["stdout"]), "".join(out["stderr"]))


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
