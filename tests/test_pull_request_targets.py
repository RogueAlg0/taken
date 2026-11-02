"""A PR number must never be scored as an available issue (issue #203)."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from taken import checks, cli, mcp_server


@pytest.fixture
def issue():
    return {
        "state": "open",
        "title": "A contribution opportunity",
        "labels": [],
        "assignees": [],
        "comments": 0,
        "user": {"login": "alice"},
        "html_url": "https://github.com/octo/repo/issues/123",
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def pr_api(monkeypatch, issue):
    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues/123":
            return {**issue, "pull_request": {}}
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint == "repos/octo/repo":
            return {"pushed_at": datetime.now(timezone.utc).isoformat(), "stargazers_count": 4}
        if endpoint.endswith(("/timeline", "/comments", "/pulls", "/commits")):
            return []
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    api = Mock(side_effect=fake)
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    monkeypatch.setattr(checks, "gh_api", api)
    return api


@pytest.mark.parametrize(
    "marker", [{"url": "https://api.github.com/repos/octo/repo/pulls/123"}, {}, None]
)
@pytest.mark.parametrize("source", ["fetched", "complete_payload", "incomplete_payload"])
def test_rejects_pr_targets(monkeypatch, issue, marker, source):
    pr = {**issue, "pull_request": marker}
    api = Mock(return_value=pr)
    monkeypatch.setattr(checks, "gh_api", api)
    payload = None if source == "fetched" else pr
    if source == "incomplete_payload":
        payload = {"pull_request": marker}

    with pytest.raises(checks.TakenError, match="octo/repo#123 is a pull request, not an issue"):
        checks.check_issue("octo", "repo", 123, payload=payload)

    if source == "fetched":
        api.assert_called_once_with("repos/octo/repo/issues/123")
    else:
        api.assert_not_called()


def test_closed_pr_is_an_error_rather_than_taken(monkeypatch, issue):
    monkeypatch.setattr(
        checks, "gh_api", Mock(return_value={**issue, "state": "closed", "pull_request": {}})
    )
    with pytest.raises(checks.TakenError, match="pull request, not an issue"):
        checks.check_issue("octo", "repo", 123)


@pytest.mark.parametrize("source", ["fetched", "payload"])
def test_real_issue_still_returns_facts(monkeypatch, issue, source):
    api = Mock(return_value=issue)
    monkeypatch.setattr(checks, "gh_api", api)
    facts = checks.check_issue("octo", "repo", 123, payload=issue if source == "payload" else None)
    assert facts == {
        "number": 123,
        "state": "open",
        "title": issue["title"],
        "labels": [],
        "assignees": [],
        "comment_count": 0,
        "author": "alice",
        "url": issue["html_url"],
        "created_at": issue["created_at"],
    }
    assert api.call_count == (source == "fetched")


@pytest.mark.parametrize("target", ["octo/repo#123", "https://github.com/octo/repo/issues/123"])
def test_cli_pr_target_exits_3_without_a_verdict(pr_api, target, capsys):
    assert cli.main(["--rest", target]) == 3
    output = capsys.readouterr()
    assert "pull request, not an issue" in output.err
    assert "verdict:" not in output.out
    pr_api.assert_called_once_with("repos/octo/repo/issues/123")


def test_mcp_pr_target_returns_error_without_a_verdict(pr_api):
    result = mcp_server.check_issue("octo", "repo", 123)
    assert "pull request, not an issue" in result["error"]
    assert "verdict" not in result
    pr_api.assert_called_once_with("repos/octo/repo/issues/123")
