"""Retries for flaky network calls: an HTTP client that retries, and a helper for git/Aider commands."""
import logging
import time

import httpx

log = logging.getLogger(__name__)

IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
RETRY_STATUS_ALWAYS = {429}  # "slow down": the request was not processed, so any method may repeat it
RETRY_STATUS_IDEMPOTENT = {500, 502, 503, 504}
MAX_WAIT = 30.0

# Git/network failures worth another try (anything else, like "branch not found", is not).
TRANSIENT_GIT = (
    "could not resolve host", "timed out", "connection reset", "connection refused", "remote end hung up",
    "rpc failed", "early eof", "the requested url returned error: 5", "returned error: 429", "tls", "unable to access",
)


def is_transient_git_error(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in TRANSIENT_GIT)


def wait_for(attempt: int, retry_after: str | None, base: float) -> float:
    """Seconds to wait before retry number `attempt` (1-based): Retry-After if sent, else base * 2^(n-1)."""
    try:
        if retry_after:
            return min(float(retry_after), MAX_WAIT)
    except ValueError:
        pass
    return min(base * 2 ** (attempt - 1), MAX_WAIT)


class RetryingClient(httpx.Client):
    """httpx.Client that retries connection problems, 429s and (for safe methods) 5xx and timeouts.

    A POST is never repeated after a timeout or a 5xx, because it may already have been applied
    (that would open two pull requests or post a comment twice).
    """

    def __init__(self, *args, tries: int = 3, backoff: float = 1.0, sleep=time.sleep, **kwargs):
        super().__init__(*args, **kwargs)
        self._tries, self._backoff, self._sleep = tries, backoff, sleep

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        safe = request.method in IDEMPOTENT
        for attempt in range(1, self._tries + 1):
            last = attempt == self._tries
            try:
                resp = super().send(request, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # never reached the server: safe for any method
                if last:
                    raise
                log.warning("%s %s: %s; retry %d", request.method, request.url.path, type(e).__name__, attempt)
                self._sleep(wait_for(attempt, None, self._backoff))
                continue
            except httpx.TransportError as e:  # may have reached the server
                if last or not safe:
                    raise
                log.warning("%s %s: %s; retry %d", request.method, request.url.path, type(e).__name__, attempt)
                self._sleep(wait_for(attempt, None, self._backoff))
                continue
            retry = resp.status_code in RETRY_STATUS_ALWAYS or (safe and resp.status_code in RETRY_STATUS_IDEMPOTENT)
            if not retry or last:
                return resp
            log.warning("%s %s -> %s; retry %d", request.method, request.url.path, resp.status_code, attempt)
            wait = wait_for(attempt, resp.headers.get("Retry-After"), self._backoff)
            resp.close()
            self._sleep(wait)
        return resp  # pragma: no cover
