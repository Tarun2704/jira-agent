import pytest


@pytest.fixture(autouse=True)
def no_plan_mode_by_default(monkeypatch):
    """A developer's .env may hold real Slack/Redis values; tests must never pick them up.

    Environment variables beat .env, so blank them. Tests that want plan-first mode set them.
    """
    for name in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_USER_ID", "REDIS_URL", "TICK_TOKEN", "PLANNER_MODEL"):
        monkeypatch.setenv(name, "")
    from app import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
