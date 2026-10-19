"""Every kind of failure the app reports, in one table: what the ticket owner is told, whether it is the admin's to fix,
whether a backup model can get around it, and whether "Retry with the same plan" is offered.

Errors are recognised once, where they happen: the LLM output classifier (agent.llm_problem) words each problem with
its kind's `reason`, and code raises AgentError(..., code=...) where it knows the kind. Everything downstream (the
Slack/Jira wording in friendly.py, the backup-model decision, the Retry button, admin alerts) asks kind_of().
Older or unexpected texts are still recognised by their pattern, and anything unknown is "internal".
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    code: str
    detect: str  # regex over the error text (our own `reason` sentences, or the wording of the place that raises it)
    owner: str  # what the ticket owner sees: what happened and what to do, no technical detail
    admin: bool = False  # a setup or technical problem only the admin can fix (config, keys, permissions, crashes)
    backup: bool = False  # another model may get around it (worth one try with the backup model)
    retry: bool = True  # offer "Retry with the same plan" (pointless while a setup problem is unfixed)
    reason: str = ""  # the technical sentence used in job details and logs, when the classifier words it


_SETUP = "I can't reach the AI service because of a setup problem on my side. Please tell the admin and quote the reference."
_USE_SETUP = "I can't use the AI service because of a setup problem on my side. Please tell the admin and quote the reference."

# Order matters: the first match wins (more specific texts first).
KINDS: list[Kind] = [
    Kind("lease_busy", r"lease held elsewhere", "Another run for this ticket was already in progress.", retry=False),
    Kind("pr_already_open", r"PR already open", "A pull request for this ticket is already open.", retry=False),
    Kind("label_removed", r"trigger label .* removed|label .* removed", "The ticket's trigger label was removed.", retry=False),
    Kind("session_state", r"session is ", "The ticket was no longer waiting for this step.", retry=False),
    Kind("branch_pushed_no_pr", r"was pushed, but opening the pull request failed",
         "The changes were saved on a branch, but I couldn't open the pull request.", retry=False),
    Kind("quota_daily", r"daily (free )?quota",
         "The AI service has reached its daily usage limit, so I can't work on this right now. Please try again later.",
         backup=True),
    Kind("stuck", r"made no progress for",
         "I got stuck (nothing happened for several minutes), so I stopped. Please try again.",
         reason="The job made no progress for JOB_STUCK_SECONDS and was stopped by the watchdog."),
    Kind("tool_timeout", r"OpenCode timed out|Aider timed out",
         "The work took longer than allowed and was stopped. Please try again; a smaller ticket, or naming the files, helps."),
    Kind("too_large", r"too large for the model",
         "This is too much for the AI to handle at once. Try a smaller change, or split it into several tickets.", retry=False,
         reason="The request was too large for the model (token/context limit). Use a smaller ticket or fewer files, or a model with a bigger limit."),
    Kind("empty_reply", r"empty reply", "The AI didn't give an answer. Please try again."),
    Kind("content_blocked", r"content filter", "The AI declined to work on this request. Try rewording it.", retry=False,
         reason="The model refused or blocked the request (content filter)."),
    Kind("step_limit", r"limit of model requests|OPENCODE_(PLAN_)?MAX_STEPS",
         "I ran out of my allowed number of steps before finishing. Naming the files to change in the ticket helps; the admin can also raise the limit."),
    Kind("invalid_request", r"rejected the request as invalid", _USE_SETUP, admin=True, backup=True, retry=False,
         reason="The AI provider rejected the request as invalid (a model setting or parameter it doesn't accept). This is a setup problem, not the ticket: check the model name and the model settings."),
    Kind("opencode_no_start", r"failed before its first model request", _USE_SETUP, admin=True, backup=True, retry=False),
    Kind("opencode_missing", r"OpenCode could not be started", _USE_SETUP, admin=True, retry=False),
    Kind("auth", r"API key was rejected", _SETUP, admin=True, retry=False,
         reason="The LLM API key was rejected. Check the key and the model name."),
    Kind("model_not_found", r"model name wasn't found", _SETUP, admin=True, backup=True, retry=False,
         reason="The model name wasn't found at the provider. Check the model settings (AIDER_MODEL / PLANNER_MODEL, or OPENCODE_MODEL / OPENCODE_PLANNER_MODEL, and their *_FALLBACK_MODEL)."),
    Kind("rate_limit", r"rate or usage limit", "The AI service is busy or at its usage limit right now. Please try again in a few minutes.",
         backup=True, reason="The LLM provider's rate or usage limit was hit. Wait a bit and reply to try again."),
    Kind("provider_down", r"unavailable or timed out|timed out", "The AI service didn't respond in time. Please try again in a minute.",
         backup=True, reason="The LLM provider was unavailable or timed out. Try again in a minute."),
    Kind("edit_mismatch", r"couldn't apply the model's edit",
         "I couldn't make that change in the code. Please tell me exactly which file and what to change."),
    Kind("plan_format", r"expected format|expected layout", "I couldn't put together a clear plan this time. Please try again."),
    Kind("github_state", r"Couldn't check the pull request|couldn't check the (state of )?pull request",
         "I couldn't check the pull request on GitHub, so nothing was changed. Please try again shortly."),
    Kind("git_clone", r"git clone", "I couldn't download the project from GitHub. Please try again shortly."),
    Kind("git_push", r"git push", "I couldn't upload the changes to GitHub. Please try again shortly."),
    Kind("permission", r"Client error '40[13]|\b40[13] (Unauthorized|Forbidden)",
         "I wasn't allowed to use GitHub or Jira (a permissions problem on my side). Please tell the admin and quote the reference.",
         admin=True, retry=False),
    Kind("storage", r"redis|storage", "I couldn't save my progress right now. Please try again in a minute.", admin=True),
]
INTERNAL = Kind("internal", "", "Something went wrong on my side. Please try again, and if it keeps happening, tell the admin and quote the reference.",
                admin=True)
BY_CODE: dict[str, Kind] = {k.code: k for k in KINDS + [INTERNAL]}


def kind_of(text: str | None, code: str | None = None) -> Kind:
    """The kind of a failure: by its code when the raiser knew it, else recognised from the text, else "internal"."""
    if code and code in BY_CODE:
        return BY_CODE[code]
    for k in KINDS:
        if re.search(k.detect, text or "", re.I):
            return k
    return INTERNAL


def reason(code: str) -> str:
    return BY_CODE[code].reason
