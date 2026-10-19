"""Settings loaded from environment variables (or a local .env file)."""
import shutil
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

    # --- Coding backend: who edits the files. "aider" (default, unchanged) or "opencode" (an agent that explores the
    # repo, edits and can run tests). Only the CODING step switches; planning, PR descriptions, git, Jira and Slack are
    # the same for both. Change it, redeploy/restart, and run the same ticket again to compare cost and quality.
    coding_backend: str = "aider"
    opencode_bin: str = "opencode"
    # OpenCode model ids are provider/model, e.g. google/gemini-2.5-flash (reads GEMINI_API_KEY), openrouter/<model>, openai/<model>.
    opencode_model: str = "google/gemini-2.5-flash"
    opencode_fallback_model: str = ""  # used once, like AIDER_FALLBACK_MODEL, when the main model is rate-limited or down
    opencode_timeout_seconds: int = 900
    # Stop the run after this many model requests (each tool-use round trip is one). 0 = no cap. Bounds cost per ticket.
    opencode_max_steps: int = 40
    # false (default) = the agent may not run shell commands (it can still read, search and edit). true = it may run
    # commands such as the repo's tests, but then ticket text could try to make it run something harmful: read docs/opencode.md first.
    opencode_allow_bash: bool = False
    opencode_variant: str = ""  # provider-specific reasoning effort (e.g. low/high), passed as --variant. Empty = model default
    opencode_pass_env: str = ""  # extra env var NAMES (comma separated) to hand to the agent, e.g. for a custom provider key
    opencode_extra_config: str = ""  # JSON merged into the generated OpenCode config (e.g. a custom provider)
    # When OpenCode itself fails (crashed or killed for memory, stuck, timed out, not installed, or used up its requests
    # without changing anything), Aider does that planning/coding step instead. Model problems use the backup models.
    tool_fallback: bool = True

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
    # Who reads the code and writes the plan (independent of CODING_BACKEND):
    #   "opencode" (default): a read-only OpenCode run (no edits, no shell) that searches file contents and reads the
    #     code it plans to change. Right on big repos; ~3x the model requests (see docs/planner-comparison.md).
    #   "aider": ~2 model requests. Files come from the ticket or from matching their PATHS against the ticket's words,
    #     plus Aider's repo map. Cheaper; fine on small repos, but on big ones it can plan around code it never saw.
    planner_backend: str = "opencode"
    opencode_planner_model: str = ""  # provider/model for the OpenCode planner. Empty = OPENCODE_MODEL
    opencode_plan_max_steps: int = 20  # model requests one OpenCode plan may use
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

    # The app calls its own public /tick every SELF_PING_INTERVAL_SECONDS. That runs the tick without an outside
    # pinger and, on Render's free tier, counts as traffic so the service doesn't go to sleep. Needs the public URL:
    # Render provides it as RENDER_EXTERNAL_URL; set SELF_PING_URL to use another address. Off when neither is set
    # (e.g. on your own machine) or when SELF_PING_ENABLED=false.
    self_ping_enabled: bool = True
    self_ping_url: str = ""
    render_external_url: str = ""  # set by Render itself
    self_ping_interval_seconds: int = 600
    # After this many failed self-pings in a row you get one Slack message (and one when it recovers).
    self_ping_alert_after: int = 3
    # A running tick holds a lock this long at most (a crashed tick can't block the next ones forever).
    tick_lock_seconds: int = 300
    # Every this many seconds the app itself re-queues tickets whose job died (no lease) and re-sends Slack messages
    # that couldn't be delivered. 0 = off (then only /tick does the first, and nothing does the second).
    watchdog_seconds: int = 60
    # A running job that shows no sign of life this long (no step, no output line, no agent event) is stopped by the
    # watchdog: its processes are killed, then it falls back to Aider (OpenCode) or is run once more by itself before
    # it is reported as failed. One model reply can take a few minutes, so keep this well above that. 0 = off.
    job_stuck_seconds: int = 420

    # Cost tracking: prices for the usual models are built in (app/costs.py DEFAULT_PRICES). This adds models or
    # overrides a built-in price: USD per 1M tokens as JSON, {"model name": [input, output]}. Free tiers: [0, 0].
    model_prices: str = ""
    # Add "Used about $X" to the delivery message in Slack.
    show_cost_in_slack: bool = True

    # Jira statuses the agent moves tickets to (empty = don't move). Must exist in the workflow.
    jira_status_in_progress: str = "In Progress"
    jira_status_in_review: str = "In Review"
    jira_status_on_failure: str = "To Do"  # failed or no changes
    jira_status_done: str = "Done"  # the PR was merged. Empty = don't move the ticket
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
        if self.planner_backend.strip().lower() not in ("aider", "opencode"):
            problems.append(f"PLANNER_BACKEND={self.planner_backend!r} is not 'aider' or 'opencode': using OpenCode.")
        if self.coding_backend.strip().lower() not in ("aider", "opencode"):
            problems.append(f"CODING_BACKEND={self.coding_backend!r} is not 'aider' or 'opencode': using Aider.")
        users = [n for n, b in (("CODING_BACKEND", self.backend), ("PLANNER_BACKEND", self.planner_backend_used)) if b == "opencode"]
        if users:  # OpenCode plans and/or codes
            if not shutil.which(self.opencode_bin):
                problems.append(f"{' and '.join(users)}=opencode but `{self.opencode_bin}` is not installed on this machine: "
                                f"those jobs will fail. Install it, or set {'/'.join(users)}=aider.")
            models = [("OPENCODE_MODEL", self.opencode_model), ("OPENCODE_FALLBACK_MODEL", self.opencode_fallback_model)]
            if self.planner_backend_used == "opencode":
                models.append(("OPENCODE_PLANNER_MODEL", self.opencode_planner_model))
            for name, model in models:
                if not model:
                    continue
                if "/" not in model:
                    problems.append(f"{name} must look like provider/model (e.g. google/gemini-2.5-flash).")
                elif model.startswith("gemini/"):  # the Aider/litellm spelling: OpenCode answers "Provider not found: gemini"
                    problems.append(f"{name}={model!r} uses Aider's name for Google; OpenCode needs google/{model.split('/', 1)[1]}.")
            from app.opencode_runner import MIN_MEMORY_MB, container_memory_mb

            mem = container_memory_mb()
            if mem is not None and mem < MIN_MEMORY_MB:
                problems.append(
                    f"This instance has about {mem} MB of memory; one OpenCode run needs roughly 550 MB on top of the app, so "
                    f"{' and '.join(users)}=opencode jobs will likely be killed (out of memory). Use an instance with at least "
                    f"1 GB (2 GB if planning and coding can run at the same time), or set {'/'.join(users)}=aider."
                )
        return problems

    @property
    def self_ping_base(self) -> str:
        """The public address to ping, or "" when self-ping should not run."""
        if not (self.self_ping_enabled and self.planning_enabled and self.tick_token):
            return ""
        return (self.self_ping_url or self.render_external_url).strip()

    @property
    def effective_planner_model(self) -> str:
        return self.planner_model or self.aider_model

    @property
    def backend(self) -> str:
        """The coding backend actually used: anything other than "opencode" means Aider."""
        return "opencode" if self.coding_backend.strip().lower() == "opencode" else "aider"

    @property
    def planner_backend_used(self) -> str:
        """Anything other than "aider" means OpenCode (the default)."""
        return "aider" if self.planner_backend.strip().lower() == "aider" else "opencode"

    @property
    def coder_model(self) -> str:
        return self.opencode_model if self.backend == "opencode" else self.aider_model

    @property
    def coder_fallback_model(self) -> str:
        return self.opencode_fallback_model if self.backend == "opencode" else self.aider_fallback_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
