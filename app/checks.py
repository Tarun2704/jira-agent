"""Checks run on the agent's changes before the PR is opened."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    status: str  # "passed" | "failed" | "skipped"
    summary: str
    errors: list[str] = field(default_factory=list)
    retried: bool = False  # True if the agent had to fix its own change

    @property
    def failed_files(self) -> list[str]:
        return [e.split(":", 1)[0] for e in self.errors]


def python_syntax_check(repo_dir: Path, changed: list[str]) -> CheckResult:
    """Compile each changed .py file in memory (no .pyc files are written)."""
    files = [f for f in changed if f.endswith(".py") and (repo_dir / f).is_file()]
    if not files:
        return CheckResult("Python syntax", "skipped", "No Python files changed.")
    errors = []
    for f in files:
        try:
            compile((repo_dir / f).read_bytes(), f, "exec", dont_inherit=True)
        except SyntaxError as e:
            errors.append(f"{f}: line {e.lineno}: {e.msg}" + (f"\n    {e.text.strip()}" if e.text else ""))
        except ValueError as e:  # e.g. null bytes in source
            errors.append(f"{f}: {e}")
    names = ", ".join(f"`{f}`" for f in files)
    if errors:
        return CheckResult("Python syntax", "failed", f"Syntax errors in {len(errors)} of {len(files)} file(s).", errors)
    return CheckResult("Python syntax", "passed", f"{len(files)} changed file(s) compile: {names}.")


_ICONS = {"passed": "✅", "failed": "❌", "skipped": "➖"}


def format_checks_md(results: list[CheckResult]) -> str:
    lines = ["## Checks"]
    for r in results:
        note = " (the agent fixed a syntax error in its first attempt)" if r.retried and r.status == "passed" else ""
        lines.append(f"- {_ICONS[r.status]} **{r.name}**: {r.summary}{note}")
        if r.errors:
            lines.append("  ```")
            lines.extend(f"  {line}" for e in r.errors for line in e.splitlines())
            lines.append("  ```")
    if any(r.status == "failed" for r in results):
        lines.append("\n> ⚠️ **Checks failed.** Do not merge until the errors above are fixed.")
    return "\n".join(lines)
