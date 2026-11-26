"""A minimal GitHub REST client (standard library only) for the workflows.

The token comes from ``GH_TOKEN`` and the repository from ``GITHUB_REPOSITORY``
(set by the runner, not by a pull request). Every path is built here from fixed
templates and values the caller has already validated; each value is still
URL-quoted.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.github.com"
_REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto a redirected request; a
    redirect away from api.github.com would hand the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise urllib.error.HTTPError(newurl, code, "refusing a cross-host redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message[:300]}")
        self.status = status


def repository() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not _REPO.fullmatch(repo):
        raise SystemExit("GITHUB_REPOSITORY is not set to owner/name")
    return repo


def q(value: str) -> str:
    """URL-quote one path segment (or a path, keeping its slashes)."""
    return urllib.parse.quote(value, safe="/")


class Client:
    def __init__(self, token: str | None = None, repo: str | None = None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self.repo = repo or repository()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> Any:
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "turbo-ea-translations-ci",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with _OPENER.open(req, timeout=60) as resp:
                    payload = resp.read()
                    if raw:
                        return payload
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                # Retry only what is plausibly transient, and never a write
                # that may have been applied.
                if exc.code in (502, 503, 504) and method == "GET" and attempt < 2:
                    last = ApiError(exc.code, text)
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ApiError(exc.code, text) from None
            except urllib.error.URLError as exc:
                if method == "GET" and attempt < 2:
                    last = exc
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        raise last or RuntimeError("request failed")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, limit: int = 1000, **params: Any) -> list[Any]:
        return self.paginate_complete(path, limit, **params)[0]

    def paginate_complete(self, path: str, limit: int = 1000, **params: Any) -> tuple[list[Any], bool]:
        """``(items, complete)``: at most ``limit`` items, and whether that was
        everything (a caller that must see all items fails closed on False)."""
        items: list[Any] = []
        page = 1
        while True:
            batch = self.get(path, per_page=100, page=page, **params)
            if not isinstance(batch, list) or not batch:
                return items, True
            items.extend(batch)
            if len(batch) < 100:
                return items[:limit], len(items) <= limit
            if len(items) >= limit:
                return items[:limit], False
            page += 1

    # -- repository helpers -------------------------------------------------

    def r(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"

    def default_branch(self) -> str:
        branch = self.get(self.r(""))["default_branch"]
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch or ""):
            raise RuntimeError("unexpected default branch name")
        return branch

    def branch_sha(self, branch: str) -> str:
        return self.get(self.r(f"/branches/{q(branch)}"))["commit"]["sha"]

    def commit_parents(self, commit_sha: str) -> list[str]:
        commit = self.get(self.r(f"/git/commits/{q(commit_sha)}"))
        return [p.get("sha") for p in commit.get("parents") or []]

    def tree_listing(self, commit_sha: str, path: str = "") -> list[dict[str, Any]] | None:
        """The entries of the directory ``path`` ('' = root) in ``commit_sha``,
        or ``None`` when there is no such directory."""
        if path:
            entry = self.tree_entry(commit_sha, path)
            if entry is None or entry.get("type") != "tree":
                return None
            tree_sha = entry["sha"]
        else:
            tree_sha = self.get(self.r(f"/git/commits/{q(commit_sha)}"))["tree"]["sha"]
        tree = self.get(self.r(f"/git/trees/{q(tree_sha)}"))
        if tree.get("truncated"):
            raise RuntimeError("tree listing truncated")
        return list(tree.get("tree", []))

    def tree_entry(self, commit_sha: str, path: str) -> dict[str, Any] | None:
        """The git tree entry (``mode``, ``type``, ``sha``, ``size``) at ``path``
        in ``commit_sha``, or ``None``. Walks one directory level at a time, so
        no path text is ever interpreted by the API."""
        commit = self.get(self.r(f"/git/commits/{q(commit_sha)}"))
        tree_sha = commit["tree"]["sha"]
        parts = path.split("/")
        entry: dict[str, Any] | None = None
        for depth, part in enumerate(parts):
            tree = self.get(self.r(f"/git/trees/{q(tree_sha)}"))
            if tree.get("truncated"):
                raise RuntimeError("tree listing truncated")
            entry = next((e for e in tree.get("tree", []) if e.get("path") == part), None)
            if entry is None:
                return None
            if depth < len(parts) - 1:
                if entry.get("type") != "tree":
                    return None
                tree_sha = entry["sha"]
        return entry

    def blob(self, sha: str) -> bytes:
        data = self.get(self.r(f"/git/blobs/{q(sha)}"))
        if data.get("encoding") != "base64":
            raise RuntimeError("unexpected blob encoding")
        return base64.b64decode(data["content"])

    def file_text(self, ref: str, path: str) -> str | None:
        """A regular file's text on ``ref`` (a commit sha), or ``None``."""
        entry = self.tree_entry(ref, path)
        if entry is None or entry.get("type") != "blob" or entry.get("mode") != "100644":
            return None
        if int(entry.get("size") or 0) > 64 * 1024:
            return None
        return self.blob(entry["sha"]).decode("utf-8", "replace")

    def put_file(self, path: str, content: bytes, message: str, branch: str, sha: str | None) -> Any:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self.request("PUT", self.r(f"/contents/{q(path)}"), body=body)
