"""Plain-language wording for errors, for the people using Slack and Jira.

The raw details (exception names, provider messages, git output) stay in the server logs and in the
run log (/runs/<ticket>); what people see is what happened and what to do next.
"""
import re

from app.errors import kind_of


def explain(error: str, code: str | None = None) -> str:
    """What happened and what to do, in words anyone can follow. Never includes technical output.
    The wording is the error kind's `owner` text (app/errors.py); `code` when the caller knows the kind."""
    text = error or ""
    m = re.search(r"The branch (\S+) was pushed, but opening the pull request failed.*?(https://github\.com/\S+/compare/\S+)", text, re.S)
    if m:
        return (f"The changes were saved on the branch `{m.group(1)}`, but I couldn't open the pull request. "
                f"You can open it yourself here: {m.group(2)}")
    kind = kind_of(text, code)
    if kind.code == "quota_daily":
        hours = re.search(r"resets in about (\d+) hour", text)
        if hours:
            return kind.owner.replace("Please try again later.", f"It should be available again in about {hours.group(1)} hour(s). Please try again later.")
    return kind.owner


def reference(job_id: str) -> str:
    """A short code people can quote; the admin finds the details in the logs with it."""
    return f"\n_Reference: {job_id}_" if job_id else ""
