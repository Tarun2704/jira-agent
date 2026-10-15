"""Smoke-test the OpenCode backend with your real settings: one tiny task in a throwaway git repo.

Checks that the `opencode` executable runs, that your model and API key work, that it can edit a file, and shows what
the run cost in tokens. It costs a few cents at most. Nothing is sent to Jira or GitHub.

Usage (on your machine, or in the Render shell):  python scripts/check_opencode.py [--bash]
(--bash also lets the agent run shell commands for this check; otherwise OPENCODE_ALLOW_BASH decides.)
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import opencode_runner as oc  # noqa: E402
from app.config import get_settings  # noqa: E402

s = get_settings()
allow_bash = s.opencode_allow_bash or "--bash" in sys.argv
ok = True


def report(name: str, passed: bool, detail: str) -> None:
    global ok
    ok &= passed
    print(f"{'✅' if passed else '❌'} {name}: {detail}")


exe = shutil.which(s.opencode_bin)
report("opencode installed", bool(exe), exe or f"`{s.opencode_bin}` not found on PATH (the Dockerfile installs it; locally: npm i -g opencode-ai)")
if not exe:
    sys.exit(1)
version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
report("version", bool(version), version or "could not read it")
report("settings", "/" in s.opencode_model, f"CODING_BACKEND={s.coding_backend} model={s.opencode_model} bash={'on' if allow_bash else 'off'} "
       f"max_requests={s.opencode_max_steps or 'unlimited'} timeout={s.opencode_timeout_seconds}s")

work = Path(tempfile.mkdtemp(prefix="opencode-check-"))
try:
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    env = oc.build_env(models=[s.opencode_model], home=work / "home", cache=work / "cache", pass_env=s.opencode_pass_env)
    key_names = [k for k in env if k.endswith("_KEY") or "CREDENTIALS" in k]
    report("API key reaches the agent", bool(key_names), ", ".join(key_names) or
           f"no key found for provider '{oc.provider_of(s.opencode_model)}': set its *_API_KEY variable (or OPENCODE_PASS_ENV)")
    task = ("Add a function `multiply(a, b)` to calc.py and a test for it in test_calc.py. "
            "Keep the change small.")
    run = oc.run_opencode(
        binary=s.opencode_bin, cwd=repo, prompt=oc.build_prompt(task, ["calc.py", "test_calc.py"], allow_bash), model=s.opencode_model, env=env,
        config=oc.build_config(allow_bash=allow_bash, extra_json=s.opencode_extra_config),
        timeout=min(s.opencode_timeout_seconds, 300), max_steps=s.opencode_max_steps, variant=s.opencode_variant, title="check",
    )
    report("run finished", run.exit_code == 0 and not run.errors and not run.stopped,
           f"exit={run.exit_code} stopped={run.stopped or 'no'} errors={run.errors or 'none'} in {run.duration_s}s")
    code = (repo / "calc.py").read_text()
    report("file was edited", "multiply" in code, "calc.py now has multiply()" if "multiply" in code else "calc.py is unchanged")
    report("no git changes by the agent", subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout.strip() == "",
           "the agent did not commit (the app does that)")
    print(f"\nUsage: {len(run.steps)} model request(s), {run.sent} tokens sent, {run.received} received"
          f" (OpenCode's own cost figure: ${run.reported_cost:.4f})")
    print("Tools used:", "; ".join(run.tools) or "none")
    print("Agent said:", run.final_text[:500] or "(nothing)")
    if run.stderr_tail and not run.errors:
        print("stderr:", run.stderr_tail[-300:])
finally:
    shutil.rmtree(work, ignore_errors=True)
print("\nAll good." if ok else "\nSomething needs attention (see ❌ above).")
sys.exit(0 if ok else 1)
