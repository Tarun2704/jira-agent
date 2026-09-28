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
    aider_timeout_seconds: int = 900

    # Webhook auth: Jira's HMAC secret, also accepted as ?token=<secret>
    webhook_secret: str

    # Behaviour
    trigger_label: str = "ai-agent"
    open_draft_pr: bool = True
    workdir: str = "/tmp/jira-agent"


@lru_cache
def get_settings() -> Settings:
    return Settings()
