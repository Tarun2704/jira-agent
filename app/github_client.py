"""Minimal GitHub REST client for repo metadata and pull requests."""
import re

import httpx

from app.config import Settings
from app.retry import RetryingClient


class GitHubClient:
    def __init__(self, settings: Settings):
        self.repo = settings.github_repo
        self._client = RetryingClient(
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

    # ---- follow-ups: what happened to a PR since we opened it ---------------------------------

    @staticmethod
    def pr_number(url: str) -> int:
        m = re.search(r"/pull/(\d+)", url or "")
        return int(m.group(1)) if m else 0

    def get_pr(self, number: int) -> dict:
        """{number, url, state: open|closed|merged, draft, branch}."""
        r = self._client.get(f"/repos/{self.repo}/pulls/{number}")
        r.raise_for_status()
        j = r.json()
        state = "merged" if j.get("merged") else j["state"]
        return {"number": number, "url": j["html_url"], "state": state, "draft": bool(j.get("draft")), "branch": j["head"]["ref"]}

    def review_comments(self, number: int) -> list[dict]:
        """Human comments on a PR: inline comments, review summaries and conversation comments, oldest first.

        Each: {id, author, path, line, hunk, body, at}. Comments the agent wrote itself are left out
        (they start with AGENT_MARK), as are bots.
        """
        out: list[dict] = []

        def keep(c: dict) -> bool:
            body = (c.get("body") or "").strip()
            return bool(body) and AGENT_MARK not in body and (c.get("user") or {}).get("type") != "Bot"

        for kind, path in (("inline", "pulls"), ("review", "pulls"), ("talk", "issues")):
            url = {
                "inline": f"/repos/{self.repo}/pulls/{number}/comments",
                "review": f"/repos/{self.repo}/pulls/{number}/reviews",
                "talk": f"/repos/{self.repo}/issues/{number}/comments",
            }[kind]
            r = self._client.get(url, params={"per_page": 100})
            r.raise_for_status()
            for c in r.json():
                if not keep(c):
                    continue
                out.append({
                    "id": f"{kind}:{c['id']}",
                    "author": (c.get("user") or {}).get("login", "someone"),
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line") or 0,
                    "hunk": (c.get("diff_hunk") or "")[-600:],
                    "body": c["body"].strip(),
                    "at": c.get("created_at") or c.get("submitted_at") or "",
                })
        return sorted(out, key=lambda c: c["at"])

    def add_pr_comment(self, number: int, body: str) -> None:
        r = self._client.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": f"{body}\n\n{AGENT_MARK}"})
        r.raise_for_status()


AGENT_MARK = "<!-- jira-agent -->"
