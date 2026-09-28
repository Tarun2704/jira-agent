"""Minimal Jira Cloud REST client (API v2 returns descriptions as plain text)."""
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    labels: list[str]
    issue_type: str
    status: str = ""


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
            params={"fields": "summary,description,labels,issuetype,status"},
        )
        r.raise_for_status()
        f = r.json()["fields"]
        return JiraIssue(
            key=key,
            summary=f.get("summary") or "",
            description=f.get("description") or "",
            labels=f.get("labels") or [],
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            status=(f.get("status") or {}).get("name", ""),
        )

    def add_comment(self, key: str, body: str) -> None:
        r = self._client.post(f"/rest/api/2/issue/{key}/comment", json={"body": body})
        r.raise_for_status()

    def _update_labels(self, key: str, op: str, label: str) -> None:
        r = self._client.put(f"/rest/api/2/issue/{key}", json={"update": {"labels": [{op: label}]}})
        r.raise_for_status()

    def add_label(self, key: str, label: str) -> None:
        self._update_labels(key, "add", label)

    def remove_label(self, key: str, label: str) -> None:
        self._update_labels(key, "remove", label)

    def search_keys(self, jql: str, limit: int = 50) -> list[str]:
        r = self._client.get(
            "/rest/api/3/search/jql", params={"jql": jql, "fields": "key", "maxResults": limit}
        )
        r.raise_for_status()
        return [i["key"] for i in r.json().get("issues", [])]

    def transition_to(self, key: str, status: str) -> str:
        """Move the ticket to the status named `status` (case-insensitive).

        Returns "moved", "already" (it was already there), or "unavailable" (the workflow
        has no transition to that status from where the ticket is now).
        """
        current = self._client.get(f"/rest/api/2/issue/{key}", params={"fields": "status"})
        current.raise_for_status()
        if current.json()["fields"]["status"]["name"].lower() == status.lower():
            return "already"

        r = self._client.get(f"/rest/api/2/issue/{key}/transitions")
        r.raise_for_status()
        transitions = r.json().get("transitions", [])
        match = next((t for t in transitions if t["to"]["name"].lower() == status.lower()), None)
        if not match:
            available = sorted({t["to"]["name"] for t in transitions})
            log.warning("Jira status %r not available for %s (available: %s)", status, key, available)
            return "unavailable"
        r = self._client.post(f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
        r.raise_for_status()
        return "moved"
