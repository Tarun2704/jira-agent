"""The plan-first workflow: ticket -> plan in Slack -> chat -> approval -> coding agent.

This module owns the state machine. main.py only wires HTTP endpoints and the job queue to it.

    planning --> awaiting_reply <--> revising        (you chat in the Slack thread)
                      |
                      v  [Proceed]
                  confirming --[Go back]--> awaiting_reply
                      |
                      v  [Yes, start now]
                  executing --> done | no_changes | failed

Jobs (planning and executing) hold a short-lived Redis lease and refresh it while they run. A
session that should be running but has no lease means the process died; /tick re-queues it.
"""
import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager

from app import slack_client as sc
from app.agent import DELIVERY_TRAILER, CodingAgent
from app.errors import kind_of
from app.config import Settings
from app.costs import make_entry, model_prices, money, summarize
from app.friendly import explain, reference
from app.github_client import GitHubClient
from app.jira_client import JiraClient, mention
from app.jobs import Job
from app.planner import Planner, approved_plan_text, build_history, chat_style
from app.store import (
    AWAITING_REPLY, CANCELLED, CONFIRMING, DELIVERED, DONE, EXECUTING, PLANNING, REVISING,
    RUNNING, TERMINAL, WAITING, Session, SessionStore,
)

log = logging.getLogger(__name__)

PLAN_JOB, EXECUTE_JOB = "plan", "execute"

# Pure thank-yous to a delivered ticket don't start a new round.
THANKS = {"thanks", "thank you", "thx", "ty", "ok", "okay", "great", "done", "lgtm", "cool", "nice", "perfect", "got it", "good", "awesome"}
COMMANDS = {"tickets", "ticket", "open", "list", "status", "help", "/tickets"}
# "retry", "can u try again", "please run it again with the same plan": nothing but a request to run the approved plan
# again. Anything else ("try again but use src/") goes to the planner, which may change the plan.
_RETRY_WORDS = {"retry", "rerun", "re-run", "again", "try"}
_RETRY_FILLER = {"can", "could", "you", "u", "pls", "plz", "please", "it", "just", "now", "run", "the", "same", "plan",
                 "with", "code", "changes", "change", "one", "more", "time", "lets", "let's", "ok", "okay", "go", "ahead"}


def is_retry_request(text: str) -> bool:
    words = re.findall(r"[a-z'-]+", text.lower())
    return bool(words) and set(words) <= _RETRY_WORDS | _RETRY_FILLER and bool(set(words) & {"retry", "rerun", "re-run", "again"})
_KEY_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*-\d+)\b\s*[:,\-\u2013\u2014]?\s*(.*)$", re.S)


def _run_later(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    h = m // 60
    return f"{h} h ago" if h < 48 else f"{h // 24} days ago"


class Workflow:
    def __init__(
        self, settings: Settings, store: SessionStore, slack: sc.SlackClient, submit, jira_factory=None, github_factory=None
    ):
        """`submit(kind, key) -> bool` queues a plan/execute job (False if one is already queued)."""
        self.s = settings
        self.store = store
        self.slack = slack
        self.submit = submit
        self._jira = jira_factory or (lambda: JiraClient(settings))
        self._github = github_factory or (lambda: GitHubClient(settings))
        self._sleep = time.sleep  # (tests replace it)
        self._later = _run_later  # (tests replace it)
        self.busy = lambda key: False  # is a job for this ticket queued or running in this process? (set by main)
        self.waiting_for = lambda key: []  # what its queued job waits for, e.g. ["CA-21's coding run"] (set by main)
        self.deferred_triggers: set[str] = set()  # tickets whose start failed (Redis down); retried on the next tick
        self._tick_guard = threading.Lock()  # one tick at a time in this process (the Redis lock covers other instances)

    # ---- helpers -------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        for secret in (self.s.github_token, self.s.slack_bot_token, self.s.jira_api_token, self.s.redis_url):
            if secret and len(secret) > 6:
                text = text.replace(secret, "***")
        return text

    def _tag(self, key: str, text: str) -> str:
        """With several tickets open, start the message with its ticket key so it's clear which one it is."""
        try:
            if key in text or len(self.store.active_keys()) < 2:
                return text
        except Exception:
            return text
        return f"*{key}* · {text}"

    def _post_reliably(self, channel: str, text: str, blocks: list | None = None, thread_ts: str = "", key: str = "") -> dict | None:
        """Post to Slack: three tries with a short wait, then once as plain text. None if all fail; the message is
        then kept in Redis and sent by the watchdog (or /tick) once Slack answers again."""
        for delay in (0, 0.5, 1.5):
            if delay:
                self._sleep(delay)
            try:
                return self.slack.post(text, channel=channel, blocks=blocks, thread_ts=thread_ts)
            except Exception:
                log.warning("Slack post failed (will retry)", exc_info=True)
        if blocks:
            try:
                return self.slack.post(text, channel=channel, thread_ts=thread_ts)
            except Exception:
                log.warning("Slack post failed even as plain text", exc_info=True)
        if channel:
            try:
                self.store.queue_slack({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts,
                                        "key": key, "at": time.time(), "tries": 0})
                log.warning("Slack unreachable: message queued for later")
            except Exception:
                log.warning("Could not queue the Slack message either; it is lost", exc_info=True)
        return None

    def flush_slack(self) -> int:
        """Send queued Slack messages, oldest first, each marked as delayed. Stops at the first failure (Slack is
        still down) and keeps the rest for next time. Returns how many were sent."""
        try:
            pending = self.store.take_slack()
        except Exception:
            log.warning("Could not read the queued Slack messages", exc_info=True)
            return 0
        sent = 0
        for i, m in enumerate(pending):
            if m.get("plan_version") and not self._plan_still_current(m):
                continue  # a newer plan replaced it meanwhile (or it was approved): never post a stale Proceed button
            note = f":hourglass: _Delayed: Slack couldn't be reached at {time.strftime('%H:%M UTC', time.gmtime(m['at']))}._"
            blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}, *m["blocks"]] if m.get("blocks") else None
            try:
                ref = self.slack.post(m["text"] if blocks else f"{note}\n{m['text']}", channel=m["channel"], blocks=blocks, thread_ts=m.get("thread_ts", ""))
            except Exception:
                rest = [dict(m, tries=m.get("tries", 0) + 1)] + pending[i + 1:]
                if rest[0]["tries"] >= 60:  # about an hour of watchdog runs
                    log.error("Giving up on a queued Slack message for %s after %s tries", m.get("key"), rest[0]["tries"])
                    rest = rest[1:]
                try:
                    self.store.requeue_slack_first(rest)
                except Exception:
                    log.warning("Could not re-queue %d Slack message(s); they are lost", len(rest), exc_info=True)
                break
            sent += 1
            if m.get("key") and ref:  # replies under the delayed message still find their ticket
                self.store.map_thread(ref["channel"], ref["ts"], m["key"])
            if m.get("plan_version") and ref:
                self._plan_delivered(m["key"], m["plan_version"], ref)
        return sent

    def _plan_still_current(self, m: dict) -> bool:
        s = self.store.get(m.get("key", ""))
        return bool(s and s.plan_version == m["plan_version"] and s.status in WAITING | {REVISING})

    def _plan_delivered(self, key: str, version: int, ref: dict) -> None:
        """A plan held back by a Slack outage was posted: its message becomes the plan message (and the thread)."""
        def apply(x: Session):
            if x.plan_version != version:
                return False
            x.plan_msg_ts = ref["ts"]
            if not x.slack_thread_ts:
                x.slack_channel, x.slack_thread_ts = ref["channel"], ref["ts"]

        self.store.update(key, apply)

    def _post_plan(self, s: Session, text: str, blocks: list, thread_ts: str, version: int) -> dict | None:
        """Post a plan. If Slack can't be reached it is kept and sent once it can (see flush_slack): the plan isn't
        lost, and the ticket waits for approval as usual. None then. If Slack answers with an error, that's raised."""
        for delay in (0, 0.5, 1.5):
            if delay:
                self._sleep(delay)
            try:
                return self.slack.post(text, channel=s.slack_channel, blocks=blocks, thread_ts=thread_ts)
            except sc.SlackError:
                raise  # Slack answered and refused it: not an outage, and waiting won't fix it
            except Exception:
                log.warning("Slack post of the plan failed (will retry)", exc_info=True)
        self.store.queue_slack({"channel": s.slack_channel or self.s.slack_user_id, "text": text, "blocks": blocks,
                                "thread_ts": thread_ts, "key": s.issue_key, "at": time.time(), "tries": 0,
                                "plan_version": version})
        log.warning("Slack unreachable: the plan for %s (v%s) is queued and will be sent when Slack answers", s.issue_key, version)
        return None

    def requeue_orphans(self) -> list[str]:
        """Tickets in a running state whose job no longer holds the lease (the process died): queue the job again."""
        requeued = []
        for key in self.store.active_keys():
            s = self.store.get(key)
            if s and s.status in RUNNING and not self.store.has_lease(key):
                log.warning("Session %s is %s but no job holds its lease; re-queuing", key, s.status)
                self.submit(EXECUTE_JOB if s.status == EXECUTING else PLAN_JOB, key)
                requeued.append(key)
        return requeued

    def watchdog(self) -> dict:
        """Run by the app every WATCHDOG_SECONDS: a crashed job restarts within about LEASE_SECONDS, without waiting
        for /tick, and Slack messages held back by an outage are sent. Reminders and PR checks stay with /tick."""
        return {"requeued": self.requeue_orphans(), "slack_sent": self.flush_slack()}

    def _say(self, s: Session, text: str, blocks: list | None = None, important: bool = False, thread_ts: str = "") -> dict | None:
        """Post a new message in the main DM chat (never hidden in a thread unless `thread_ts` says so). Never raises.

        `important` messages (results, failures) that still can't be delivered are written to the Jira
        ticket instead, so the outcome isn't lost.
        """
        text = self._tag(s.issue_key, text)
        ref = self._post_reliably(s.slack_channel, text, blocks, thread_ts=thread_ts, key=s.issue_key)
        if ref is None and important:
            self._jira_comment(s.issue_key, "🤖 (I couldn't reach Slack, so this is posted here instead)\n" + re.sub(r"[`*]", "", text))
        self._map(ref, s.issue_key)
        return ref

    def _map(self, ref: dict | None, key: str) -> None:
        """Replies in a thread under this message belong to `key` (else, with two tickets open, the owner is asked
        which ticket they mean). Never raises."""
        if not ref:
            return
        try:
            self.store.map_thread(ref["channel"], ref["ts"], key)
        except Exception:
            log.warning("Could not remember the ticket of a Slack message", exc_info=True)

    def _save_runs(self, job: Job, kind: str) -> None:
        """Keep the prompts and outputs of this job's Aider runs in Redis. Never raises."""
        if not job.runs:
            return
        try:
            entries = [
                {**r, "job": job.id, "kind": kind, "prompt": self._scrub(r["prompt"]), "output": self._scrub(r["output"])}
                for r in job.runs
            ]
            self.store.append_runs(job.issue_key, entries)
        except Exception:
            log.warning("Could not save the run log for %s", job.issue_key, exc_info=True)

    def _save_cost(self, job: Job, kind: str) -> None:
        """Keep what this job's model calls used and cost, under the ticket's current round. Never raises."""
        if not job.calls:
            return
        try:
            cur = self.store.get(job.issue_key)  # re-read: planning may just have started a new round
            entry = make_entry(job.id, kind, cur.round_no if cur else 0, job.calls, model_prices(self.s.model_prices))
            self.store.append_cost(job.issue_key, entry)
        except Exception:
            log.warning("Could not save the cost record for %s", job.issue_key, exc_info=True)

    def _cost_line(self, key: str, round_no: int) -> str:
        """One short line for the delivery message: this round's use and the ticket total. "" if unknown/off."""
        if not self.s.show_cost_in_slack:
            return ""
        try:
            summary = summarize(self.store.get_costs(key))
        except Exception:
            return ""
        this = next((r for r in summary["rounds"] if r["round"] == round_no), None)
        if not this or not this["requests"]:
            return ""
        total = summary["total"]
        if this["cost_usd"] is None:
            return f"_AI use this round: {this['requests']} requests, {(this['sent'] + this['received']) // 1000}k tokens._"
        line = f"_Cost this round: about {money(this['cost_usd'])}"
        if len(summary["rounds"]) > 1 and total["cost_usd"] is not None:
            line += f" (ticket total {money(total['cost_usd'])})"
        return line + "._"

    def _jira_comment(self, key: str, body: str, tries: int = 0) -> None:
        """Comment on the ticket. If Jira refuses, keep the comment and retry on the next tick."""
        try:
            self._jira().add_comment(key, body)
        except Exception as e:
            log.warning("Could not comment on %s", key, exc_info=True)
            try:
                self.store.queue_jira_comment(key, body, tries + 1)
            except Exception:
                log.warning("Could not queue the Jira comment either", exc_info=True)
            if tries == 0:  # tell you once, not on every retry
                s = self.store.get(key) if self.store else None
                if s and s.slack_channel:
                    self._post_reliably(s.slack_channel, f":warning: *{key}*: couldn't comment on the Jira ticket. I'll retry shortly.")

    def _already_shipped(self, s: Session, st: dict) -> dict | None:
        """The delivery of this run if its commit is already on GitHub with an open PR (the job was cut off after the
        push), else None. Any doubt (no stamp, GitHub unreachable) means None: the run then goes ahead as usual."""
        if not s.delivery_id:
            return None
        try:
            gh = self._github()
            if st["mode"] == "continue":
                pr = {"number": st["pr_number"], "url": st["pr_url"], "branch": st["branch"]}
            elif st["mode"] == "fresh":
                pr = gh.open_pr_for_branch(st["branch"])
            else:
                url = gh.find_open_pr_for_issue(s.issue_key)
                pr = gh.get_pr(gh.pr_number(url)) if url else None
                pr = pr and {"number": pr["number"], "url": pr["url"], "branch": pr["branch"]}
            if not pr:
                return None
            message = gh.branch_head_message(pr["branch"]) or ""
        except Exception:
            log.warning("Could not check whether %s was already pushed; running it", s.issue_key, exc_info=True)
            return None
        if f"{DELIVERY_TRAILER}{s.delivery_id}" not in message:
            return None
        return {"branch": pr["branch"], "pr_number": pr["number"], "pr_url": pr["url"], "diff": "",
                "summary": message.split("\n", 1)[0].split(": ", 1)[-1][:300], "deleted": list(s.delete_files)}

    def _retry_when_lease_lapses(self, kind: str, key: str) -> None:
        """A job that found the ticket's lease taken tries again once that lease would have lapsed.

        The holder may be a process that died with it (a deploy, a crash, a restart while the result was still being
        posted). Without this, the ticket waited for the next /tick (~10 minutes; never without a pinger) while the
        owner's message sat on "Looking into it...". If the holder is alive and still working, the retry finds the
        lease refreshed and schedules itself again, until the session no longer needs this job."""
        def again():
            s = self.store.get(key)
            if s and (s.status == EXECUTING if kind == EXECUTE_JOB else s.status in (PLANNING, REVISING)):
                self.submit(kind, key)

        self._later(self.store.lease_ttl(key) + 2, again)

    @contextmanager
    def leased(self, key: str):
        """Hold the job lease for `key`, refreshing it in the background. Yields False if taken."""
        owner = uuid.uuid4().hex
        ttl = self.s.lease_seconds
        if not self.store.acquire_lease(key, owner, ttl):
            yield False
            return
        stop = threading.Event()

        def beat():
            while not stop.wait(max(ttl / 3, 1)):
                try:
                    if not self.store.refresh_lease(key, owner, ttl):
                        return
                except Exception:
                    log.warning("Lease refresh failed for %s", key, exc_info=True)

        t = threading.Thread(target=beat, name=f"lease-{key}", daemon=True)
        t.start()
        try:
            yield True
        finally:
            stop.set()
            try:
                self.store.release_lease(key, owner)
            except Exception:
                log.warning("Could not release lease for %s", key, exc_info=True)

    # ---- entry points: Jira ------------------------------------------------------------

    def on_trigger(self, key: str) -> bool:
        """A ticket got the trigger label. False if a plan for it is already in progress."""
        s = self.store.get(key)
        if s and s.status == DELIVERED:  # re-triggered after delivery: a follow-up round, always with a plan
            return self._start_followup(key, "jira", "Ticket updated in Jira and re-triggered (see its current description).", True)
        if s and s.status == AWAITING_REPLY and not s.plan and s.result:
            # the first plan failed (an error was sent to you): re-adding the label tries again
            retried = self.store.transition(key, {AWAITING_REPLY}, PLANNING, result="")
            if retried is None:
                return False
            self.submit(PLAN_JOB, key)
            return True
        if self.store.create(key) is None:  # (a completed ticket starts a new session that keeps its earlier rounds)
            return False
        self.submit(PLAN_JOB, key)
        return True

    def defer_trigger(self, key: str) -> None:
        """Starting failed (storage down): remember the ticket and try again on the next tick."""
        self.deferred_triggers.add(key)

    def _start_followup(self, key: str, source: str, text: str, force_plan: bool) -> bool:
        started = []

        def apply(x: Session):
            if x.status != DELIVERED:
                return False
            x.followup, x.force_plan, x.round_source, x.round_request = True, force_plan, source, text
            x.add_message("user", text)
            # Nobody wrote in Slack (a button, a PR review, a Jira change): the plan goes to the main chat, not into
            # whatever thread the last conversation happened to be in.
            x.reply_to = ""
            x.status = REVISING
            x.reminded = False
            x.touch()
            started.append(True)

        self.store.update(key, apply)
        if not started:
            return False
        self.submit(PLAN_JOB, key)
        return True

    def on_description_changed(self, key: str) -> str:
        """Jira says the description of `key` changed. After delivery, ask whether to plan a follow-up from it."""
        asked = []

        def apply(x: Session):
            if x.status != DELIVERED or time.time() - x.desc_prompt_at < 600:
                return False
            x.desc_prompt_at = time.time()
            asked.append(x.title)

        s = self.store.update(key, apply)
        if not asked:
            return "ignored"
        self._say(s, f"The description of {key} was updated in Jira.", sc.description_blocks(key, asked[0]))
        return "asked"

    # ---- planning jobs -----------------------------------------------------------------

    def run_plan_job(self, job: Job) -> None:
        key = job.issue_key
        with self.leased(key) as ok:
            if not ok:
                log.info("Plan job for %s skipped: another worker holds the lease; trying again when it lapses", key)
                job.finish("skipped", "lease held elsewhere")
                self._retry_when_lease_lapses(PLAN_JOB, key)
                return
            s = self.store.get(key)
            if not s or s.status not in (PLANNING, REVISING):
                log.info("Plan job for %s skipped: session is %s", key, s.status if s else "missing")
                job.finish("skipped", f"session is {s.status if s else 'missing'}")
                return
            try:
                outcome = self._plan(job, s)
                job.finish("succeeded" if outcome == "posted" else "skipped", outcome)
            except Exception as e:
                error = self._scrub(str(e))
                kind = kind_of(error, getattr(e, "code", None))
                log.exception("Planning failed for %s (%s)", key, kind.code)
                if kind.admin:
                    log.error("ADMIN: %s needs fixing [%s]: %s", key, kind.code, error[:300])
                job.finish("failed", error[-500:], code=kind.code)
                if kind.code == "stuck" and self._again_after_stuck(key, "the plan"):
                    return  # (still planning: the job is queued again as soon as this one has ended)
                self._plan_failed(s, error, job.id, kind.code)
            finally:
                self._save_runs(job, "plan")
                self._save_cost(job, "plan")

    def _again_after_stuck(self, key: str, what: str) -> bool:
        """A job stopped because it was stuck: run it once more by itself (the session keeps its state, so the next
        job picks it up: main's resume_if_needed for plans, the watchdog's requeue for runs). False if it already was."""
        again = []

        def apply(x: Session):
            if x.stuck_retried:
                return False
            x.stuck_retried = True
            again.append(True)

        s = self.store.update(key, apply)
        if not again:
            return False
        self._say(s, f":hourglass: I got stuck on {what} for *{key}* (nothing happened for several minutes), so I stopped "
                     "it and I'm trying once more.", thread_ts=s.reply_to)
        self._later(3, lambda: self.submit(PLAN_JOB if s.status in (PLANNING, REVISING) else EXECUTE_JOB, key))
        return True

    def _round_context(self, issue, s: Session) -> str:
        """New input for a follow-up round: human comments on the Jira ticket since the last round."""
        if not s.rounds:
            return ""
        try:
            comments = self._jira().human_comments_since(s.issue_key, s.rounds[-1].get("finished_at", 0.0))
        except Exception:
            log.warning("Could not read Jira comments for %s", s.issue_key, exc_info=True)
            return ""
        if not comments:
            return ""
        return "Comments on the Jira ticket since the last round:\n" + "\n".join(f"- {c['author']}: {c['body'][:500]}" for c in comments[-8:])

    def _plan_branch(self, s: Session) -> str | None:
        """For a follow-up on an open PR, plan against that PR's branch so the planner sees the earlier changes."""
        if not (s.rounds and s.pr_number):
            return None
        try:
            pr = self._github().get_pr(s.pr_number)
            return pr["branch"] if pr["state"] == "open" else None
        except Exception as e:
            log.warning("Could not check PR #%s for %s", s.pr_number, s.issue_key, exc_info=True)
            self._say(s, f":warning: I couldn't check the state of PR #{s.pr_number}, so I looked at the main branch instead. The plan may miss the earlier changes.")
            return None

    def _plan(self, job: Job, s: Session) -> str:
        key = s.issue_key
        with job.step("fetch_ticket"):
            issue = self._jira().get_issue(key)
        if self.s.trigger_label and s.round_source == "jira" and self.s.trigger_label not in issue.labels:
            if s.rounds:  # a follow-up from Jira whose label was taken off again: back to waiting for follow-ups
                self.store.transition(key, {PLANNING, REVISING}, DELIVERED, followup=False, force_plan=False)
            else:
                self.store.transition(key, {PLANNING, REVISING}, CANCELLED, result="trigger label removed")
            return f"label {self.s.trigger_label!r} removed"

        first = not s.slack_thread_ts
        handled = {"n": 0}

        def mark(x: Session):
            handled["n"] = len(x.messages)
            x.handled_upto = len(x.messages)
            x.title = issue.summary
            x.reporter_account_id = issue.reporter_account_id

        s = self.store.update(key, mark) or s
        chatting = chat_style(s)  # can the planner answer without a plan? (a chat about a plan, or a delivered ticket)
        new_round = s.followup  # this reply may start a new round
        context = self._round_context(issue, s)
        branch = self._plan_branch(s)

        result = Planner(self.s, job).run(issue, s, context, branch)

        def remember_reply(x: Session):
            if result.reply:  # the answer joins the history, ahead of anything that arrived meanwhile
                x.messages.insert(x.handled_upto, {"role": "agent", "text": result.reply, "at": time.time()})
                x.handled_upto += 1

        def warn():
            for w in result.warnings:
                self._say(s, f":warning: {w}", thread_ts=s.reply_to)

        if chatting and not result.changed:
            # Just a conversation: answer where you wrote, leave the plan and its version alone.
            ref = None
            with job.step("slack_post"):
                if result.show and not new_round:  # "show me the plan again": the same plan, as a fresh message below your own
                    ref = self._post_plan(
                        s, f"Plan for {key}: {s.title}", sc.plan_blocks(key, s.title, s.plan, s.plan_version, bool(s.questions), round_no=s.round_no),
                        s.reply_to, s.plan_version,
                    )
                    if ref:
                        self.store.map_thread(ref["channel"], ref["ts"], key)
                        self._supersede(s, s.plan_version, again=True)
                self._answer(s, handled["n"], result.reply)
                warn()

            def answered(x: Session):
                if ref:
                    x.plan_msg_ts = ref["ts"]
                x.stuck_retried = False
                remember_reply(x)
                if x.followup:  # a question after delivery: it stays delivered, nothing new starts
                    x.followup = x.force_plan = False
                    x.status = REVISING if x.unhandled else DELIVERED
                else:
                    x.status = REVISING if x.unhandled else AWAITING_REPLY
                x.reminded = False
                x.result = ""
                x.touch()

            self.store.update(key, answered)
            return "posted"

        round_no = (max(r.get("n", 0) for r in s.rounds) + 1) if new_round and s.rounds else s.round_no
        version = 1 if new_round else s.plan_version + 1
        text = f"{'Follow-up plan' if round_no > 1 else 'Plan'} for {key}: {issue.summary}"
        blocks = sc.plan_blocks(key, issue.summary, result.body, version, bool(result.questions), round_no=round_no)
        with job.step("slack_post"):
            # Answered first, so the new plan really is below the answer. Only when the planner said something or the
            # owner is waiting on a "Looking into it..." for what they wrote: a round started by a button, a PR review
            # or Jira gets just the plan (it would otherwise be followed by a stray "Taken into account").
            said = result.reply or "Taken into account in the plan."
            if new_round:
                said += f" The follow-up plan (round {round_no}) is below."
            elif not first:
                said += f" The updated plan (v{version}) is below."
            if result.reply or self._acked(key, handled["n"]):
                self._answer(s, handled["n"], said)
            # (the first plan opens the DM; a new version goes right below your message, wherever you wrote it)
            ref = self._post_plan(s, text, blocks, "" if first else s.reply_to, version)
            if ref:
                self.store.map_thread(ref["channel"], ref["ts"], key)  # replies under this plan find their ticket
            if first and ref:
                channel, thread_ts = ref["channel"], ref["ts"]
            else:  # (held back by a Slack outage: flush_slack fills these in when it is posted)
                channel, thread_ts = s.slack_channel or self.s.slack_user_id, s.slack_thread_ts
            if not first:
                if not new_round:  # (the previous round's plan message is already locked as approved)
                    self._supersede(s, version)
            warn()

        def finish(x: Session):
            x.plan, x.questions, x.plan_version, x.files = result.body, result.questions, version, result.files
            x.delete_files = list(result.delete_files)
            x.slack_channel, x.slack_thread_ts, x.plan_msg_ts = channel, thread_ts, ref["ts"] if ref else ""
            x.stuck_retried = False
            if new_round:
                x.round_no, x.followup, x.force_plan = round_no, False, False
                x.pr_notified = ""
            remember_reply(x)
            if result.questions:  # keep the questions in the history, ahead of any reply that arrived meanwhile
                x.messages.insert(x.handled_upto, {"role": "agent", "text": result.questions, "at": time.time()})
                x.handled_upto += 1
            x.status = REVISING if x.unhandled else AWAITING_REPLY
            x.reminded = False
            x.result = ""
            x.touch()

        self.store.update(key, finish)
        if first and not ref:
            self._jira_comment(key, "🤖 I've drafted a plan, but Slack can't be reached right now; it will be sent there as soon as "
                                    "it can. Waiting for approval before touching any code.")
        elif first:
            self._jira_comment(key, "🤖 I've drafted a plan and sent it to Slack. Waiting for approval before touching any code.")
        elif new_round:
            asked = f" Request: {s.round_request[:300]}" if s.round_request else ""
            self._jira_comment(key, f"🤖 Follow-up round {round_no} ({s.round_source}): plan sent to Slack, waiting for approval.{asked}")
        return "posted"

    def _answer(self, s: Session, upto: int, text: str) -> None:
        """Put `text` where you wrote: it replaces the "Looking into it..." message of your newest
        message handled this round; older ones just point to it. With no such message, post in the thread."""
        fresh = self.store.get(s.issue_key) or s
        done = [a for a in fresh.pending_acks if a.get("i", 0) < upto]
        left = [a for a in fresh.pending_acks if a.get("i", 0) >= upto]
        text = self._tag(s.issue_key, text or "Done.")
        try:
            if not done:
                self._say_in(fresh, text)
            else:
                for a in done[:-1]:
                    self.slack.update(a["channel"], a["ts"], "Answered below.", sc.superseded_blocks("_Answered in my next reply._"))
                last = done[-1]
                self.slack.update(last["channel"], last["ts"], text, sc.reply_blocks(text))
        except Exception:
            log.warning("Could not post the answer for %s", s.issue_key, exc_info=True)
        self.store.update(s.issue_key, lambda x: setattr(x, "pending_acks", [a for a in x.pending_acks if a.get("i", 0) >= upto]))

    def _acked(self, key: str, upto: int) -> bool:
        """Is a "Looking into it..." waiting for an answer to one of the first `upto` messages?"""
        fresh = self.store.get(key)
        return bool(fresh and any(a.get("i", 0) < upto for a in fresh.pending_acks))

    def _say_in(self, s: Session, text: str) -> None:
        # exactly where the owner wrote: main chat if they wrote there (never into the plan's thread instead)
        self._map(self.slack.post(text, channel=s.slack_channel, blocks=sc.reply_blocks(text), thread_ts=s.reply_to), s.issue_key)

    def _supersede(self, s: Session, new_version: int, again: bool = False) -> None:
        """Old plans stay readable; they just lose their Proceed button and say which version replaced them."""
        if not (s.slack_channel and s.plan_msg_ts):
            return
        note = (
            ":arrows_counterclockwise: _Posted again below. Kept here for reference._" if again
            else f":arrows_counterclockwise: _Superseded by v{new_version}. Kept here for reference._"
        )
        try:
            blocks = sc.plan_blocks(s.issue_key, s.title, s.plan, s.plan_version, False, note=note, round_no=s.round_no)
            self.slack.update(s.slack_channel, s.plan_msg_ts, f"Plan for {s.issue_key} (v{s.plan_version}, superseded)", blocks)
        except Exception:
            log.warning("Could not mark old plan message superseded for %s", s.issue_key, exc_info=True)

    def _plan_failed(self, s: Session, error: str, job_id: str = "", code: str | None = None) -> None:
        key = s.issue_key
        fresh = self.store.get(key) or s
        again = "Ask again to retry." if fresh.followup else "Reply here to try again."
        # an error answers what you just wrote: in the thread if you wrote in a thread, in the main chat if you wrote there
        ref = self._say(fresh, f":x: I couldn't produce the plan for {key}. {explain(error, code)}\n{again}{reference(job_id)}", important=True, thread_ts=fresh.reply_to)
        thread = fresh.slack_thread_ts or (ref or {}).get("ts", "")
        channel = fresh.slack_channel or (ref or {}).get("channel", "")
        if ref and not fresh.slack_thread_ts:
            self.store.map_thread(channel, thread, key)

        for a in fresh.pending_acks:
            try:
                self.slack.update(a["channel"], a["ts"], "Couldn't process this.", sc.superseded_blocks("_I couldn't handle this one. See the error below._"))
            except Exception:
                log.warning("Could not close acknowledgement", exc_info=True)

        def fail(x: Session):
            x.pending_acks = []
            x.stuck_retried = False
            x.slack_channel, x.slack_thread_ts = channel, thread
            x.handled_upto = len(x.messages)
            if x.followup:  # the ticket stays delivered; nothing new started
                x.followup = x.force_plan = False
                x.status = DELIVERED
            else:
                x.status = AWAITING_REPLY
            x.result = error[-500:]
            x.touch()

        self.store.update(key, fail)
        self._jira_comment(key, f"🤖 I couldn't produce the plan. {explain(error, code)}{reference(job_id).replace('_', '')}")

    def resume_if_needed(self, key: str) -> None:
        """After a plan job: if you wrote again while it ran, plan again."""
        s = self.store.get(key)
        if s and s.status in (PLANNING, REVISING) and not self.store.has_lease(key):
            self.submit(PLAN_JOB, key)

    # ---- entry points: Slack ---------------------------------------------------------------

    def on_user_message(self, channel: str, thread_ts: str, user: str, text: str, message_ts: str = "") -> str:
        """A DM from you. Returns a short outcome string (for logs and tests)."""
        text = (text or "").strip()
        if user != self.s.slack_user_id or not text:
            log.warning(
                "Ignored a Slack message from user %r (expected SLACK_USER_ID=%r, text empty=%s)",
                user, self.s.slack_user_id, not text,
            )
            return "ignored"
        if text.lower().strip(" ?!.") in COMMANDS:
            self._show_tickets(channel, thread_ts)
            return "tickets"

        active = self.store.active_keys()
        key = self.store.key_for_thread(channel, thread_ts) if thread_ts else None
        if not key:  # "CA-7: also add tests" goes to CA-7, whatever else is waiting
            m = _KEY_PREFIX.match(text)
            if m and m.group(1).upper() in active:
                key, rest = m.group(1).upper(), m.group(2).strip()
                if not rest:
                    self._reply_loose(channel, thread_ts, f"What should I tell {key}? Write it after the ticket key, e.g. `{key}: also add tests`.")
                    return "empty_after_key"
                text = rest
            elif m:
                old = self.store.get(m.group(1).upper())
                if old and old.status in TERMINAL:
                    self._reply_loose(channel, thread_ts, self._completed_text(old))
                    return "completed"
        if not key:
            waiting = [k for k in active if (x := self.store.get(k)) and (x.status in WAITING or x.status == DELIVERED)]
            if len(waiting) == 1:
                key = waiting[0]
            elif not waiting:
                self._reply_loose(
                    channel, thread_ts,
                    "Nothing is waiting for a reply right now. Type `tickets` to see what's open.",
                )
                return "no_session"
            else:
                # Don't guess: keep the message and let you pick the ticket with one click.
                rows = [(k, (self.store.get(k) or Session(k)).title) for k in sorted(waiting)]
                self.store.stash(channel, message_ts or "0", {"text": text, "thread": thread_ts})
                self._reply_loose(
                    channel, thread_ts,
                    f"Which ticket is this about? ({', '.join(k for k, _ in rows)}) Press its button, or write `{rows[0][0]}: your message`.",
                    sc.pick_blocks(rows, message_ts or "0"),
                )
                return "ambiguous"
        return self._deliver(key, channel, thread_ts, text, message_ts)

    def _deliver(self, key: str, channel: str, thread_ts: str, text: str, message_ts: str = "") -> str:
        """Add your message to the ticket's chat and (re)start the planner."""
        if message_ts and not thread_ts:  # a thread started under your message belongs to this ticket too
            self.store.map_thread(channel, message_ts, key)
        before = self.store.get(key)
        if before and before.status == DELIVERED and text.lower().strip(" .!") in THANKS:
            self._reply_loose(channel, thread_ts, "\U0001F44D")
            return "thanks"
        if before and self._can_retry(before) and is_retry_request(text):
            return self._retry_failed_round(key, channel, "", said=text, thread_ts=thread_ts)
        # Where the answer goes: like a normal chat. A message in the main DM is answered in the
        # main DM; a message inside a thread is answered in that thread.
        anchor = thread_ts
        outcome = []
        index = {}

        def apply(x: Session):
            if x.status in TERMINAL or x.status == EXECUTING:
                outcome.append(x.status)
                return False
            x.add_message("user", text)
            index["i"] = len(x.messages) - 1
            x.reply_to = anchor
            x.touch()
            x.reminded = False
            if x.status == DELIVERED:  # any message to a delivered ticket: an answer, or a follow-up round
                outcome.append("followup")
                x.followup, x.force_plan, x.round_source, x.round_request = True, False, "slack", text
                x.status = REVISING
            elif x.status in (AWAITING_REPLY, CONFIRMING):
                outcome.append(f"revise:{x.status}")
                x.status = REVISING
            else:
                outcome.append("queued")  # planning/revising: the running job loops again at the end

        s = self.store.update(key, apply)
        if s is None:
            self._reply_loose(channel, thread_ts, f"I don't have an active session for {key}.")
            return "no_session"
        result = outcome[0]
        if result in TERMINAL or result == EXECUTING:
            msg = (
                f"{key} is already running, so the plan can't be changed now."
                if result == EXECUTING
                else self._completed_text(s)
            )
            self._reply_loose(channel, anchor, msg)
            return f"rejected:{result}"
        note = ":hourglass_flowing_sand: Looking into it..."
        if result == "revise:confirming":
            note = ":hourglass_flowing_sand: Looking into it... (I've paused the start-confirmation; press Proceed again when you're happy.)"
        ref = None
        try:
            ref = self.slack.post(self._tag(key, note), channel=channel, thread_ts=anchor)
            self.store.map_thread(ref["channel"], ref["ts"], key)
            self.store.update(key, lambda x: x.pending_acks.append({"channel": ref["channel"], "ts": ref["ts"], "i": index["i"], "at": time.time()}))
        except Exception:
            log.warning("Could not post the acknowledgement for %s", key, exc_info=True)
        self.submit(PLAN_JOB, key)  # a no-op while a plan job is in flight; resume_if_needed catches it
        queued = self._queued_note(key)
        if ref and queued:  # say why nothing happens for a while, on the same message
            text = self._tag(key, f"{note} {queued}")
            try:
                self.slack.update(ref["channel"], ref["ts"], text, sc.reply_blocks(text))
            except Exception:
                log.warning("Could not add the queue note for %s", key, exc_info=True)
        return result

    def _queued_note(self, key: str) -> str:
        """" (Waiting for CA-21's coding run to finish first: one job runs at a time.)", or "" if it can start now."""
        try:
            ahead = self.waiting_for(key)
        except Exception:
            return ""
        if not ahead:
            return ""
        return (f"_(Waiting for {', '.join(ahead[:3])}{' and more' if len(ahead) > 3 else ''} to finish first: "
                "one job runs at a time.)_")

    @staticmethod
    def _can_retry(s: Session) -> bool:
        return s.status == DELIVERED and bool(s.plan) and bool(s.rounds) and s.rounds[-1].get("outcome") == "failed"

    def _retry_failed_round(self, key: str, channel: str, message_ts: str, said: str = "", thread_ts: str = "") -> str:
        """Run the approved plan of a failed round again: no new plan, no new approval (it was approved already).

        The failed attempt is taken off the round list (its runs and costs stay saved), so the retry delivers exactly
        as the first attempt would have: same round number, same branch and PR rules."""
        def apply(x: Session):
            if not self._can_retry(x):
                return False
            x.rounds.pop()
            x.delivery_id = uuid.uuid4().hex[:12]
            if said:
                x.add_message("user", said)
                x.handled_upto = len(x.messages)
            x.status, x.followup, x.force_plan, x.result = EXECUTING, False, False, ""
            x.stuck_retried = False
            x.touch()

        before = self.store.get(key)
        s = self.store.update(key, apply)
        if not s or s.status != EXECUTING or not before or before.status == EXECUTING:
            self._reply_loose(channel, thread_ts, f"*{key}* has no failed run to retry right now.")
            return "ignored_retry"
        if message_ts:  # the button: it can't be pressed twice
            self._update(channel, message_ts, "Retrying.", f":repeat: _Retrying *{key}* with the approved plan..._")
        self.submit(EXECUTE_JOB, key)
        queued = self._queued_note(key)
        self._say(s, f":repeat: *Retrying {key}* with the approved plan (v{s.plan_version}). I'll post the result in the main chat."
                     + (f"\n{queued}" if queued else ""), thread_ts=thread_ts)
        self._jira_comment(key, "🤖 Retrying the approved plan after the previous attempt failed.")
        return "retrying"

    def _completed_text(self, s: Session) -> str:
        return (
            f":white_check_mark: *{s.issue_key}* is completed ({s.result or s.status}). To change something, create a new Jira ticket, "
            f"or update this ticket's description and add the `{self.s.trigger_label}` label again."
        )

    def _reply_loose(self, channel: str, thread_ts: str, text: str, blocks: list | None = None) -> None:
        try:
            self.slack.post(text, channel=channel, thread_ts=thread_ts, blocks=blocks)
        except Exception:
            log.warning("Could not reply in Slack", exc_info=True)
            if blocks:  # the rich version was refused: say it in plain text rather than say nothing
                try:
                    self.slack.post(text, channel=channel, thread_ts=thread_ts)
                except Exception:
                    log.warning("Could not reply in Slack (plain text either)", exc_info=True)

    def on_action(self, action_id: str, key: str, user: str, channel: str, message_ts: str) -> str:
        """A button click. `message_ts` is the message holding the clicked button."""
        if user != self.s.slack_user_id:
            log.warning("Ignored a button click from user %r (expected SLACK_USER_ID=%r)", user, self.s.slack_user_id)
            return "ignored"
        action_id = sc.action_name(action_id)
        if action_id == sc.ACTION_PICK:
            return self._picked(key, channel, message_ts)
        if action_id == sc.ACTION_SHOW:
            return self._show_plan(key, channel)
        if action_id in (sc.ACTION_REVIEW_PLAN, sc.ACTION_REVIEW_IGNORE):
            return self._review_choice(key, channel, message_ts, plan=action_id == sc.ACTION_REVIEW_PLAN)
        if action_id in (sc.ACTION_DESC_PLAN, sc.ACTION_DESC_IGNORE):
            if action_id == sc.ACTION_DESC_IGNORE:
                self._update(channel, message_ts, "Ignored.", f"_OK, ignoring the description change on *{key}*._")
                return "ignored_desc"
            started = self._start_followup(key, "jira", "The ticket description was updated in Jira.", True)
            self._update(channel, message_ts, "Planning.", f":hourglass_flowing_sand: _Planning a follow-up for *{key}* from the updated description..._" if started else "_This is no longer valid._")
            return "followup" if started else "ignored_desc"
        s = self.store.get(key)
        if not s:
            return "no_session"

        if action_id == sc.ACTION_RETRY:
            return self._retry_failed_round(key, channel, message_ts)

        if action_id == sc.ACTION_PROCEED:
            if not s.plan:
                return "no_plan"
            # (also while confirming: the plan may have been shown again, which took the earlier confirmation away)
            moved = self.store.transition(key, {AWAITING_REPLY, CONFIRMING}, CONFIRMING)
            if not moved:
                return self._stale_click(key, channel)
            # The question replaces the Proceed button on the very message you clicked, so it can't be missed.
            body = [b for b in sc.plan_blocks(key, moved.title, moved.plan, moved.plan_version, False, round_no=moved.round_no) if b["type"] not in ("actions", "context")]
            confirm = sc.confirm_blocks(key, moved.title, bool(moved.questions), self._strategy_note(moved))
            try:
                self.slack.update(channel, message_ts, f"Confirm start of {key}", body + [{"type": "divider"}] + confirm)
            except Exception:
                log.warning("Could not show the confirmation in place; posting it instead", exc_info=True)
                self._say(moved, "Confirm to start.", confirm)
            return "confirming"

        if action_id == sc.ACTION_BACK:
            moved = self.store.transition(key, CONFIRMING, AWAITING_REPLY)
            if not moved:
                return self._stale_click(key, channel)
            try:  # put the plan, with its Proceed button, back
                self.slack.update(channel, message_ts, f"Plan for {key}: {moved.title}",
                                  sc.plan_blocks(key, moved.title, moved.plan, moved.plan_version, bool(moved.questions), round_no=moved.round_no))
            except Exception:
                log.warning("Could not restore the plan message", exc_info=True)
            return "back"

        if action_id == sc.ACTION_CONFIRM:
            moved = self.store.transition(key, CONFIRMING, EXECUTING, delivery_id=uuid.uuid4().hex[:12], stuck_retried=False)
            if not moved:
                return self._stale_click(key, channel)
            self._lock_plan_message(moved, message_ts)
            self.submit(EXECUTE_JOB, key)
            queued = self._queued_note(key)
            self._say(moved, f":rocket: *Started {key}.* I'll post the pull request here when it's ready. This can't be stopped from Slack yet."
                             + (f"\n{queued}" if queued else ""))
            self._jira_comment(key, "🤖 Plan approved in Slack. Starting work now.")
            return "executing"
        return "unknown_action"

    def _stale_click(self, key: str, channel: str) -> str:
        """A plan button that no longer applies (you wrote meanwhile, it already started...): say why, not nothing."""
        s = self.store.get(key)
        status = s.status if s else "missing"
        if s and status in (PLANNING, REVISING):
            text = f"*{key}*: I'm updating the plan from your latest message. Approve the new version when it arrives."
        elif s and status not in TERMINAL:
            text = f"*{key}*: that button no longer applies. The ticket is {self._state_words(s)}."
        else:
            text = f"*{key}*: that button no longer applies. The ticket is completed."
        self._reply_loose(channel, "", text)
        return f"ignored:{status}"

    def _strategy(self, s: Session) -> dict:
        """How this round's code gets delivered. Raises if GitHub can't be asked.

        first    round 1: a new branch and PR, as always.
        continue the earlier PR is still open: more commits on its branch, same PR.
        fresh    the earlier PR was closed or merged (or there was none): new branch from the main branch, new PR.
        """
        if not s.rounds:
            return {"mode": "first"}
        fresh = {"mode": "fresh", "branch": f"ai/{s.issue_key}-r{s.round_no}", "pr_url": "", "pr_number": 0}
        if not s.pr_number:
            return fresh
        pr = self._github().get_pr(s.pr_number)
        if pr["state"] == "open":
            return {"mode": "continue", "branch": pr["branch"], "pr_number": pr["number"], "pr_url": pr["url"], "state": "open"}
        return {**fresh, "state": pr["state"]}

    def _strategy_note(self, s: Session) -> str:
        gone = ("\n:wastebasket: These files will be deleted first: " + ", ".join(f"`{p}`" for p in s.delete_files)) if s.delete_files else ""
        return self._delivery_note(s) + gone

    def _delivery_note(self, s: Session) -> str:
        if not s.rounds:
            return ""
        try:
            st = self._strategy(s)
        except Exception as e:
            log.warning("Could not check the PR for %s", s.issue_key, exc_info=True)
            return f":information_source: I couldn't check PR #{s.pr_number} just now; I'll check again when I start."
        if st["mode"] == "continue":
            return f":information_source: This adds a commit to the open PR #{st['pr_number']} (same branch)."
        why = {"merged": "was merged", "closed": "was closed without merging"}.get(st.get("state", ""), "doesn't exist")
        return f":information_source: The earlier PR {why}, so this starts from the main branch and opens a new PR."

    def _review_choice(self, key: str, channel: str, message_ts: str, plan: bool) -> str:
        s = self.store.get(key)
        pending = (s.review_pending if s else {}) or {}
        if not s or s.status != DELIVERED or not pending:
            self._update(channel, message_ts, "No longer valid.", "_This review prompt is no longer valid._")
            return "ignored_review"
        self.store.update(key, lambda x: setattr(x, "review_pending", {}))
        if not plan:
            self._update(channel, message_ts, "Ignored.", f"_OK, ignoring those review comments on *{key}*._")
            return "ignored_review"
        started = self._start_followup(key, "review", "Address these review comments on the pull request:\n" + pending["text"], True)
        self._update(channel, message_ts, "Planning.", f":hourglass_flowing_sand: _Planning changes for *{key}* from the review..._" if started else "_This is no longer valid._")
        return "followup" if started else "ignored_review"

    def _picked(self, value: str, channel: str, button_ts: str) -> str:
        key, _, orig_ts = value.partition("|")
        stashed = self.store.unstash(channel, orig_ts)
        if not stashed:
            self._update(channel, button_ts, "Expired.", "_That message has expired or was already sent. Please write it again._")
            return "expired"
        self._update(channel, button_ts, f"Sent to {key}.", f":white_check_mark: _Sent to *{key}*._")
        return self._deliver(key, channel, stashed.get("thread", ""), stashed["text"], "" if orig_ts == "0" else orig_ts)

    def _state_words(self, s: Session) -> str:
        return {
            PLANNING: "making the first plan",
            REVISING: "updating the plan",
            AWAITING_REPLY: "has questions for you" if s.questions else "plan ready, waiting for your approval",
            CONFIRMING: "waiting for you to confirm the start",
            EXECUTING: "coding now (can't be stopped)",
            DELIVERED: f"delivered (round {s.round_no}){': PR #' + str(s.pr_number) if s.pr_number else ''}. Reply to ask for changes",
        }.get(s.status, s.status)

    def _show_tickets(self, channel: str, thread_ts: str) -> None:
        rows = []
        base = self.s.jira_base_url.rstrip("/")
        for k in self.store.active_keys():
            s = self.store.get(k)
            if not s or s.status in TERMINAL:
                continue
            rows.append({
                "key": k, "title": s.title or "(planning)", "url": f"{base}/browse/{k}",
                "state": f"{self._state_words(s)} (plan v{s.plan_version})" if s.plan_version else self._state_words(s),
                "age": _age(time.time() - s.last_activity_at), "has_plan": bool(s.plan) and s.status in WAITING,
            })
        if not rows:
            text = f"No open tickets. Add the `{self.s.trigger_label}` label to a Jira ticket to start one."
            self._reply_loose(channel, thread_ts, text)
            return
        text = "Open tickets: " + ", ".join(r["key"] for r in rows)
        hint = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "_Reply `CA-7: your message` to talk to a specific ticket._"}]}] if len(rows) > 1 else []
        self._reply_loose(channel, thread_ts, text, sc.tickets_blocks(rows) + hint)

    def _show_plan(self, key: str, channel: str) -> str:
        s = self.store.get(key)
        if not s or not s.plan or s.status not in WAITING:
            self._reply_loose(channel, "", f"*{key}* has no plan to show right now.")
            return "no_plan"
        ref = self.slack.post(
            f"Plan for {key}: {s.title}", channel=s.slack_channel or channel,
            blocks=sc.plan_blocks(key, s.title, s.plan, s.plan_version, bool(s.questions), round_no=s.round_no),
        )
        self.store.map_thread(ref["channel"], ref["ts"], key)
        self._supersede(s, s.plan_version, again=True)
        self.store.update(key, lambda x: setattr(x, "plan_msg_ts", ref["ts"]))
        return "shown"

    def _update(self, channel: str, ts: str, text: str, note: str) -> None:
        try:
            self.slack.update(channel, ts, text, sc.superseded_blocks(note))
        except Exception:
            log.warning("Could not update Slack message", exc_info=True)

    def _lock_plan_message(self, s: Session, ts: str = "") -> None:
        """Re-render the approved plan without its buttons."""
        ts = ts or s.plan_msg_ts
        if not (s.slack_channel and ts):
            return
        blocks = [b for b in sc.plan_blocks(s.issue_key, s.title, s.plan, s.plan_version, False, round_no=s.round_no) if b["type"] != "actions"]
        blocks = [b for b in blocks if b["type"] != "context"] + sc.superseded_blocks(":white_check_mark: _Approved and started._")
        try:
            self.slack.update(s.slack_channel, ts, f"Plan for {s.issue_key} (approved)", blocks)
        except Exception:
            log.warning("Could not lock plan message for %s", s.issue_key, exc_info=True)

    # ---- execution -----------------------------------------------------------------------

    def run_execute_job(self, job: Job) -> None:
        key = job.issue_key
        with self.leased(key) as ok:
            if not ok:
                log.info("Execute job for %s skipped: another worker holds the lease; trying again when it lapses", key)
                job.finish("skipped", "lease held elsewhere")
                self._retry_when_lease_lapses(EXECUTE_JOB, key)
                return
            s = self.store.get(key)
            if not s or s.status != EXECUTING:
                job.finish("skipped", f"session is {s.status if s else 'missing'}")
                return
            followup = None
            try:  # decided now, not at planning time: the PR may have been merged or closed since
                st = self._strategy(s)
                if st["mode"] != "first":
                    followup = {
                        "round": s.round_no, "mode": st["mode"], "branch": st["branch"], "pr_number": st["pr_number"],
                        "pr_url": st["pr_url"], "previous_pr": s.pr_url or (s.rounds[-1].get("pr_url", "") if s.rounds else ""),
                        "history": build_history(s, max_chars=4000), "require_label": s.round_source == "jira",
                    }
            except Exception as e:
                log.warning("Could not decide how to deliver %s", key, exc_info=True)
                job.finish("failed", "Couldn't check the pull request's state. Nothing was changed.")
                self._finish_execution(s, job)
                return
            shipped = self._already_shipped(s, st)
            if shipped:  # a restart cut the job off after its push: record what is on GitHub, don't do it again
                log.warning("%s: this run's commit is already on %s; recording it instead of coding again", key, shipped["branch"])
                job.delivery, job.pr_url = shipped, shipped["pr_url"]
                job.warnings.append("I was restarted while finishing this; the work had already been pushed, so it wasn't redone.")
                job.finish("succeeded")
                self._finish_execution(s, job)
                return
            # handle_issue never raises: it records the outcome on the job and comments on the ticket.
            try:
                CodingAgent(self.s, job, plan=approved_plan_text(s), plan_files=s.files, followup=followup,
                            deletions=s.delete_files, delivery_id=s.delivery_id).handle_issue(key)
            finally:
                self._save_runs(job, "execute")
                self._save_cost(job, "execute")
            self._finish_execution(s, job)

    @staticmethod
    def _gist(plan: str) -> str:
        """A one-line stand-in for "what changed" when the PR summary is missing: the plan's first step."""
        m = re.search(r"## Plan\n(.*?)(?=\n## |\Z)", plan or "", re.S)
        first = (m.group(1) if m else plan or "").strip().splitlines()
        return first[0].lstrip("0123456789.-* ")[:300] if first else ""

    def _finish_execution(self, s: Session, job: Job) -> None:
        """Archive the round and wait for follow-ups: a delivered ticket stays open for FOLLOWUP_DAYS."""
        if job.status == "failed" and job.error_code == "stuck" and not job.delivery and self._again_after_stuck(s.issue_key, "the code changes"):
            self._jira_comment(s.issue_key, "🤖 The run got stuck and was stopped; trying it once more.")
            return  # (still executing: queued again as soon as this job has ended)
        d = job.delivery or {}
        outcome = {"succeeded": "delivered", "no_changes": "no changes", "skipped": "skipped"}.get(job.status, "failed")
        detail = job.pr_url or job.detail or ""
        continued = bool(d.get("pr_url")) and d.get("pr_url") == s.pr_url
        seen: list[str] = []
        if d.get("pr_number"):  # whatever people said on the PR so far was just handled: don't offer it again
            try:
                seen = [c["id"] for c in self._github().review_comments(d["pr_number"])]
            except Exception:
                log.warning("Could not read PR comments after delivery", exc_info=True)
        rnd = {
            "n": s.round_no, "request": s.round_request or s.title, "plan": s.plan, "files": s.files,
            "deleted": d.get("deleted", []),
            "summary": d.get("summary") or self._gist(s.plan), "outcome": outcome, "detail": (job.detail or "")[:500],
            "code": getattr(job, "error_code", None) if job.status == "failed" else None,
            "pr_url": d.get("pr_url", ""), "pr_number": d.get("pr_number", 0), "branch": d.get("branch", ""),
            "diff": d.get("diff", ""), "source": s.round_source, "finished_at": time.time(),
        }

        def done(x: Session):
            if x.status != EXECUTING:
                return False
            x.rounds.append(rnd)
            x.status, x.delivered_at, x.followup, x.force_plan, x.result = DELIVERED, time.time(), False, False, detail
            x.stuck_retried = False
            x.pr_notified, x.review_pending = "", {}
            if d.get("pr_number"):
                x.pr_number, x.pr_url, x.branch = d["pr_number"], d["pr_url"], d.get("branch", "")
                x.review_seen = (x.review_seen + seen)[-300:]
            x.touch()

        self.store.update(s.issue_key, done)

        key, n = s.issue_key, s.round_no
        days = self.s.followup_days
        ask = f"Reply here to ask for changes (this ticket stays open for follow-ups for {days} days)."
        if job.status == "succeeded":
            if continued:
                msg = f":white_check_mark: *{key}* (round {n}): pushed to the pull request: {job.pr_url}"
            elif s.rounds:
                msg = f":white_check_mark: *{key}* (round {n}): opened a new pull request: {job.pr_url}"
            else:
                msg = f":white_check_mark: *{key}*: pull request opened: {job.pr_url}"
            if d.get("deleted"):
                msg += "\n:wastebasket: Deleted: " + ", ".join(f"`{p}`" for p in d["deleted"])
            if job.detail:
                msg += f"\n:warning: {job.detail}"
            msg += f"\n{ask}"
        elif job.status == "skipped":
            msg = f":information_source: *{key}* was skipped. {explain(job.detail or '')}\n{ask}"
        elif job.status == "no_changes":
            said = "\n".join("> " + ln for ln in (job.detail or "(no explanation given)")[-700:].splitlines() if ln.strip())
            msg = (f":thinking_face: *{key}*: I looked at the code and didn't change anything. Here is what I found:\n{said}\n"
                   "Reply here with what to do differently (naming the files helps) and I'll plan again.")
        else:
            kind = kind_of(job.detail, getattr(job, "error_code", None))
            again = ("Press *Retry* (or reply `retry`) to run the same approved plan again, or reply with what to change and I'll plan again."
                     if kind.retry else
                     "Once the admin has fixed this, reply `retry` to run the same approved plan again, or reply with what to change and I'll plan again."
                     if kind.admin else "Reply with what to change and I'll plan again.")
            msg = f":x: *{key}*: I couldn't finish this. {explain(job.detail or '', kind.code)}\n{again}{reference(job.id)}"
        for w in job.warnings:
            msg += f"\n:warning: {w}"
        if job.status != "skipped":
            cost = self._cost_line(key, n)
            if cost:
                msg += f"\n{cost}"
        fresh = self.store.get(key) or s
        text = self._scrub(msg)
        retry = job.status == "failed" and bool(fresh.plan) and kind_of(job.detail, getattr(job, "error_code", None)).retry
        ref = self._say(fresh, text, sc.retry_blocks(self._tag(key, text), key) if retry else None, important=True)
        if ref:  # replies under the result message belong to this ticket
            self.store.map_thread(ref["channel"], ref["ts"], key)

    # ---- the periodic tick (called by an external pinger every ~10 minutes) --------------------

    def tick(self) -> dict:
        """Run one tick unless another is already running (here or on another instance); then it just says so.

        Overlapping ticks (self-ping plus an outside pinger, a slow tick, two instances during a deploy) could
        otherwise send a reminder or a PR-comment prompt twice. If Redis is down the lock raises, like the tick would.
        """
        if not self._tick_guard.acquire(blocking=False):
            return {"skipped": "a tick is already running"}
        owner = uuid.uuid4().hex
        try:
            if not self.store.acquire_tick_lock(owner, self.s.tick_lock_seconds):
                return {"skipped": "a tick is already running on another instance"}
            try:
                return self._tick_body()
            finally:
                try:
                    self.store.release_tick_lock(owner)
                except Exception:
                    log.warning("Could not release the tick lock (it expires by itself)", exc_info=True)
        finally:
            self._tick_guard.release()

    def _tick_body(self) -> dict:
        """Recover crashed jobs, nudge people who haven't replied, watch delivered PRs. Cheap enough to call often."""
        out = {"checked": 0, "requeued": [], "reminded": [], "merged": [], "reviews": [], "expired": [], "stale": [], "retried": []}
        for key in sorted(self.deferred_triggers):  # starts that failed because storage was down
            self.deferred_triggers.discard(key)
            try:
                if self.on_trigger(key):
                    out["retried"].append(key)
            except Exception:
                log.warning("Retrying the start of %s failed again", key, exc_info=True)
                self.deferred_triggers.add(key)
        self._flush_jira_comments()
        out["slack_sent"] = self.flush_slack()
        for key in self.store.active_keys():
            s = self.store.get(key)
            if not s or s.status in TERMINAL:
                continue
            out["checked"] += 1
            if s.pending_acks and self._close_stale_acks(s):
                out["stale"].append(key)
            if s.status in RUNNING:
                if not self.store.has_lease(key):
                    kind = EXECUTE_JOB if s.status == EXECUTING else PLAN_JOB
                    log.warning("Session %s is %s but no job holds its lease; re-queuing", key, s.status)
                    self.submit(kind, key)
                    out["requeued"].append(key)
            elif s.status in WAITING and not s.reminded:
                if time.time() - s.last_activity_at >= self.s.plan_reminder_hours * 3600:
                    self._remind(s)
                    out["reminded"].append(key)
            elif s.status == DELIVERED:
                if time.time() - s.last_activity_at >= self.s.followup_days * 86400:
                    if self.store.transition(key, DELIVERED, DONE, result=f"closed after {self.s.followup_days} days without follow-ups"):
                        out["expired"].append(key)
                    continue
                if s.pr_number:
                    seen = self._check_pr(s)
                    if seen:
                        out[seen].append(key)
        return out

    def _flush_jira_comments(self) -> None:
        try:
            pending = self.store.take_jira_comments()
        except Exception:
            log.warning("Could not read the queued Jira comments", exc_info=True)
            return
        for c in pending:
            if c["tries"] >= 5:
                log.error("Giving up on a Jira comment for %s after %s tries", c["key"], c["tries"])
                continue
            self._jira_comment(c["key"], c["body"], tries=c["tries"])

    def _close_stale_acks(self, s: Session) -> bool:
        """A "Looking into it..." that nothing is working on any more (the process died): close it and say so."""
        if s.status in RUNNING and (self.store.has_lease(s.issue_key) or self.busy(s.issue_key)):
            return False  # being worked on, or queued behind another ticket's job (one job at a time on a small server)
        cutoff = time.time() - self.s.ack_stale_seconds
        stale = [a for a in s.pending_acks if a.get("at", time.time()) < cutoff]
        if not stale:
            return False
        for a in stale:
            try:
                self.slack.update(a["channel"], a["ts"], "Lost track of this one.", sc.superseded_blocks("_I lost track of this message. Please send it again._"))
            except Exception:
                log.warning("Could not close a stale acknowledgement", exc_info=True)
        self.store.update(s.issue_key, lambda x: setattr(x, "pending_acks", [a for a in x.pending_acks if a not in stale]))
        return True

    def _check_pr(self, s: Session) -> str:
        """Look at a delivered ticket's PR. Returns "merged", "reviews" or "" (nothing to report)."""
        key = s.issue_key
        try:
            pr = self._github().get_pr(s.pr_number)
        except Exception:
            log.warning("Could not check PR #%s for %s", s.pr_number, key, exc_info=True)
            return ""
        if pr["state"] == "merged":
            if self.store.transition(key, DELIVERED, DONE, result=f"PR #{pr['number']} merged"):
                self._say(s, f":tada: *{key}*: PR #{pr['number']} was merged. The ticket is completed. For more changes, create a new Jira ticket (or update this one and add the `{self.s.trigger_label}` label again).")
                self._jira_comment(key, f"🤖 PR #{pr['number']} was merged. Marking this ticket completed.")
                self._move_jira(key, self.s.jira_status_done)
                return "merged"
            return ""
        if pr["state"] == "closed":
            if s.pr_notified != "closed":
                self.store.update(key, lambda x: setattr(x, "pr_notified", "closed"))
                self._say(s, f":information_source: *{key}*: PR #{pr['number']} was closed without merging. If you ask for more changes, I'll start fresh from the main branch.")
            return ""
        if s.review_pending:  # one question at a time
            return ""
        try:
            new = [c for c in self._github().review_comments(pr["number"]) if c["id"] not in s.review_seen]
        except Exception:
            log.warning("Could not read review comments for PR #%s", pr["number"], exc_info=True)
            return ""
        if not new:
            return ""
        lines = []
        for c in new[:20]:
            where = f" on `{c['path']}`" + (f" line {c['line']}" if c["line"] else "") if c["path"] else ""
            hunk = f"\n  code:\n{c['hunk']}" if c["hunk"] else ""
            lines.append(f"- {c['author']}{where}: {c['body'][:600]}{hunk}")
        text = "\n".join(lines)[:3500]
        self.store.update(key, lambda x: (
            setattr(x, "review_pending", {"text": text, "ids": [c["id"] for c in new]}),
            setattr(x, "review_seen", (x.review_seen + [c["id"] for c in new])[-300:]),
        ))
        self._say(s, f"New review on PR #{pr['number']} for {key}.", sc.review_blocks(key, pr["number"], pr["url"], new))
        return "reviews"

    def _move_jira(self, key: str, status: str) -> None:
        """Move the ticket to `status` if the project's workflow allows it from where it is. Never raises."""
        if not status:
            return
        try:
            outcome = self._jira().transition_to(key, status)
            if outcome == "unavailable":
                log.warning("%s: no Jira transition to %r from its current status; left as it is", key, status)
        except Exception:
            log.warning("Could not move %s to %r in Jira", key, status, exc_info=True)

    def _remind(self, s: Session) -> None:
        hours = self.s.plan_reminder_hours
        body = (
            f"{mention(s.reporter_account_id)} 🤖 I'm still waiting for your reply on the plan for this ticket "
            f"(sent over {hours // 24 or 1} day(s) ago). Please check Slack to continue. "
            "Nothing will run until you approve it."
        )
        self._jira_comment(s.issue_key, body.strip())
        self._say(s, f":bell: Reminder: *{s.issue_key}* has been waiting {hours} hours for your reply.")
        self.store.update(s.issue_key, lambda x: setattr(x, "reminded", True))
