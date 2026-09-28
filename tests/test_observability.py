import logging

from app.jobs import Job, JobStore, RedactTokenFilter, parse_aider_tokens
from tests.test_webhook import SECRET, _issue, _post, client  # noqa: F401  (fixture)


def test_parse_aider_tokens():
    out = "Tokens: 2.5k sent, 155 received. Cost: $0.0004\n...\nTokens: 6.8k sent, 1.1k received.\n"
    assert parse_aider_tokens(out) == (9300, 1255)
    assert parse_aider_tokens("no usage here") == (0, 0)


def test_job_lifecycle_and_summary():
    job = Job(issue_key="CA-3", event="jira:issue_created")
    assert job.status == "queued" and job.label.startswith("CA-3#")
    job.start()
    with job.step("aider"):
        pass
    job.add_tokens(100, 20)
    job.pr_url = "https://github.com/o/r/pull/4"
    job.finish("succeeded")
    d = job.to_dict()
    assert d["status"] == "succeeded" and "aider" in d["steps"] and "_t0" not in d
    s = job.summary()
    assert "status=succeeded" in s and "100 sent / 20 received" in s and "pull/4" in s


def test_store_filters_by_issue_newest_first():
    store = JobStore()
    store.add_job(Job("CA-1", "e"))
    store.add_job(Job("CA-2", "e"))
    store.record_webhook(event="e", issue="CA-2", outcome="ignored", reason="x")
    snap = store.snapshot("CA-2")
    assert [j["issue_key"] for j in snap["jobs"]] == ["CA-2"]
    assert snap["webhooks"][0]["reason"] == "x"
    assert [j["issue_key"] for j in store.snapshot()["jobs"]] == ["CA-2", "CA-1"]


def test_jobs_endpoint_requires_token(client):  # noqa: F811
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs?token=wrong").status_code == 401
    assert client.get(f"/jobs?token={SECRET}").status_code == 200
    assert client.get("/jobs", headers={"Authorization": f"Bearer {SECRET}"}).status_code == 200


def test_webhooks_recorded_with_reason(client):  # noqa: F811
    _post(client, _issue("CA-7", labels=["other"]))
    r = _post(client, _issue("CA-8"))
    assert r.json()["job_id"]
    hooks = client.get(f"/jobs?token={SECRET}").json()["webhooks"]
    by_issue = {h["issue"]: h for h in hooks}
    assert by_issue["CA-7"]["outcome"] == "ignored" and "missing label" in by_issue["CA-7"]["reason"]
    assert by_issue["CA-8"]["outcome"] == "accepted"
    jobs = client.get(f"/jobs?token={SECRET}&issue=CA-8").json()["jobs"]
    assert jobs[0]["status"] == "queued"


def test_bad_signature_is_recorded(client):  # noqa: F811
    _post(client, _issue(), secret="wrong")
    hooks = client.get(f"/jobs?token={SECRET}").json()["webhooks"]
    assert hooks[0]["outcome"] == "rejected"


def test_access_log_token_redacted():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET", "/jobs?token=s3cret&issue=CA-1"), None)
    RedactTokenFilter().filter(rec)
    assert "s3cret" not in rec.getMessage() and "token=***" in rec.getMessage()
