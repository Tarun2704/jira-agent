import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "test-secret"


@pytest.fixture
def client(monkeypatch):
    for k, v in {
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_EMAIL": "a@b.c",
        "JIRA_API_TOKEN": "x",
        "GITHUB_TOKEN": "x",
        "GITHUB_REPO": "o/r",
        "WEBHOOK_SECRET": SECRET,
        "TRIGGER_LABEL": "ai-agent",
    }.items():
        monkeypatch.setenv(k, v)

    from app import config, main

    config.get_settings.cache_clear()
    submitted = []
    monkeypatch.setattr(main._executor, "submit", lambda fn, key: submitted.append(key))
    main._in_flight.clear()
    c = TestClient(main.app)
    c.submitted = submitted
    return c


def _post(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/jira-webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature": f"sha256={sig}"},
    )


def _issue(key="DEMO-1", labels=("ai-agent",)):
    return {"webhookEvent": "jira:issue_created", "issue": {"key": key, "fields": {"labels": list(labels)}}}


def test_valid_signature_queues_job(client):
    r = _post(client, _issue())
    assert r.status_code == 202 and r.json()["accepted"]
    assert client.submitted == ["DEMO-1"]


def test_bad_signature_rejected(client):
    assert _post(client, _issue(), secret="wrong").status_code == 401
    assert client.submitted == []


def test_query_token_auth(client):
    r = client.post(f"/jira-webhook?token={SECRET}", json=_issue())
    assert r.status_code == 202 and r.json()["accepted"]


def test_missing_label_ignored(client):
    r = _post(client, _issue(labels=["other"]))
    assert not r.json()["accepted"]
    assert client.submitted == []


def test_duplicate_while_in_flight_ignored(client):
    _post(client, _issue())
    r = _post(client, _issue())
    assert r.json()["reason"] == "already queued"
    assert client.submitted == ["DEMO-1"]


def test_update_adding_label_triggers(client):
    p = _issue()
    p["webhookEvent"] = "jira:issue_updated"
    p["changelog"] = {"items": [{"field": "labels", "fromString": "bug", "toString": "bug ai-agent"}]}
    assert _post(client, p).json()["accepted"]


def test_comment_update_ignored(client):
    # Jira sends issue_updated for comments (including the agent's own) - must not loop.
    p = _issue()
    p["webhookEvent"] = "jira:issue_updated"
    p["comment"] = {"body": "🤖 Coding agent finished but made no changes."}
    r = _post(client, p)
    assert not r.json()["accepted"]
    assert client.submitted == []


def test_branch_name():
    from app.agent import branch_name
    from app.jira_client import JiraIssue

    issue = JiraIssue("DEMO-7", "Fix: login button crashes on Safari!", "", [], "Bug")
    assert branch_name(issue) == "ai/DEMO-7-fix-login-button-crashes-on-safari"


def test_crlf_preserved(tmp_path):
    import subprocess

    from app.agent import crlf_files, restore_crlf

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=tmp_path, check=True)
    (tmp_path / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "unix.py").write_bytes(b"a = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert crlf_files(tmp_path) == {"win.py"}

    (tmp_path / "win.py").write_bytes(b"a = 1\nb = 3\n")  # model rewrote with LF
    restore_crlf(tmp_path, {"win.py"})
    assert (tmp_path / "win.py").read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_mentioned_files(tmp_path):
    from app.agent import mentioned_files
    from app.jira_client import JiraIssue

    (tmp_path / "data").mkdir()
    for name, size in [("Crimes in India Dashboard.py", 10), ("data/big.csv", 300_000), ("util.py", 10)]:
        (tmp_path / name).write_bytes(b"x" * size)
    tracked = ["Crimes in India Dashboard.py", "data/big.csv", "util.py"]

    issue = JiraIssue("CA-1", "Fix path", "In `Crimes in India Dashboard.py` and big.csv ...", [], "Bug")
    assert mentioned_files(issue, tracked, tmp_path) == ["Crimes in India Dashboard.py"]
