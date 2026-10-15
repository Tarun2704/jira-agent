"""OpenCode as the coding agent (the alternative to Aider, chosen with CODING_BACKEND=opencode).

This module only knows how to run ONE OpenCode session in a cloned repo and report what happened. Everything
around it (clone, branch, commit, push, PR, Jira, Slack, costs) stays in agent.py and is identical for both backends.

How a run works (details in docs/opencode.md):
  1. build_config()  -> the OpenCode config as JSON, passed inline via OPENCODE_CONFIG_CONTENT (nothing is written
                        into the repo). It denies git writes, web access and sub-agents, and can deny shell access.
  2. build_env()     -> a SCRUBBED environment: only PATH, a private HOME and the API key(s) of the chosen model.
                        Jira/GitHub/Slack/Redis secrets never reach the agent.
  3. run_opencode()  -> `opencode run --format json` as a child process in its own process group, stdin closed,
                        with a hard timeout and a cap on model requests. Its JSON events are parsed as they stream.
  4. render_log()    -> a compact text log (tool calls, final answer, errors) in a format the rest of the app uses
                        for PR bodies, Jira comments and /runs.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class OpenCodeNotInstalled(Exception):
    """The `opencode` executable can't be started."""


# --- configuration given to OpenCode ---------------------------------------------------------------------------

# The agent edits files; the APP commits and pushes. These are blocked by name. Pattern matching is a guard rail for the
# model, not a security boundary (`sh -c 'git commit'` slips through), so agent.py also repairs the repo afterwards.
_GIT_WRITES = (
    "git commit*", "git push*", "git add*", "git rm*", "git mv*", "git checkout*", "git switch*", "git branch*",
    "git reset*", "git restore*", "git stash*", "git rebase*", "git merge*", "git cherry-pick*", "git tag*",
    "git remote*", "git config*", "git fetch*", "git pull*", "git clean*", "git clone*", "git init*", "git worktree*",
    "git submodule*", "git am*", "git apply*", "git revert*",
)

# Names of the API-key variables each provider reads. Only the chosen model's provider key is handed to the agent.
PROVIDER_KEY_VARS: dict[str, tuple[str, ...]] = {
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "google-vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "togetherai": ("TOGETHER_API_KEY",),
    "fireworks-ai": ("FIREWORKS_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
}

# Harmless variables the child needs (certificates and proxies when a network requires them).
_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


def provider_of(model: str) -> str:
    """'google/gemini-2.5-flash' -> 'google' (OpenCode model ids are provider/model)."""
    return model.split("/", 1)[0] if "/" in model else ""


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def build_config(*, allow_bash: bool, extra_json: str = "", read_only: bool = False) -> dict:
    """The OpenCode config for one run. `extra_json` (OPENCODE_EXTRA_CONFIG) is merged on top, e.g. to add a custom provider.
    `read_only` (the planner): no edits and no shell, whatever else is configured."""
    bash: str | dict = {"*": "allow", **{p: "deny" for p in _GIT_WRITES}} if allow_bash and not read_only else "deny"
    cfg: dict = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,  # OpenCode's own undo history is not needed here (the app uses git) and costs time and disk
        "lsp": False,
        "formatter": False,  # never reformat files behind the app's back: diffs must only show the real change
        "permission": {
            "edit": "deny" if read_only else "allow",
            "bash": bash,
            "webfetch": "deny",
            "external_directory": "deny",  # file tools stay inside the cloned repo
            "task": "deny",  # no sub-agents: they multiply cost and are harder to bound
            "question": "deny",  # nobody is there to answer in a headless run; asking would hang
            "doom_loop": "deny",
        },
    }
    if extra_json.strip():
        try:
            extra = json.loads(extra_json)
            if not isinstance(extra, dict):
                raise ValueError("not an object")
            cfg = deep_merge(cfg, extra)
        except ValueError:
            log.warning("OPENCODE_EXTRA_CONFIG is not a JSON object; ignoring it")
    if read_only:  # (after the merge, so extra config can't hand the planner write access)
        cfg["permission"].update(edit="deny", bash="deny")
    return cfg


def build_env(*, models: list[str], home: Path, cache: Path, pass_env: str = "", source: dict | None = None) -> dict[str, str]:
    """The ONLY environment the agent process gets. Notably absent: Jira, GitHub, Slack and Redis credentials."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in _PASSTHROUGH if src.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),  # sessions live here: private to this run and deleted after it
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(cache),  # model list and provider packages: shared between runs so they aren't re-downloaded
        "TERM": "dumb", "NO_COLOR": "1", "CI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",  # a repo's own opencode.json must not change permissions or add tools
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
    })
    wanted: list[str] = []
    for model in models:
        prov = provider_of(model)
        wanted += PROVIDER_KEY_VARS.get(prov) or ((f"{prov.upper().replace('-', '_')}_API_KEY",) if prov else ())
    wanted += [n.strip() for n in pass_env.split(",") if n.strip()]
    for name in wanted:
        if src.get(name):
            env[name] = src[name]
    # Google's SDK inside OpenCode only reads GOOGLE_GENERATIVE_AI_API_KEY, although OpenCode lists GEMINI_API_KEY and
    # GOOGLE_API_KEY as accepted names (it then fails with "API key is missing"). So the app passes the key under that
    # name too: people (and Aider) usually have it as GEMINI_API_KEY.
    if any(provider_of(m) == "google" for m in models) and not env.get("GOOGLE_GENERATIVE_AI_API_KEY"):
        for alias in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if env.get(alias):
                env["GOOGLE_GENERATIVE_AI_API_KEY"] = env[alias]
                break
    return env


def secret_values(env: dict[str, str]) -> list[str]:
    """Key values handed to the agent, so they can be blanked out of anything the app stores or posts."""
    values = [v for k, v in env.items() if len(v) >= 8 and (k.endswith(("_KEY", "_TOKEN", "_SECRET")) or "CREDENTIALS" in k)]
    return list(dict.fromkeys(values))  # one key can be passed under two names: list it once


# One OpenCode run peaks at roughly 550 MB of memory (measured), on top of the web app and a possible Aider planning
# process. On a 512 MB instance the system kills it mid-run, so this is checked up front and reported in /health.
MIN_MEMORY_MB = 1024


def container_memory_mb(paths: tuple[str, ...] = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")) -> int | None:
    """The memory limit of this container in MB (cgroup v2, then v1), or None when there is no limit or it can't be read."""
    for path in paths:
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:  # "max" and absurdly large numbers both mean "no limit"
            return int(raw) // (1024 * 1024)
    return None


# --- running it -------------------------------------------------------------------------------------------------

@dataclass
class OpenCodeRun:
    model: str
    exit_code: int | None = None
    duration_s: float = 0.0
    steps: list[dict] = field(default_factory=list)  # one per model request: sent, received, cache_hit, cache_write, cost
    tools: list[str] = field(default_factory=list)  # short description per tool call
    texts: list[str] = field(default_factory=list)  # assistant messages, in order
    errors: list[str] = field(default_factory=list)
    stopped: str = ""  # "" | "timeout" | "max_steps"
    stderr_tail: str = ""
    version: str = ""

    @property
    def sent(self) -> int:
        return sum(s["sent"] for s in self.steps)

    @property
    def received(self) -> int:
        return sum(s["received"] for s in self.steps)

    @property
    def reported_cost(self) -> float:
        return sum(s["cost"] for s in self.steps)

    @property
    def final_text(self) -> str:
        return next((t.strip() for t in reversed(self.texts) if t.strip()), "")


def _tool_summary(part: dict) -> str:
    state = part.get("state") or {}
    inp = state.get("input") or {}
    name = part.get("tool", "?")
    arg = inp.get("filePath") or inp.get("path") or inp.get("pattern") or inp.get("command") or inp.get("url") or ""
    arg = " ".join(str(arg).split())[:120]
    status = state.get("status", "")
    return f"{name} {arg}".strip() + (f" [{status}]" if status and status != "completed" else "")


def handle_event(run: OpenCodeRun, ev: dict) -> None:
    """Fold one JSON event from `opencode run --format json` into the run summary. Unknown events are ignored."""
    kind, part = ev.get("type"), ev.get("part") or {}
    if kind == "step_finish":
        t = part.get("tokens") or {}
        cache = t.get("cache") or {}
        cache_hit, cache_write = int(cache.get("read") or 0), int(cache.get("write") or 0)
        run.steps.append({
            # Counted the way Aider reports them, so costs.py prices both alike: "sent" includes cache writes,
            # cache hits (cheaper) are separate.
            "sent": int(t.get("input") or 0) + cache_write,
            "received": int(t.get("output") or 0) + int(t.get("reasoning") or 0),
            "cache_hit": cache_hit, "cache_write": cache_write,
            "cost": float(part.get("cost") or 0),
        })
    elif kind == "tool_use":
        run.tools.append(_tool_summary(part))
    elif kind == "text":
        if part.get("text"):
            run.texts.append(part["text"])
    elif kind == "error":
        err = ev.get("error") or {}
        data = err.get("data") or {}
        # one line: render_log() marks each error with "[error] " and only those lines are read back as the error
        msg = " ".join(str(data.get("message") or err.get("message") or json.dumps(err)[:300]).split())
        code = data.get("statusCode")
        run.errors.append(f"{err.get('name', 'Error')}: {msg}" + (f" (HTTP {code})" if code else ""))


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # the agent's shell commands (pytest, node...) die with it
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


_KNOWN_MODELS: dict[str, set[str]] = {}  # provider -> model ids OpenCode knows (per process: the list rarely changes)


def known_models(exe: str, provider: str, env: dict[str, str], config: dict) -> set[str] | None:
    """OpenCode's model ids for `provider`, or None when they can't be listed (then the run itself reports problems).

    Needed because OpenCode reports an unknown model only as "UnknownError: Unexpected server error", with nothing on
    stderr, which can't be told apart from a real crash."""
    if provider in _KNOWN_MODELS:
        return _KNOWN_MODELS[provider]
    try:
        out = subprocess.run([exe, "models", provider], env=dict(env, OPENCODE_CONFIG_CONTENT=json.dumps(config)),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    models = {ln.strip() for ln in out.stdout.splitlines() if ln.strip().startswith(f"{provider}/")}
    if out.returncode != 0 or not models:  # e.g. a custom provider it lists differently: don't guess
        return None
    _KNOWN_MODELS[provider] = models
    return models


def run_opencode(
    *, binary: str, cwd: Path, prompt: str, model: str, env: dict[str, str], config: dict,
    timeout: int, max_steps: int = 0, variant: str = "", title: str = "",
) -> OpenCodeRun:
    """Run `opencode run` once. Raises OpenCodeNotInstalled if the executable is missing; everything else is in the result."""
    exe = shutil.which(binary, path=env.get("PATH")) or shutil.which(binary)
    if not exe:
        raise OpenCodeNotInstalled(f"`{binary}` was not found on PATH")
    known = known_models(exe, provider_of(model), env, config)
    if known is not None and model not in known:
        run = OpenCodeRun(model=model)
        run.errors.append(f"NotFoundError: model {model} is not in OpenCode's model list (`opencode models {provider_of(model)}`)")
        return run
    cmd = [exe, "run", "--model", model, "--format", "json"]
    if variant:
        cmd += ["--variant", variant]
    if title:
        cmd += ["--title", title]
    cmd.append(prompt)
    run = OpenCodeRun(model=model)
    child_env = dict(env, OPENCODE_CONFIG_CONTENT=json.dumps(config))
    err_file = tempfile.TemporaryFile(mode="w+")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,  # with an open stdin `opencode run` waits for input forever
            stdout=subprocess.PIPE, stderr=err_file, text=True, start_new_session=True,
        )
    except OSError as e:
        raise OpenCodeNotInstalled(str(e)) from e

    def on_timeout() -> None:
        run.stopped = "timeout"
        _kill_group(proc)

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                handle_event(run, json.loads(line))
            except ValueError:
                continue
            if max_steps and len(run.steps) >= max_steps and not run.stopped:
                run.stopped = "max_steps"
                _kill_group(proc)
                break
        proc.wait()
    finally:
        timer.cancel()
        _kill_group(proc)  # also stops anything the agent left running
        run.duration_s = round(time.monotonic() - t0, 1)
        run.exit_code = proc.returncode
        try:
            err_file.seek(0)
            run.stderr_tail = err_file.read()[-1500:].strip()
        finally:
            err_file.close()
    if not run.errors and run.exit_code not in (0, None) and not run.stopped:
        # e.g. an invalid config: OpenCode prints the reason on stderr and exits with 1 without any JSON event
        run.errors.append(f"OpenCode exited with code {run.exit_code}: {run.stderr_tail[-600:] or '(no message)'}")
    return run


# --- the text log the rest of the app works with -----------------------------------------------------------------

def render_log(run: OpenCodeRun, redact=lambda s: s) -> str:
    """Compact transcript. Markers at the start of a line ([opencode] [tool] [said] [error] [stopped]) are what
    opencode_problem() and opencode_reply() read, so keep them stable."""
    lines = [
        f"[opencode] model={run.model} | model requests={len(run.steps)} | tokens in={run.sent} out={run.received}"
        f" | {run.duration_s}s | exit={run.exit_code}"
    ]
    lines += [f"[tool] {t}" for t in run.tools[:80]]
    if len(run.tools) > 80:
        lines.append(f"[tool] ... and {len(run.tools) - 80} more")
    if run.final_text:
        lines += ["[said]", run.final_text]
    lines += [f"[error] {e}" for e in run.errors]
    if run.stopped == "max_steps":
        lines.append(f"[stopped] step limit reached ({len(run.steps)} model requests); the work may be incomplete")
    elif run.stopped == "timeout":
        lines.append("[stopped] timed out")
    return redact("\n".join(lines))


def opencode_problem(log_text: str, classify) -> str | None:
    """A plain-words reason if the run hit an LLM/provider problem. `classify` is agent.llm_problem (shared wording).

    Only the [error] lines are classified: the agent's own explanation may contain words like 'limit' or 'error'."""
    errors = [ln[len("[error] "):] for ln in log_text.splitlines() if ln.startswith("[error] ")]
    if not errors:
        return None
    if "model requests=0 " in log_text and errors[0].startswith("UnknownError"):
        # OpenCode's catch-all before it reached the model (it gives no details): another model may well work
        return (f"OpenCode failed before its first model request and gave no reason (often a model or provider it can't "
                f"use).\nOpenCode said: {errors[0][:300]}")
    return classify("\n".join(errors)) or f"OpenCode reported an error: {errors[0][:300]}"


def opencode_reply(log_text: str, limit: int = 1200) -> str:
    """The agent's final message (what it says it did, or why it changed nothing)."""
    out, on = [], False
    for ln in log_text.splitlines():
        if ln == "[said]":
            on = True
        elif ln.startswith(("[tool]", "[error]", "[stopped]", "[opencode]")):
            on = False
        elif on:
            out.append(ln)
    return "\n".join(out).strip()[-limit:]


# --- prompt -----------------------------------------------------------------------------------------------------------

_RULES_COMMON = (
    "\n\nHow to work here:\n"
    "- You are running unattended in a fresh clone of the repository. Edit files directly.\n"
    "- Do NOT run git commands that change anything (commit, add, push, checkout, reset, stash, branch, config...). "
    "The system commits and pushes your changes itself.\n"
    "- Do not create notes, logs or scratch files; leave no files behind that the change doesn't need.\n"
)
_RULES_BASH = (
    "- You may run the project's existing tests or linters to check your work if they run quickly. "
    "Do not install dependencies (no pip/npm/apt installs) and do not use the network; if something can't run without "
    "installing, skip it and say so.\n"
)
_RULES_NO_BASH = "- You cannot run shell commands here: read, search and edit files only, and check your work by re-reading it.\n"
_RULES_END = "- Finish with a short plain-text summary (2-5 sentences): what you changed and why, or, if you changed nothing, why not."


_RULES_PLAN = (
    "\n\nHow to work here:\n"
    "- You are PLANNING only, unattended, in a fresh clone of the repository. Nobody can answer questions while you work.\n"
    "- Search the code (grep for the names, messages and terms in the ticket; glob for files) and open the files involved "
    "before deciding what to change. Name real paths that exist in the repository.\n"
    "- Do not edit or create files and do not run commands: you can only read and search.\n"
    "- Your LAST message must be the answer in exactly the layout asked for above (markers on their own lines), "
    "with nothing after it."
)


def build_plan_prompt(planner_prompt: str, files: list[str]) -> str:
    hint = ""
    if files:
        hint = "\n\nFiles the ticket names (start there; search for others): " + ", ".join(f"`{f}`" for f in files)
    return planner_prompt + hint + _RULES_PLAN


def build_prompt(task_prompt: str, files: list[str], allow_bash: bool = False) -> str:
    hint = ""
    if files:
        hint = "\n\nFiles most likely relevant (start there; look elsewhere if needed): " + ", ".join(f"`{f}`" for f in files)
    return task_prompt + hint + _RULES_COMMON + (_RULES_BASH if allow_bash else _RULES_NO_BASH) + _RULES_END
