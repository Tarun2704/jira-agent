"""Plain-language wording for errors, for the people using Slack and Jira.

The raw details (exception names, provider messages, git output) stay in the server logs and in the
run log (/runs/<ticket>); what people see is what happened and what to do next.
"""
import re

_RULES: list[tuple[str, str]] = [
    (r"lease held elsewhere", "Another run for this ticket was already in progress."),
    (r"PR already open", "A pull request for this ticket is already open."),
    (r"trigger label .* removed|label .* removed", "The ticket's trigger label was removed."),
    (r"session is ", "The ticket was no longer waiting for this step."),
    (r"too large for the model", "This is too much for the AI to handle at once. Try a smaller change, or split it into several tickets."),
    (r"empty reply", "The AI didn't give an answer. Please try again."),
    (r"content filter", "The AI declined to work on this request. Try rewording it."),
    (r"API key was rejected|model name wasn't found", "I can't reach the AI service because of a setup problem on my side. Please tell the admin and quote the reference."),
    (r"rate or usage limit", "The AI service is busy or at its usage limit right now. Please try again in a few minutes."),
    (r"unavailable or timed out|timed out", "The AI service didn't respond in time. Please try again in a minute."),
    (r"couldn't apply the model's edit", "I couldn't make that change in the code. Please tell me exactly which file and what to change."),
    (r"expected format|expected layout", "I couldn't put together a clear plan this time. Please try again."),
    (r"Couldn't check the pull request|couldn't check the (state of )?pull request", "I couldn't check the pull request on GitHub, so nothing was changed. Please try again shortly."),
    (r"git clone", "I couldn't download the project from GitHub. Please try again shortly."),
    (r"git push", "I couldn't upload the changes to GitHub. Please try again shortly."),
    (r"Client error '40[13]|\b40[13] (Unauthorized|Forbidden)", "I wasn't allowed to use GitHub or Jira (a permissions problem on my side). Please tell the admin and quote the reference."),
    (r"redis|storage", "I couldn't save my progress right now. Please try again in a minute."),
]


def explain(error: str) -> str:
    """What happened and what to do, in words anyone can follow. Never includes technical output."""
    text = error or ""
    m = re.search(r"The branch (\S+) was pushed, but opening the pull request failed.*?(https://github\.com/\S+/compare/\S+)", text, re.S)
    if m:
        return (f"The changes were saved on the branch `{m.group(1)}`, but I couldn't open the pull request. "
                f"You can open it yourself here: {m.group(2)}")
    if "daily quota" in text or "daily free quota" in text:
        hours = re.search(r"resets in about (\d+) hour", text)
        when = f" It should be available again in about {hours.group(1)} hour(s)." if hours else ""
        return f"The AI service has reached its daily usage limit, so I can't work on this right now.{when} Please try again later."
    for pattern, sentence in _RULES:
        if re.search(pattern, text, re.I):
            return sentence
    return "Something went wrong on my side. Please try again, and if it keeps happening, tell the admin and quote the reference."


def reference(job_id: str) -> str:
    """A short code people can quote; the admin finds the details in the logs with it."""
    return f"\n_Reference: {job_id}_" if job_id else ""
