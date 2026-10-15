"""The error table (app/errors.py): one place decides the wording, the backup model, the Retry button, admin alerts."""
import pytest

from app import agent
from app.errors import BY_CODE, INTERNAL, KINDS, kind_of
from app.friendly import explain


def test_the_table_is_complete():
    assert len(BY_CODE) == len(KINDS) + 1  # codes are unique
    for k in KINDS + [INTERNAL]:
        assert k.owner and k.owner.endswith(".") and "Error" not in k.owner  # plain words for the ticket owner
    for _, code in agent._LLM_PROBLEMS:  # every classifier outcome is in the table, worded by it
        assert BY_CODE[code].reason and kind_of(BY_CODE[code].reason).code == code


@pytest.mark.parametrize("provider_output,code,backup,retry,admin", [
    ("litellm.RateLimitError: 429 tokens per minute", "rate_limit", True, True, False),
    ("litellm.ServiceUnavailableError: 503 high demand", "provider_down", True, True, False),
    ("litellm.NotFoundError: model gemini-0 is not found", "model_not_found", True, False, True),
    ("litellm.BadRequestError: INVALID_ARGUMENT Invalid value at 'safety_settings'", "invalid_request", True, False, True),
    ("litellm.AuthenticationError: invalid api key", "auth", False, False, True),
    ("litellm.ContentPolicyViolationError: blocked by the content filter", "content_blocked", False, False, False),
    ("litellm.ContextWindowExceededError: too many tokens", "too_large", False, False, False),
])
def test_classified_problems_decide_backup_retry_and_admin(provider_output, code, backup, retry, admin):
    problem = agent.llm_problem(provider_output)
    k = kind_of(problem)
    assert (k.code, k.backup, k.retry, k.admin) == (code, backup, retry, admin)
    assert agent.retryable_llm_problem(problem) is backup


def test_unknown_errors_are_internal_and_go_to_the_admin():
    k = kind_of("KeyError: 'plan_msg_ts'")
    assert k is INTERNAL and k.admin and "Something went wrong" in explain("KeyError: 'plan_msg_ts'")


def test_a_known_code_wins_over_the_text():
    assert kind_of("something vague", "step_limit").code == "step_limit"
    assert "allowed number of steps" in explain("something vague", "step_limit")
