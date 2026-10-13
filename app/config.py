"""Settings loaded from environment variables (or a local .env file)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Jira
    jira_base_url: str  # e.g. https://yourname.atlassian.net
    jira_email: str
    jira_api_token: str

    # GitHub
    github_token: str  # fine-grained PAT: Contents RW + Pull requests RW
    github_repo: str  # "owner/repo"

    # LLM (Aider reads the provider key from the env itself, e.g. GEMINI_API_KEY)
    aider_model: str = "gemini/gemini-2.5-flash"
    # "diff" = search/replace blocks, so the model only touches the lines it changes
    # (Aider falls back to "whole"-file rewrites for models it doesn't know).
    aider_edit_format: str = "diff"
    # Groq's free tier caps each request (prompt + reply, incl. reasoning) at ~8k tokens per
    # minute, so keep requests small: skip the repo map when the ticket names its files, and
    # keep reasoning short. Empty reasoning effort = don't send the setting.
    aider_map_tokens_when_files_named: int = 0
    aider_reasoning_effort: str = ""
    aider_timeout_seconds: int = 900
    # A rate-limited model run (no fallback model set) waits this long and tries once more; 0 = don't
    rate_limit_wait_seconds: int = 45

    # Webhook auth: Jira's HMAC secret, also accepted as ?token=<secret>
    webhook_secret: str

    # Behaviour
    trigger_label: str = "ai-agent"
    # Added while a ticket is queued/running, removed when done. Tickets still carrying it
    # after a restart are re-queued on startup. Empty = no persistence.
    queue_label: str = "ai-agent-queued"
    recovery_delay_seconds: int = 120

    # --- Plan-first workflow (Slack + Redis). Leave these empty to keep the old direct flow. ---
    slack_bot_token: str = ""  # xoxb-... (Bot User OAuth Token)
    slack_signing_secret: str = ""
    slack_user_id: str = ""  # the one person who gets the DMs and may approve plans
    redis_url: str = ""  # rediss://default:<password>@<host>:6379
    tick_token: str = ""  # secret for GET /tick, called by an external pinger every ~10 min
    # Model for planning/revising. Empty = same as AIDER_MODEL (change later for a stronger one).
    planner_model: str = ""
    planner_timeout_seconds: int = 600
    plan_reminder_hours: int = 48  # no reply for this long -> Jira comment tagging the reporter
    lease_seconds: int = 300  # a running job refreshes its lease; a lapsed lease = crashed job
    # Planning/revising gets its own single worker, so a long coding run never delays your replies.
    # Costs up to two Aider processes at once; set false on a small instance to share one worker.
    separate_plan_queue: bool = True
    # How many plans/revisions may run at once (each is a clone plus an Aider process, roughly 200-300 MB).
    # 1 is safe on a 512 MB instance; raise it where there is memory and LLM rate limit to spare.
    plan_workers: int = 1
    # Follow-ups: a delivered ticket stays open for more requests this long, then counts as completed.
    followup_days: int = 14
    # Used once, for the same call, when the main model is rate-limited or down. Empty = no fallback.
    planner_fallback_model: str = ""
    aider_fallback_model: str = ""
    # A "Looking into it..." message older than this, with nothing working on it, is closed on the next tick.
    ack_stale_seconds: int = 1800

    # Cost tracking: USD per 1M tokens as JSON, {"model name": [input, output]}. Empty = costs show as unknown
    # (tokens and request counts are still recorded). Free-tier models can be listed as [0, 0].
    model_prices: str = ""
    # Add "Used about $X" to the delivery message in Slack.
    show_cost_in_slack: bool = True

    # Jira statuses the agent moves tickets to (empty = don't move). Must exist in the workflow.
    jira_status_in_progress: str = "In Progress"
    jira_status_in_review: str = "In Review"
    jira_status_on_failure: str = "To Do"  # failed or no changes
    open_draft_pr: bool = True
    workdir: str = "/tmp/jira-agent"


    @property
    def planning_enabled(self) -> bool:
        """Plan-first mode needs Slack (to talk to you) and Redis (to remember the chat)."""
        return bool(self.slack_bot_token and self.slack_user_id and self.redis_url)

    def config_problems(self) -> list[str]:
        """Misconfigurations that otherwise fail quietly (e.g. the DM arrives but replies don't)."""
        have = {"SLACK_BOT_TOKEN": self.slack_bot_token, "SLACK_USER_ID": self.slack_user_id, "REDIS_URL": self.redis_url}
        problems = []
        if any(have.values()) and not all(have.values()):
            missing = ", ".join(k for k, v in have.items() if not v)
            problems.append(f"Plan-first mode is OFF because {missing} is empty (tickets go straight to a PR).")
        if self.planning_enabled:
            if not self.slack_signing_secret:
                problems.append("SLACK_SIGNING_SECRET is empty: plans are sent but your replies and button clicks are rejected.")
            if not self.tick_token:
                problems.append("TICK_TOKEN is empty: /tick is disabled, so crashed jobs and reminders are never recovered.")
            if not self.redis_url.startswith(("redis://", "rediss://", "unix://")):
                problems.append("REDIS_URL must start with rediss:// (Upstash) or redis://.")
        return problems

    @property
    def effective_planner_model(self) -> str:
        return self.planner_model or self.aider_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
