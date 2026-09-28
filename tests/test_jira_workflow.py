"""Jira status moves and queue persistence."""
import httpx

from app.jira_client import JiraClient
from app.jobs import Job
from tests.test_webhook import _issue, _post, client  # noqa: F401  (fixture)


def _jira_with(handler) -> JiraClient:
    jira = JiraClient.__new__(JiraClient)
    jira._client = httpx.Client(base_url="https://x.atlassian.net", transport=httpx.MockTransport(handler))
    return jira


def _workflow(current: str, posted: list):
    transitions = [
        {"id": "11", "to": {"name": "To Do"}},
        {"id": "21", "to": {"name": "In Progress"}},
        {"id": "31", "to": {"name": "Done"}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": transitions})
        if req.method == "GET":
            return httpx.Response(200, json={"fields": {"status": {"name": current}}})
        posted.append(req.content)
        return httpx.Response(204)

    return handler


def test_transition_moves_to_matching_status():
    posted = []
    assert _jira_with(_workflow("To Do", posted)).transition_to("CA-1", "in progress") == "moved"
    assert b'"id":"21"' in posted[0].replace(b" ", b"")


def test_transition_already_there_or_unavailable():
    posted = []
    jira = _jira_with(_workflow("In Progress", posted))
    assert jira.transition_to("CA-1", "In Progress") == "already"
    assert jira.transition_to("CA-1", "In Review") == "unavailable"
    assert posted == []


def test_accepted_webhook_adds_queue_label(client):  # noqa: F811
    _post(client, _issue("CA-4"))
    assert client.queue_labels == [("CA-4", True)]


def test_ignored_webhook_does_not_touch_labels(client):  # noqa: F811
    _post(client, _issue("CA-4", labels=["other"]))
    assert client.queue_labels == []


def test_job_end_removes_queue_label(client, monkeypatch):  # noqa: F811
    from app import main

    class FakeAgent:
        def __init__(self, settings, job):
            self.job = job

        def handle_issue(self, key):
            self.job.finish("succeeded")

    monkeypatch.setattr(main, "CodingAgent", FakeAgent)
    job = Job("CA-5", "jira:issue_created")
    main._in_flight.add("CA-5")
    main._run_job(job)
    assert client.queue_labels[-1] == ("CA-5", False)
    assert "CA-5" not in main._in_flight


def test_recover_queued_requeues_labelled_tickets(client, monkeypatch):  # noqa: F811
    from app import main

    searched = []

    class FakeJira:
        def search_keys(self, jql):
            searched.append(jql)
            return ["CA-6", "CA-7"]

    monkeypatch.setattr(main, "_jira", lambda: FakeJira())
    main._in_flight.add("CA-7")  # already running here -> not queued twice
    assert main.recover_queued() == ["CA-6"]
    assert client.submitted == ["CA-6"]
    assert '"ai-agent-queued"' in searched[0]


def test_agent_move_never_raises(monkeypatch):
    from app.agent import CodingAgent

    agent = CodingAgent.__new__(CodingAgent)
    agent.job = Job("CA-8", "e")

    class BrokenJira:
        def transition_to(self, key, status):
            raise httpx.ConnectError("down")

    agent.jira = BrokenJira()
    agent._move("CA-8", "In Progress")  # logs a warning, doesn't raise
    assert agent.job.jira_status is None

    class OkJira:
        def transition_to(self, key, status):
            return "moved"

    agent.jira = OkJira()
    agent._move("CA-8", "In Progress")
    assert agent.job.jira_status == "In Progress"
