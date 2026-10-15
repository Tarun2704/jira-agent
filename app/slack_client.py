"""Slack: post/update messages, verify Slack's request signatures, build the plan message.

The bot only talks to one person (SLACK_USER_ID) in a DM. Posting with channel=<user id>
opens/uses that DM and returns the real channel id (D...), which is stored in the session.
"""
import hashlib
import hmac
import logging
import re
import time

import httpx

from app.retry import RetryingClient

log = logging.getLogger(__name__)

API = "https://slack.com/api"
MAX_SECTION = 2800  # Slack's limit is 3000 characters per section block

ACTION_PROCEED = "plan_proceed"
ACTION_CONFIRM = "plan_confirm"
ACTION_BACK = "plan_back"
ACTION_PICK = "pick_ticket"  # value "KEY|<ts of the message to deliver>"
ACTION_SHOW = "show_plan"  # value KEY
ACTION_REVIEW_PLAN = "review_plan"  # value KEY: plan changes from the new PR review comments
ACTION_REVIEW_IGNORE = "review_ignore"  # value KEY
ACTION_DESC_PLAN = "desc_plan"  # value KEY: the Jira description changed, plan a follow-up
ACTION_DESC_IGNORE = "desc_ignore"  # value KEY
ACTION_RETRY = "retry_round"  # value KEY: run the approved plan again after a failed attempt


class SlackError(Exception):
    pass


def verify_signature(signing_secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """Slack signs `v0:<timestamp>:<raw body>` with the app's signing secret (HMAC-SHA256)."""
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > 300 or not signing_secret or not signature:  # replay protection
        return False
    expected = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SlackClient:
    def __init__(self, token: str, user_id: str):
        self.user_id = user_id
        self._client = RetryingClient(
            base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=20
        )

    def _call(self, method: str, payload: dict) -> dict:
        r = self._client.post(f"/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
        return data

    def auth_test(self) -> dict:
        """Who this token is: {team, user, url...}. Raises SlackError if the token is invalid."""
        return self._call("auth.test", {})

    def post(self, text: str, *, channel: str = "", blocks: list | None = None, thread_ts: str = "") -> dict:
        """Post to a channel/thread, or (no channel) to the person's DM. Returns {channel, ts}."""
        payload: dict = {"channel": channel or self.user_id, "text": text[:3000]}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        data = self._call("chat.postMessage", payload)
        return {"channel": data["channel"], "ts": data["ts"]}

    def delete(self, channel: str, ts: str) -> None:
        self._call("chat.delete", {"channel": channel, "ts": ts})

    def update(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        payload: dict = {"channel": channel, "ts": ts, "text": text[:3000]}
        payload["blocks"] = blocks if blocks is not None else []
        self._call("chat.update", payload)


# ---- message building ------------------------------------------------------------------


def to_mrkdwn(text: str) -> str:
    """Rough Markdown -> Slack mrkdwn: headings and **bold** become *bold*, links become <url|text>."""
    text = re.sub(r"^#{1,6}\s*(.+?)\s*$", r"*\1*", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    return text


def _sections(text: str) -> list[dict]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_SECTION:  # a single enormous line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:MAX_SECTION])
            line = line[MAX_SECTION:]
        if len(cur) + len(line) + 1 > MAX_SECTION:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c.rstrip()}} for c in chunks if c.strip()][:45]


def action_name(action_id: str) -> str:
    """Buttons in one message need distinct action ids, so some carry a ":KEY" suffix; this strips it."""
    return action_id.split(":", 1)[0]


def _button(text: str, action_id: str, key: str, style: str = "") -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": key}
    if style:
        b["style"] = style
    return b


def plan_blocks(
    key: str, title: str, body_md: str, version: int, questions_open: bool, note: str = "", round_no: int = 1
) -> list[dict]:
    """The plan message. With `note` it is a read-only record (no button): used for superseded
    and approved plans, which stay readable instead of disappearing."""
    header = (
        f"*Follow-up plan for {key}: {title}* (round {round_no}, v{version})" if round_no > 1
        else f"*Plan for {key}: {title}* (v{version})"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
        *_sections(to_mrkdwn(body_md)),
    ]
    if note:
        return blocks + [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]
    hint = (
        "_I have questions above. Reply here to answer them, ask me anything, or ask for a change._"
        if questions_open
        else "_Ask me anything or ask for a change by replying here. When it looks right, approve it below._"
    )
    return blocks + [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]},
        {"type": "actions", "elements": [_button("Proceed with this plan", ACTION_PROCEED, key, "primary")]},
    ]


def confirm_blocks(key: str, title: str, questions_open: bool, note: str = "") -> list[dict]:
    warn = (
        "\n:warning: There are still unanswered questions in the plan." if questions_open else ""
    ) + (f"\n{note}" if note else "")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Are you sure you want to start {key}: {title}?*\n"
                    "The agent will write code, push a branch and open a pull request. "
                    "*Once it starts, it can't be stopped from Slack yet.*" + warn
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                _button("Yes, start now", ACTION_CONFIRM, key, "danger"),
                _button("Go back", ACTION_BACK, key),
            ],
        },
    ]


def superseded_blocks(note: str) -> list[dict]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]


def retry_blocks(text: str, key: str) -> list[dict]:
    """A failed run's message, with a button that runs the same approved plan again (no new plan)."""
    return reply_blocks(text) + [{"type": "actions", "elements": [_button("Retry with the same plan", ACTION_RETRY, key, "primary")]}]


def reply_blocks(text: str) -> list[dict]:
    """A chat answer: plain mrkdwn sections (no buttons)."""
    return _sections(to_mrkdwn(text)) or [{"type": "section", "text": {"type": "mrkdwn", "text": "Done."}}]


def pick_blocks(rows: list[tuple[str, str]], message_ts: str) -> list[dict]:
    """"Which ticket is this about?": one button per waiting ticket. rows = [(key, title)]."""
    lines = "\n".join(f"*{k}*: {title}" for k, title in rows)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Which ticket is this about?\n{lines}"}},
        {"type": "actions", "elements": [_button(k, f"{ACTION_PICK}:{k}", f"{k}|{message_ts}") for k, _ in rows[:5]]},
    ]


def tickets_blocks(rows: list[dict]) -> list[dict]:
    """Open tickets. Each row: {key, title, url, state, age}. A "Show plan" button where a plan exists."""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Open tickets* ({len(rows)})"}}]
    for r in rows[:20]:
        link = f"<{r['url']}|{r['key']}>" if r.get("url") else f"*{r['key']}*"
        section = {"type": "section", "text": {"type": "mrkdwn", "text": f"{link}  {r['title']}\n_{r['state']} · {r['age']}_"}}
        if r.get("has_plan"):
            section["accessory"] = _button("Show plan", f"{ACTION_SHOW}:{r['key']}", r["key"])
        blocks.append(section)
    return blocks


def review_blocks(key: str, pr_number: int, pr_url: str, comments: list[dict]) -> list[dict]:
    """New review comments on a delivered ticket's PR: offer to turn them into the next round."""
    authors = ", ".join(sorted({c["author"] for c in comments}))
    preview = "\n".join(
        f"> {('`' + c['path'] + '`: ') if c.get('path') else ''}{c['body'][:140].replace(chr(10), ' ')}" for c in comments[:3]
    )
    more = f"\n_and {len(comments) - 3} more_" if len(comments) > 3 else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{key}*: new review on <{pr_url}|PR #{pr_number}> from {authors} ({len(comments)} comment{'s' if len(comments) != 1 else ''})\n{preview}{more}"
        )}},
        {"type": "actions", "elements": [
            _button("Plan changes from these", ACTION_REVIEW_PLAN, key, "primary"),
            _button("Ignore", ACTION_REVIEW_IGNORE, key),
        ]},
    ]


def description_blocks(key: str, title: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{key}*: the description in Jira was updated ({title}).\nPlan a follow-up from it?"}},
        {"type": "actions", "elements": [
            _button("Plan a follow-up", ACTION_DESC_PLAN, key, "primary"),
            _button("Ignore", ACTION_DESC_IGNORE, key),
        ]},
    ]
