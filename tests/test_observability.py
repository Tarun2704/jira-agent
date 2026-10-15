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


def test_aider_model_settings_file_is_valid():
    """A comment glued onto a value ("BLOCK_NONE# ...") once made Google reject every Aider call with a 400."""
    from pathlib import Path

    import yaml

    from app.agent import AIDER_MODEL_SETTINGS_FILE

    entries = yaml.safe_load(Path(AIDER_MODEL_SETTINGS_FILE).read_text())
    assert all("name" in e for e in entries)
    for e in entries:
        for setting in (e.get("extra_params") or {}).get("safety_settings", []):
            assert setting["threshold"] in {"BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE", "OFF"}


def test_gemini_3_gets_no_temperature_from_aider_or_direct_calls(tmp_path):
    """litellm: a temperature below 1.0 can make Gemini 3 models loop or reason worse; Aider would send 0."""
    import yaml

    from aider.models import Model, register_models

    from app.agent import AIDER_MODEL_SETTINGS_FILE, aider_settings_file
    from app.pr_writer import takes_temperature

    assert not takes_temperature("gemini/gemini-3.5-flash-lite") and not takes_temperature("claude-opus-5-5")
    assert takes_temperature("gemini/gemini-2.5-flash") and takes_temperature("groq/llama-3.3-70b-versatile")
    assert aider_settings_file("gemini/gemini-2.5-flash", tmp_path) == AIDER_MODEL_SETTINGS_FILE  # nothing to add
    path = aider_settings_file("gemini/gemini-3.5-flash-lite", tmp_path)
    entries = yaml.safe_load(path.read_text())
    assert {"name": "gemini/gemini-3.5-flash-lite", "use_temperature": False} in entries
    assert any(e["name"] == "gemini/gemini-2.5-flash" for e in entries)  # the shared settings are kept
    register_models([str(path)])
    assert Model("gemini/gemini-3.5-flash-lite").use_temperature is False  # Aider really reads it that way


def test_logs_never_show_api_keys_or_tokens(capsys):
    """2026-10-14: a failed Gemini call logged Google's request URL with ?key=<the API key> in the traceback."""
    import logging

    from app.jobs import RedactingFormatter, secret_values

    env = {"GEMINI_API_KEY": "AQ.secret-gemini-key-123", "GITHUB_TOKEN": "ghp_supersecret999", "REDIS_URL": "rediss://default:redispass123@h:6379",
           "AIDER_MODEL": "gemini/gemini-2.5-flash"}
    secrets = secret_values(env)
    assert "redispass123" in secrets and "gemini/gemini-2.5-flash" not in secrets
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(logging.Formatter("%(message)s"), secrets))
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    try:
        raise RuntimeError("503 for url 'https://generativelanguage.googleapis.com/v1/models/x:generateContent?key=AIzaUnlisted0000&alt=sse'")
    except RuntimeError:
        log.warning("call failed with ghp_supersecret999 and https://x-access-token:ghp_other777@github.com/o/r", exc_info=True)
    log.removeHandler(handler)
    err = capsys.readouterr().err
    assert "AIzaUnlisted0000" not in err and "ghp_supersecret999" not in err and "ghp_other777" not in err
    assert "key=***" in err and "Traceback" in err  # the traceback is still there, just masked
