from app import pr_writer
from app.jira_client import JiraIssue

ISSUE = JiraIssue("CA-2", "Replace deprecated Dash imports", "Use from dash import dcc, html", ["ai-agent"], "Task")
DESC = """## Summary
The dashboard used removed Dash packages.
This switches to the modern import.

## Changes
- `app.py`: replaced two imports with `from dash import dcc, html`.

## How to verify
1. Run the dashboard."""


def test_describe_changes_strips_fence_and_crlf(monkeypatch):
    seen = {}

    def fake(model, prompt):
        seen["prompt"] = prompt
        return f"```markdown\n{DESC}\n```"

    monkeypatch.setattr(pr_writer, "_complete", fake)
    out = pr_writer.describe_changes("m", ISSUE, "-a\r\n+b\r\n")
    assert out == DESC
    assert "\r" not in seen["prompt"] and "CA-2" in seen["prompt"]


def test_describe_changes_returns_none_on_error_or_junk(monkeypatch):
    def boom(model, prompt):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(pr_writer, "_complete", boom)
    assert pr_writer.describe_changes("m", ISSUE, "diff") is None

    monkeypatch.setattr(pr_writer, "_complete", lambda m, p: "Sure! Here is a description.")
    assert pr_writer.describe_changes("m", ISSUE, "diff") is None


def test_summary_line():
    assert pr_writer.summary_line(DESC) == "The dashboard used removed Dash packages. This switches to the modern import."
    assert pr_writer.summary_line(None) == ""


def test_pr_body_with_and_without_description():
    kw = dict(issue=ISSUE, jira_url="https://x/browse/CA-2", diff_stat=" app.py | 3 +-", agent_log="log")
    body = pr_writer.build_pr_body(description=DESC, **kw)
    assert body.startswith("## Summary\nThe dashboard") and "[CA-2](https://x/browse/CA-2)" in body

    fallback = pr_writer.build_pr_body(description=None, **kw)
    assert "could not be generated" in fallback and "app.py | 3 +-" in fallback
