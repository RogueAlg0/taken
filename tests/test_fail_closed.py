"""Fail-closed tests: any check that cannot do its job must error (exit 3),
never degrade quietly into a GO verdict."""

import pytest

from taken import checks
from taken.cli import main


def boom(endpoint, params=None):
    raise checks.TakenError("simulated `gh` failure")


def test_api_failure_is_exit_3_not_go(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", boom)
    assert main(["octo/repo#1"]) == 3
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "simulated `gh` failure" in err


def test_null_timeline_response_raises(monkeypatch):
    def fake(endpoint, params=None):
        return None  # API returned JSON null

    monkeypatch.setattr(checks, "gh_api", fake)
    with pytest.raises(checks.TakenError):
        checks.check_timeline("octo", "repo", 1)


def test_null_timeline_is_exit_3(monkeypatch):
    def fake(endpoint, params=None):
        if endpoint.endswith("/timeline"):
            return None
        return {
            "state": "open",
            "title": "x",
            "labels": [],
            "assignees": [],
            "comments": 0,
            "user": {"login": "someone"},
            "html_url": "https://github.com/octo/repo/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["octo/repo#1"]) == 3


def test_non_dict_issue_response_raises(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", lambda endpoint, params=None: ["not", "a", "dict"])
    with pytest.raises(checks.TakenError):
        checks.check_issue("octo", "repo", 1)


@pytest.mark.parametrize("field", ["labels", "assignees"])
def test_null_issue_collection_is_empty(monkeypatch, field):
    response = {
        "state": "open",
        "title": "x",
        "labels": [{"name": "good first issue"}],
        "assignees": [{"login": "alice"}],
        "comments": 0,
        "user": {"login": "someone"},
        "html_url": "https://github.com/octo/repo/issues/1",
        "created_at": "2026-10-20T00:00:00Z",
    }
    response[field] = None
    calls = []

    def fake(endpoint, params=None):
        calls.append(endpoint)
        return response

    monkeypatch.setattr(checks, "gh_api", fake)
    facts = checks.check_issue("octo", "repo", 1)

    assert calls == ["repos/octo/repo/issues/1"]
    assert facts["labels"] == ([] if field == "labels" else ["good first issue"])
    assert facts["assignees"] == ([] if field == "assignees" else ["alice"])


@pytest.mark.parametrize("field", ["labels", "assignees"])
def test_null_issue_collection_in_search_payload_falls_back_to_get(monkeypatch, field):
    response = {
        "state": "open",
        "title": "x",
        "labels": [{"name": "good first issue"}],
        "assignees": [{"login": "alice"}],
        "comments": 0,
        "user": {"login": "someone"},
        "html_url": "https://github.com/octo/repo/issues/1",
        "created_at": "2026-10-20T00:00:00Z",
    }
    response[field] = None
    calls = []

    def fake(endpoint, params=None):
        calls.append(endpoint)
        return response

    monkeypatch.setattr(checks, "gh_api", fake)
    facts = checks.check_issue("octo", "repo", 1, payload=response)

    assert calls == ["repos/octo/repo/issues/1"]
    assert facts["labels"] == ([] if field == "labels" else ["good first issue"])
    assert facts["assignees"] == ([] if field == "assignees" else ["alice"])


def test_non_dict_contributing_response_raises(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", lambda endpoint, params=None: ["a", "directory"])
    with pytest.raises(checks.TakenError):
        checks.check_ai_policy("octo", "repo")


def test_undecodable_contributing_raises(monkeypatch):
    def fake(endpoint, params=None):
        if "contents" in endpoint:
            return {"content": "!!! not base64 !!!", "encoding": "base64"}
        raise checks.NotFoundError(endpoint)

    monkeypatch.setattr(checks, "gh_api", fake)
    with pytest.raises(checks.TakenError, match="could not decode"):
        checks.check_ai_policy("octo", "repo")


def test_non_list_pulls_response_raises(monkeypatch):
    def fake(endpoint, params=None):
        if endpoint.endswith("/pulls"):
            return {"unexpected": "object"}
        return {"pushed_at": "2026-09-25T00:00:00Z", "stargazers_count": 1}

    monkeypatch.setattr(checks, "gh_api", fake)
    with pytest.raises(checks.TakenError):
        checks.check_repo_health("octo", "repo")


def test_non_list_comments_response_raises(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", lambda endpoint, params=None: None)
    with pytest.raises(checks.TakenError):
        checks.check_claimants("octo", "repo", 1)


def test_claimant_scan_rejects_non_list():
    with pytest.raises(checks.TakenError):
        checks.find_claimant_hits(None)


def test_missing_gh_binary_is_exit_3(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no gh here")

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    assert main(["octo/repo#1"]) == 3
