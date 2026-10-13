"""Self-ping: the app calls its own public /tick every few minutes.

Why through the public URL: Render's free tier sleeps a service after ~15 minutes without *incoming* requests.
A call to our own https://...onrender.com address goes through Render's front door, so it counts as traffic
(keeps the service awake) and it runs the tick (recovery, reminders, merge and review checks) in one go.
It cannot wake a service that is already asleep, so an outside pinger is still a good extra.

The token travels in an Authorization header, not in the URL, so it never shows up in request logs.
"""
import logging
import threading

import httpx

log = logging.getLogger(__name__)


def tick_url(base: str) -> str:
    return f"{base.rstrip('/')}/tick"


def explain_failure(status: int | None) -> str:
    """Plain words for why our own /tick call failed (shown in the Slack alert)."""
    if status in (401, 403):
        return "The address answered but refused the token: TICK_TOKEN in the settings doesn't match."
    if status == 404:
        return "The address answered but has no /tick: check the service URL (RENDER_EXTERNAL_URL / SELF_PING_URL)."
    if status is None:
        return "The app could not reach its own address at all (it may be starting up, or the URL is wrong)."
    if status == 503:
        return "The tick itself failed (usually the Redis database can't be reached); the logs have the details."
    return f"The address answered with an unexpected error ({status}); the logs have the details."


class SelfPinger:
    def __init__(self, base_url: str, token: str, interval_seconds: int, get=None, timeout: float = 90.0,
                 alert_after: int = 3, notify=None):
        self.url = tick_url(base_url)
        self._token = token
        self.interval = max(30, int(interval_seconds))
        self.timeout = timeout
        self.alert_after = max(1, int(alert_after))
        self.notify = notify  # notify(text): tell the person (Slack). Called once per bad streak, once on recovery.
        self._get = get or (lambda url, timeout, headers: httpx.get(url, timeout=timeout, headers=headers, follow_redirects=True))
        self._stop = threading.Event()
        self.last_status: int | None = None
        self.failures = 0
        self.alerted = False

    def ping_once(self) -> int | None:
        """One call to our own /tick. Returns the HTTP status, None if it failed. Never raises."""
        try:
            r = self._get(self.url, self.timeout, {"Authorization": f"Bearer {self._token}"})
            status = r.status_code
        except httpx.TimeoutException:
            # Slower than the timeout: the tick is most likely still running (a long tick is not a failure).
            log.info("Self-ping: /tick is taking longer than %ss; it is probably still running", int(self.timeout))
            return None
        except Exception as e:  # network blip, still starting up, etc.: try again next time
            self.last_status = None
            self._failed(None, f"could not reach our own /tick ({type(e).__name__})")
            return None
        self.last_status = status
        if status == 200:
            log.info("Self-ping: /tick answered 200")
            self._recovered()
        else:
            self._failed(status, f"/tick answered {status}")
        return status

    def _failed(self, status: int | None, what: str) -> None:
        self.failures += 1
        log.warning("Self-ping: %s (%s in a row)", what, self.failures)
        if self.failures >= self.alert_after and not self.alerted:
            self.alerted = True
            self._tell(
                ":warning: *Background checks may not be running.* "
                f"The app's own tick call has failed {self.failures} times in a row. {explain_failure(status)} "
                "Until this is fixed, reminders, merge detection and recovery of interrupted jobs won't run "
                "(planning and coding still work)."
            )

    def _recovered(self) -> None:
        was_alerted = self.alerted
        self.failures, self.alerted = 0, False
        if was_alerted:
            self._tell(":white_check_mark: Background checks are running again.")

    def _tell(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify(text)
        except Exception:
            log.warning("Self-ping: could not send the alert to Slack", exc_info=True)

    def run(self) -> None:
        # The startup tick has just run, so the first ping waits one full interval.
        while not self._stop.wait(self.interval):
            self.ping_once()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="self-ping", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
