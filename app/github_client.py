"""Minimal GitHub REST client for repo metadata and pull requests."""
import httpx

from app.config import Settings


class GitHubClient:
    def __init__(self, settings: Settings):
        self.repo = settings.github_repo
        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def default_branch(self) -> str:
        r = self._client.get(f"/repos/{self.repo}")
        r.raise_for_status()
        return r.json()["default_branch"]

    def find_open_pr_for_issue(self, issue_key: str) -> str | None:
        """Open agent PR for this ticket, matched by key so a renamed ticket still matches."""
        r = self._client.get(f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100})
        r.raise_for_status()
        for pr in r.json():
            ref = pr["head"]["ref"]
            if ref == f"ai/{issue_key}" or ref.startswith(f"ai/{issue_key}-"):
                return pr["html_url"]
        return None

    def create_pr(self, *, head: str, base: str, title: str, body: str, draft: bool) -> str:
        payload = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        r = self._client.post(f"/repos/{self.repo}/pulls", json=payload)
        # GitHub Free only allows draft PRs on public repos; retry as a normal PR.
        if r.status_code == 422 and draft and "draft" in r.text.lower():
            r = self._client.post(f"/repos/{self.repo}/pulls", json={**payload, "draft": False})
        r.raise_for_status()
        return r.json()["html_url"]
