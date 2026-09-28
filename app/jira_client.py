"""Minimal Jira Cloud REST client (API v2 returns descriptions as plain text)."""
from dataclasses import dataclass

import httpx

from app.config import Settings


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    labels: list[str]
    issue_type: str


class JiraClient:
    def __init__(self, settings: Settings):
        self._client = httpx.Client(
            base_url=settings.jira_base_url.rstrip("/"),
            auth=(settings.jira_email, settings.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=30,
        )

    def get_issue(self, key: str) -> JiraIssue:
        r = self._client.get(
            f"/rest/api/2/issue/{key}",
            params={"fields": "summary,description,labels,issuetype"},
        )
        r.raise_for_status()
        f = r.json()["fields"]
        return JiraIssue(
            key=key,
            summary=f.get("summary") or "",
            description=f.get("description") or "",
            labels=f.get("labels") or [],
            issue_type=(f.get("issuetype") or {}).get("name", ""),
        )

    def add_comment(self, key: str, body: str) -> None:
        r = self._client.post(f"/rest/api/2/issue/{key}/comment", json={"body": body})
        r.raise_for_status()
