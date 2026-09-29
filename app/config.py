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

    # Webhook auth: Jira's HMAC secret, also accepted as ?token=<secret>
    webhook_secret: str

    # Behaviour
    trigger_label: str = "ai-agent"
    # Added while a ticket is queued/running, removed when done. Tickets still carrying it
    # after a restart are re-queued on startup. Empty = no persistence.
    queue_label: str = "ai-agent-queued"
    recovery_delay_seconds: int = 120

    # Jira statuses the agent moves tickets to (empty = don't move). Must exist in the workflow.
    jira_status_in_progress: str = "In Progress"
    jira_status_in_review: str = "In Review"
    jira_status_on_failure: str = "To Do"  # failed or no changes
    open_draft_pr: bool = True
    workdir: str = "/tmp/jira-agent"


@lru_cache
def get_settings() -> Settings:
    return Settings()
