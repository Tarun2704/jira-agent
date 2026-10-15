import pytest

from app.friendly import explain, reference

TECHNICAL = ("litellm", "Traceback", "RuntimeError", "HTTPStatusError", "TPM", "aider", "git ", "token", "model ", "quota", "API")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("The LLM call failed. The LLM provider's rate or usage limit was hit. Wait a bit.\nProvider said: litellm.RateLimitError: x", "busy or at its usage limit"),
        ("The daily free quota for gemini-2.5-flash is used up (daily quota: ...). It resets in about 20 hour(s). Set a fallback", "daily usage limit"),
        ("The request was too large for the model (token/context limit).", "too much for the AI"),
        ("The model refused or blocked the request (content filter).", "declined"),
        ("The LLM API key was rejected. Check the key.", "setup problem"),
        ("The model name wasn't found at the provider. Check AIDER_MODEL", "setup problem"),
        ("Nothing was changed. The coding agent used up its limit of model requests (OPENCODE_MAX_STEPS)", "allowed number of steps"),
        ("The planning agent used up its limit of 20 model requests (OPENCODE_PLAN_MAX_STEPS)", "allowed number of steps"),
        ("The AI provider rejected the request as invalid (a model setting", "setup problem"),
        ("OpenCode failed before its first model request and gave no reason", "setup problem"),
        ("Aider timed out after 900s", "didn't respond in time"),
        ("The LLM call failed. Aider couldn't apply the model's edit (its change didn't match...", "which file and what to change"),
        ("The model returned an empty reply (possibly blocked by its content filter).", "didn't give an answer"),
        ("The planner's reply wasn't in the expected format. Reply to try again.\nstuff", "clear plan"),
        ("Couldn't check the pull request's state. Nothing was changed.", "check the pull request"),
        ("`git clone --depth 50` failed:\nfatal: unable to access", "download the project"),
        ("`git push --force -u` failed:\nremote: denied", "upload the changes"),
        ("httpx.HTTPStatusError: Client error '403 Forbidden' for url", "permissions problem"),
        ("something unforeseen: KeyError('x')", "Something went wrong on my side"),
    ],
)
def test_errors_become_plain_language(raw, expected):
    said = explain(raw)
    assert expected in said
    for word in ("litellm", "Traceback", "RuntimeError", "HTTPStatusError", "KeyError", "AIDER_MODEL", "gemini"):
        assert word not in said


def test_the_daily_wait_is_given_in_hours():
    assert "about 20 hour(s)" in explain("The daily free quota for x is used up. It resets in about 20 hour(s).")


def test_a_pull_request_failure_keeps_the_helpful_link_only():
    raw = ("The branch ai/CA-1-x was pushed, but opening the pull request failed: 422 Client Error ...\n"
           "Open it yourself: https://github.com/o/r/compare/main...ai/CA-1-x?expand=1")
    said = explain(raw)
    assert "`ai/CA-1-x`" in said and "https://github.com/o/r/compare/main...ai/CA-1-x?expand=1" in said and "422" not in said


def test_reference_is_short_and_optional():
    assert "abc123" in reference("abc123") and reference("") == ""
