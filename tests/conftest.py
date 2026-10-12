import pytest


@pytest.fixture(autouse=True)
def no_plan_mode_by_default(monkeypatch):
    """A developer's .env may hold real Slack/Redis values; tests must never pick them up.

    Environment variables beat .env, so blank them. Tests that want plan-first mode set them.
    """
    # Required settings: CI has no .env, so without these Settings() fails validation.
    for name, value in {
        "JIRA_BASE_URL": "https://example.atlassian.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "x",
        "GITHUB_TOKEN": "x", "GITHUB_REPO": "o/r", "WEBHOOK_SECRET": "w",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_USER_ID", "REDIS_URL", "TICK_TOKEN", "PLANNER_MODEL"):
        monkeypatch.setenv(name, "")
    from app import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()