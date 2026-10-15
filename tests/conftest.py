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
    for name in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_USER_ID", "REDIS_URL", "TICK_TOKEN", "PLANNER_MODEL",
                 # model choices from a developer's .env change which retry/fallback path a test takes
                 "AIDER_FALLBACK_MODEL", "PLANNER_FALLBACK_MODEL", "OPENCODE_FALLBACK_MODEL", "CODING_BACKEND", "MODEL_PRICES",
                 # a server's public address in .env would switch self-ping on in tests
                 "SELF_PING_URL", "RENDER_EXTERNAL_URL"):
        monkeypatch.setenv(name, "")
    # Tests that exercise planning use fake Aider runs unless they ask for OpenCode (the production default).
    monkeypatch.setenv("PLANNER_BACKEND", "aider")
    from app import config

    # Never read the developer's .env: its values (models, intervals, URLs...) change which path a test takes.
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()