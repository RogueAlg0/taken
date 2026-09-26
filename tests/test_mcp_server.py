"""Tests for the taken MCP server tools."""

import asyncio
from datetime import datetime, timezone

import pytest

from taken import checks
from taken.mcp_server import check_issue, discover_candidates, mcp, scan_repo


def issue_payload(number, kind="go"):
    return {
        "number": number,
        "state": "closed" if kind == "closed" else "open",
        "title": f"issue {number}",
        "labels": [],
        "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
        "comments": 0,
        "user": {"login": "alice"},
        "html_url": f"https://github.com/octo/repo/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_fake(states):
    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues":
            return [issue_payload(n) for n in sorted(states)]
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            if endpoint.endswith("/comments"):
                return []
            if endpoint.endswith("/timeline"):
                return []
            return issue_payload(number, states.get(number, "go"))
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint == "repos/octo/repo":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"pushed_at": now, "stargazers_count": 4}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fake


@pytest.fixture
def faked(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "go", 2: "taken", 3: "closed"}))


def test_server_registers_three_tools():
    async def go():
        return await mcp.list_tools()

    tools = asyncio.run(go())
    assert sorted(t.name for t in tools) == [
        "check_issue",
        "discover_candidates",
        "scan_repo",
    ]
    schemas = {t.name: t.input_schema for t in tools}
    assert schemas["check_issue"]["required"] == ["owner", "repo", "issue_number"]
    assert schemas["check_issue"]["properties"]["issue_number"]["type"] == "integer"


def test_check_issue_go(faked):
    payload = check_issue("octo", "repo", 1)
    assert payload["target"] == "octo/repo#1"
    assert payload["verdict"] == "GO"
    assert payload["reasons"]
    assert payload["findings"]["issue"]["title"] == "issue 1"


def test_check_issue_taken_when_assigned(faked):
    payload = check_issue("octo", "repo", 2)
    assert payload["verdict"] == "TAKEN"
    assert any("dk5488" in reason for reason in payload["reasons"])


def test_check_issue_taken_when_closed(faked):
    payload = check_issue("octo", "repo", 3)
    assert payload["verdict"] == "TAKEN"


def test_check_issue_returns_error_dict(monkeypatch):
    def boom(endpoint, params=None):
        raise checks.TakenError("network down")

    monkeypatch.setattr(checks, "gh_api", boom)
    payload = check_issue("octo", "repo", 1)
    assert payload == {"target": "octo/repo#1", "error": "network down"}


def test_scan_repo_reports_each_issue(faked):
    payload = scan_repo("octo", "repo", limit=10)
    assert payload["target"] == "octo/repo"
    by_target = {r["target"]: r["verdict"] for r in payload["results"]}
    assert by_target == {
        "octo/repo#1": "GO",
        "octo/repo#2": "TAKEN",
        "octo/repo#3": "TAKEN",
    }


def test_scan_repo_error_dict(monkeypatch):
    def boom(endpoint, params=None):
        raise checks.TakenError("repo gone")

    monkeypatch.setattr(checks, "gh_api", boom)
    payload = scan_repo("octo", "repo")
    assert payload == {"target": "octo/repo", "error": "repo gone"}


def search_item(number):
    return {
        "number": number,
        "title": f"issue {number}",
        "user": {"login": "alice"},
        "repository_url": "https://api.github.com/repos/octo/repo",
        "updated_at": "2026-09-25T00:00:00Z",
        "html_url": f"https://github.com/octo/repo/issues/{number}",
    }


def test_discover_candidates_verifies_and_ranks(monkeypatch):
    items = [search_item(1), search_item(2)]
    base = make_fake({1: "go", 2: "taken"})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 2, "incomplete_results": False, "items": items}
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    payload = discover_candidates(limit=5, label="good first issue")
    assert [r["target"] for r in payload["results"]] == ["octo/repo#1"]
    assert payload["results"][0]["verdict"] == "GO"
    assert payload["results"][0]["score"] >= 0
