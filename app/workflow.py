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
from app.agent import CodingAgent
from app.config import Settings
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
_KEY_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*-\d+)\b\s*[:,\-\u2013\u2014]?\s*(.*)$", re.S)


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
        self.deferred_triggers: set[str] = set()  # tickets whose start failed (Redis down); retried on the next tick

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

    def _post_reliably(self, channel: str, text: str, blocks: list | None = None, thread_ts: str = "") -> dict | None:
        """Post to Slack: three tries with a short wait, then once as plain text. None if all fail."""
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
        return None

    def _say(self, s: Session, text: str, blocks: list | None = None, important: bool = False, thread_ts: str = "") -> dict | None:
        """Post a new message in the main DM chat (never hidden in a thread unless `thread_ts` says so). Never raises.

        `important` messages (results, failures) that still can't be delivered are written to the Jira
        ticket instead, so the outcome isn't lost.
        """
        text = self._tag(s.issue_key, text)
        ref = self._post_reliably(s.slack_channel, text, blocks, thread_ts=thread_ts)
        if ref is None and important:
            self._jira_comment(s.issue_key, "🤖 (I couldn't reach Slack, so this is posted here instead)\n" + re.sub(r"[`*]", "", text))
        return ref

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
                    self._post_reliably(s.slack_channel, f":warning: *{key}*: couldn't comment on the Jira ticket ({type(e).__name__}). I'll retry shortly.")

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
                log.info("Plan job for %s skipped: another worker holds the lease", key)
                job.finish("skipped", "lease held elsewhere")
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
                log.exception("Planning failed for %s", key)
                self._plan_failed(s, self._scrub(str(e)))
                job.finish("failed", self._scrub(str(e))[-500:])
            finally:
                self._save_runs(job, "plan")

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
            self._say(s, f":warning: I couldn't check the state of PR #{s.pr_number} ({type(e).__name__}), so I looked at the main branch instead. The plan may miss the earlier changes.")
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
                self._say(s, f":warning: {w}")

        if chatting and not result.changed:
            # Just a conversation: answer where you wrote, leave the plan and its version alone.
            ref = None
            with job.step("slack_post"):
                if result.show and not new_round:  # "show me the plan again": the same plan, as a fresh message below your own
                    ref = self.slack.post(
                        f"Plan for {key}: {s.title}", channel=s.slack_channel, thread_ts=s.reply_to,
                        blocks=sc.plan_blocks(key, s.title, s.plan, s.plan_version, bool(s.questions), round_no=s.round_no),
                    )
                    self.store.map_thread(ref["channel"], ref["ts"], key)
                    self._supersede(s, s.plan_version, again=True)
                self._answer(s, handled["n"], result.reply)
                warn()

            def answered(x: Session):
                if ref:
                    x.plan_msg_ts = ref["ts"]
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
            if first:
                ref = self.slack.post(text, blocks=blocks)
                self.store.map_thread(ref["channel"], ref["ts"], key)
                channel, thread_ts = ref["channel"], ref["ts"]
            else:
                # the new version goes right below your message, wherever you wrote it
                ref = self.slack.post(text, channel=s.slack_channel, blocks=blocks, thread_ts=s.reply_to)
                self.store.map_thread(ref["channel"], ref["ts"], key)  # replies under this version find their ticket
                channel, thread_ts = s.slack_channel, s.slack_thread_ts
                if not new_round:  # (the previous round's plan message is already locked as approved)
                    self._supersede(s, version)
            said = result.reply or "Taken into account in the plan."
            if new_round:
                said += f" The follow-up plan (round {round_no}) is below."
            elif not first:
                said += f" The updated plan (v{version}) is below."
            self._answer(s, handled["n"], said)
            warn()

        def finish(x: Session):
            x.plan, x.questions, x.plan_version, x.files = result.body, result.questions, version, result.files
            x.delete_files = list(result.delete_files)
            x.slack_channel, x.slack_thread_ts, x.plan_msg_ts = channel, thread_ts, ref["ts"]
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
        if first:
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

    def _say_in(self, s: Session, text: str) -> None:
        self.slack.post(text, channel=s.slack_channel, blocks=sc.reply_blocks(text), thread_ts=s.reply_to or s.slack_thread_ts)

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

    def _plan_failed(self, s: Session, error: str) -> None:
        key = s.issue_key
        fresh = self.store.get(key) or s
        again = "Ask again to retry." if fresh.followup else "Reply here to try again."
        # an error answers what you just wrote: in the thread if you wrote in a thread, in the main chat if you wrote there
        ref = self._say(fresh, f":x: I couldn't produce the plan for {key}:\n```{error[-1200:]}```\n{again}", important=True, thread_ts=fresh.reply_to)
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
        self._jira_comment(key, f"🤖 Planner failed:\n{{noformat}}{error[-1200:]}{{noformat}}")

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
        try:
            ref = self.slack.post(self._tag(key, note), channel=channel, thread_ts=anchor)
            self.store.map_thread(ref["channel"], ref["ts"], key)
            self.store.update(key, lambda x: x.pending_acks.append({"channel": ref["channel"], "ts": ref["ts"], "i": index["i"], "at": time.time()}))
        except Exception:
            log.warning("Could not post the acknowledgement for %s", key, exc_info=True)
        self.submit(PLAN_JOB, key)  # a no-op while a plan job is in flight; resume_if_needed catches it
        return result

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

        if action_id == sc.ACTION_PROCEED:
            if not s.plan:
                return "no_plan"
            moved = self.store.transition(key, AWAITING_REPLY, CONFIRMING)
            if not moved:
                return f"ignored:{s.status}"
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
                return f"ignored:{s.status}"
            try:  # put the plan, with its Proceed button, back
                self.slack.update(channel, message_ts, f"Plan for {key}: {moved.title}",
                                  sc.plan_blocks(key, moved.title, moved.plan, moved.plan_version, bool(moved.questions), round_no=moved.round_no))
            except Exception:
                log.warning("Could not restore the plan message", exc_info=True)
            return "back"

        if action_id == sc.ACTION_CONFIRM:
            moved = self.store.transition(key, CONFIRMING, EXECUTING)
            if not moved:
                return f"ignored:{s.status}"
            self._lock_plan_message(moved, message_ts)
            self._say(moved, f":rocket: *Started {key}.* I'll post the pull request here when it's ready. This can't be stopped from Slack yet.")
            self._jira_comment(key, "🤖 Plan approved in Slack. Starting work now.")
            self.submit(EXECUTE_JOB, key)
            return "executing"
        return "unknown_action"

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
            return f":information_source: I couldn't check PR #{s.pr_number} just now ({type(e).__name__}); I'll check again when I start."
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
                job.finish("skipped", "lease held elsewhere")
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
                job.finish("failed", f"Couldn't check the pull request's state ({type(e).__name__}: {self._scrub(str(e))[:200]}). Nothing was changed.")
                self._finish_execution(s, job)
                return
            # handle_issue never raises: it records the outcome on the job and comments on the ticket.
            try:
                CodingAgent(self.s, job, plan=approved_plan_text(s), plan_files=s.files, followup=followup, deletions=s.delete_files).handle_issue(key)
            finally:
                self._save_runs(job, "execute")
            self._finish_execution(s, job)

    @staticmethod
    def _gist(plan: str) -> str:
        """A one-line stand-in for "what changed" when the PR summary is missing: the plan's first step."""
        m = re.search(r"## Plan\n(.*?)(?=\n## |\Z)", plan or "", re.S)
        first = (m.group(1) if m else plan or "").strip().splitlines()
        return first[0].lstrip("0123456789.-* ")[:300] if first else ""

    def _finish_execution(self, s: Session, job: Job) -> None:
        """Archive the round and wait for follow-ups: a delivered ticket stays open for FOLLOWUP_DAYS."""
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
            "pr_url": d.get("pr_url", ""), "pr_number": d.get("pr_number", 0), "branch": d.get("branch", ""),
            "diff": d.get("diff", ""), "source": s.round_source, "finished_at": time.time(),
        }

        def done(x: Session):
            if x.status != EXECUTING:
                return False
            x.rounds.append(rnd)
            x.status, x.delivered_at, x.followup, x.force_plan, x.result = DELIVERED, time.time(), False, False, detail
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
            msg = f":information_source: *{key}* was skipped: {job.detail}\n{ask}"
        elif job.status == "no_changes":
            msg = (f":thinking_face: *{key}*: the agent finished but changed nothing. This is what the model said:\n"
                   f"```{(job.detail or '(no explanation)')[-1000:]}```\n"
                   "Reply here with what to do differently (file names help) and I'll plan again.")
        else:
            msg = f":x: *{key}* failed:\n```{(job.detail or 'unknown error')[-1200:]}```\nReply to try again."
        for w in job.warnings:
            msg += f"\n:warning: {w}"
        fresh = self.store.get(key) or s
        ref = self._say(fresh, self._scrub(msg), important=True)
        if ref:  # replies under the result message belong to this ticket
            self.store.map_thread(ref["channel"], ref["ts"], key)

    # ---- the periodic tick (called by an external pinger every ~10 minutes) --------------------

    def tick(self) -> dict:
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
        if s.status in RUNNING and self.store.has_lease(s.issue_key):
            return False
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
