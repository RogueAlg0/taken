"""Tests for the taken MCP server tools."""

import asyncio
import subprocess
from datetime import datetime, timezone

import pytest

from taken import checks, discover, graphql, mcp_server
from taken.mcp_server import check_issue, discover_candidates, mcp, scan_repo


@pytest.fixture(autouse=True)
def _anonymous_transport_by_default(monkeypatch):
    # Pin the identity probe so transport selection is deterministic in
    # every environment (CI has no `gh` auth; a dev machine might).
    # Authenticated behavior gets its own tests below.
    monkeypatch.setattr(checks, "_github_identity", lambda: None)


def issue_payload(number, kind="go", labels=()):
    return {
        "number": number,
        "state": "closed" if kind == "closed" else "open",
        "title": f"issue {number}",
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
        "comments": 0,
        "user": {"login": "alice"},
        "html_url": f"https://github.com/octo/repo/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_fake(states, labels_map=None):
    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues":
            # The real issues endpoint returns the same full issue objects
            # as the per-issue GET, so the listing mirrors it exactly.
            return [
                issue_payload(n, states.get(n, "go"), (labels_map or {}).get(n, ()))
                for n in sorted(states)
            ]
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            if endpoint.endswith("/comments"):
                return []
            if endpoint.endswith("/timeline"):
                return []
            return issue_payload(
                number, states.get(number, "go"), (labels_map or {}).get(number, ())
            )
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint.startswith("repos/octo/repo/commits"):
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
    scan_properties = schemas["scan_repo"]["properties"]
    assert scan_properties["limit"]["description"] == "Max open issues to check. Default: 20."
    assert scan_properties["limit"]["default"] == 20
    assert scan_properties["label"]["description"] == (
        "Only consider open issues carrying this label. Default: no label filter."
    )
    assert scan_properties["label"]["default"] is None
    assert scan_properties["me"]["description"] == (
        "Your GitHub login; your own comments are ignored. Default: none."
    )
    assert scan_properties["me"]["default"] is None
    discover_properties = schemas["discover_candidates"]["properties"]
    assert discover_properties["limit"]["description"] == ("Max candidates to return. Default: 10.")
    assert discover_properties["limit"]["default"] == 10
    assert discover_properties["language"]["description"] == (
        "Only consider repositories in this language. Default: no language filter."
    )
    assert discover_properties["language"]["default"] is None
    assert discover_properties["label"]["description"] == (
        "Issue label to search. Default: good first issue, good-first-issue, "
        "beginner friendly, and help wanted."
    )
    assert discover_properties["label"]["default"] is None
    assert discover_properties["min_contributors"]["description"] == (
        "Only consider repositories with at least this many contributors "
        "in the last 90 days. Default: 0."
    )
    assert discover_properties["min_contributors"]["default"] == 0
    assert discover_properties["me"]["description"] == (
        "Your GitHub login; your own comments are ignored. Default: none."
    )
    assert discover_properties["me"]["default"] is None


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
    assert payload == {"target": "octo/repo#1", "error": "network down", "error_code": "unknown"}


def test_check_issue_carries_friendly_and_welcoming(monkeypatch):
    monkeypatch.setattr(
        checks, "gh_api", make_fake({1: "go"}, labels_map={1: ["good first issue", "bug"]})
    )
    payload = check_issue("octo", "repo", 1)
    assert payload["friendly_labels"] == ["good first issue"]
    assert payload["welcoming"] == []  # no CONTRIBUTING.md in this fake


def test_scan_repo_reports_each_issue(faked):
    payload = scan_repo("octo", "repo", limit=10)
    assert payload["target"] == "octo/repo"
    assert payload["effective_parameters"] == {"limit": 10, "label": None, "me": None}
    by_target = {r["target"]: r["verdict"] for r in payload["results"]}
    assert by_target == {
        "octo/repo#1": "GO",
        "octo/repo#2": "TAKEN",
        "octo/repo#3": "TAKEN",
    }


def test_scan_repo_echoes_effective_default_parameters(faked):
    payload = scan_repo("octo", "repo")
    assert payload["effective_parameters"] == {
        "limit": 20,
        "label": None,
        "me": None,
    }


def test_scan_repo_recommends_go_first(faked):
    payload = scan_repo("octo", "repo", limit=10)
    assert [r["target"] for r in payload["results"]] == [
        "octo/repo#1",
        "octo/repo#2",
        "octo/repo#3",
    ]
    assert payload["recommendations"] == ["octo/repo#1"]
    assert payload["summary"] == {"GO": 1, "CAUTION": 0, "TAKEN": 2, "errors": 0}


def test_scan_repo_carries_friendly_and_welcoming(monkeypatch):
    monkeypatch.setattr(
        checks, "gh_api", make_fake({1: "go"}, labels_map={1: ["good first issue", "bug"]})
    )
    payload = scan_repo("octo", "repo", limit=10)
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["friendly_labels"] == ["good first issue"]
    assert result["welcoming"] == []  # no CONTRIBUTING.md in this fake


def test_scan_repo_error_dict(monkeypatch):
    def boom(endpoint, params=None):
        raise checks.TakenError("repo gone")

    monkeypatch.setattr(checks, "gh_api", boom)
    payload = scan_repo("octo", "repo", limit=7, label="help wanted", me="octocat")
    assert payload == {
        "target": "octo/repo",
        "effective_parameters": {"limit": 7, "label": "help wanted", "me": "octocat"},
        "error": "repo gone",
        "error_code": "unknown",
    }


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
    assert payload["effective_parameters"] == {
        "limit": 5,
        "language": None,
        "labels": ["good first issue"],
        "min_contributors": 0,
        "me": None,
    }
    assert [r["target"] for r in payload["results"]] == ["octo/repo#1"]
    assert payload["results"][0]["verdict"] == "GO"
    assert payload["results"][0]["score"] >= 0


def test_discover_candidates_echoes_effective_default_parameters(monkeypatch):
    monkeypatch.setattr(discover, "discover", lambda **kwargs: [])
    payload = discover_candidates()
    assert payload["effective_parameters"] == {
        "limit": 10,
        "language": None,
        "labels": [
            "good first issue",
            "good-first-issue",
            "beginner friendly",
            "help wanted",
        ],
        "min_contributors": 0,
        "me": None,
    }


def test_discover_candidates_error_echoes_effective_parameters(monkeypatch):
    def boom(**kwargs):
        raise checks.TakenError("search unavailable")

    monkeypatch.setattr(discover, "discover", boom)
    payload = discover_candidates(
        limit=3,
        language="Python",
        label="help wanted",
        min_contributors=50,
        me="octocat",
    )
    assert payload == {
        "effective_parameters": {
            "limit": 3,
            "language": "Python",
            "labels": ["help wanted"],
            "min_contributors": 50,
            "me": "octocat",
        },
        "error": "search unavailable",
        "error_code": "unknown",
    }


def test_discover_candidates_carries_friendly_and_welcoming(monkeypatch):
    items = [search_item(1)]
    base = make_fake({1: "go"}, labels_map={1: ["good first issue"]})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 1, "incomplete_results": False, "items": items}
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    payload = discover_candidates(limit=5, label="good first issue")
    result = payload["results"][0]
    assert result["friendly_labels"] == ["good first issue"]
    assert result["welcoming"] == []


def _block_mcp_import(monkeypatch):
    """Make any `import mcp...` raise ImportError, simulating a plain install."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.partition(".")[0] == "mcp":
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_create_server_raises_without_mcp(monkeypatch):
    import taken.mcp_server as ms

    _block_mcp_import(monkeypatch)
    with pytest.raises(ImportError):
        ms._create_server()


def test_main_without_mcp_prints_guidance(monkeypatch, capsys):
    import taken.mcp_server as ms

    monkeypatch.setattr(ms, "mcp", None)
    assert ms.main() == 2
    err = capsys.readouterr().err
    assert "taken-gh[mcp]" in err


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (checks.RateLimitError("rate limit reached"), "rate_limited"),
        (checks.NotFoundError("repository missing"), "not_found"),
        (checks.TakenError("Bad credentials (HTTP 401)"), "auth_failed"),
        (subprocess.TimeoutExpired("gh api", 60), "timeout"),
        (checks.TakenError("`gh api repos/octo/repo` timed out after 60s"), "timeout"),
        (checks.TakenError("network down"), "unknown"),
    ],
)
@pytest.mark.parametrize("tool", ["check_issue", "scan_repo", "discover_candidates"])
def test_tools_return_machine_readable_error_codes(monkeypatch, error, code, tool):
    def boom(*args, **kwargs):
        raise error

    if tool == "discover_candidates":
        monkeypatch.setattr(discover, "discover", boom)
        payload = discover_candidates()
    else:
        monkeypatch.setattr(checks, "gh_api", boom)
        payload = (
            check_issue("octo", "repo", 1) if tool == "check_issue" else scan_repo("octo", "repo")
        )
    assert payload["error_code"] == code
    assert payload["error"] == str(error)
def _stub_tool_output(monkeypatch):
    """Replace decide/labels helpers so canned findings flow through."""
    monkeypatch.setattr(mcp_server, "decide", lambda findings: ("GO", []))
    monkeypatch.setattr(checks, "friendly_labels", lambda findings: [])
    monkeypatch.setattr(checks, "welcoming_signals", lambda findings: [])


def test_check_issue_uses_graphql_when_authenticated(monkeypatch):
    seen = {}
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")

    def fake(owner, repo, number, me=None, mode="rest", session=None, payload=None):
        seen["mode"] = mode
        return {"transport": mode}

    monkeypatch.setattr(graphql, "run_checks_with_fallback", fake)
    _stub_tool_output(monkeypatch)
    payload = check_issue("octo", "repo", 1)
    assert seen["mode"] == "graphql"
    assert payload["verdict"] == "GO"
    assert payload["findings"]["transport"] == "graphql"


def test_check_issue_stays_rest_when_anonymous(monkeypatch):
    seen = {}

    def fake(owner, repo, number, me=None, mode="rest", session=None, payload=None):
        seen["mode"] = mode
        return {"transport": mode}

    monkeypatch.setattr(graphql, "run_checks_with_fallback", fake)
    _stub_tool_output(monkeypatch)
    payload = check_issue("octo", "repo", 1)
    assert seen["mode"] == "rest"
    assert payload["findings"]["transport"] == "rest"


def test_check_issue_explicit_flags_still_win(monkeypatch):
    seen = {}
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")

    def fake(owner, repo, number, me=None, mode="rest", session=None, payload=None):
        seen["mode"] = mode
        return {"transport": mode}

    monkeypatch.setattr(graphql, "run_checks_with_fallback", fake)
    _stub_tool_output(monkeypatch)
    check_issue("octo", "repo", 1, graphql=True)
    assert seen["mode"] == "graphql"
    check_issue("octo", "repo", 1, persistent_session=True)
    assert seen["mode"] == "persistent"
    monkeypatch.setenv("TAKEN_REST", "1")
    check_issue("octo", "repo", 1)
    assert seen["mode"] == "rest"


def test_check_issue_graphql_fallback_is_surfaced(monkeypatch, faked):
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")

    def boom(owner, repo, number, me=None, mode="graphql", session=None):
        raise checks.TakenError("transport down")

    monkeypatch.setattr(graphql, "run_checks_graphql", boom)
    payload = check_issue("octo", "repo", 1)
    assert payload["findings"]["transport"] == "rest"
    assert "transport down" in payload["findings"]["transport_fallback"]


def test_scan_repo_resolves_mode_automatically(monkeypatch, faked):
    seen = []
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")

    def fake(owner, repo, number, me=None, mode="rest", session=None, payload=None):
        seen.append(mode)
        return {"transport": mode}

    monkeypatch.setattr(graphql, "run_checks_with_fallback", fake)
    _stub_tool_output(monkeypatch)
    payload = scan_repo("octo", "repo", limit=3)
    assert seen == ["graphql", "graphql", "graphql"]
    assert payload["summary"] == {"GO": 3, "CAUTION": 0, "TAKEN": 0, "errors": 0}


def test_discover_candidates_uses_automatic_mode(monkeypatch):
    seen = {}

    class FakeResults(list):
        search_errors = []

    def fake_discover(**kwargs):
        seen.update(kwargs)
        return FakeResults()

    monkeypatch.setattr(discover, "discover", fake_discover)
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")
    out = discover_candidates(limit=3)
    assert seen["mode"] == "graphql"
    assert out["results"] == []
    assert out["search_errors"] == []
    # Anonymous stays on REST.
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    discover_candidates(limit=3)
    assert seen["mode"] == "rest"


def test_mcp_and_cli_verdict_parity_on_graphql_path(monkeypatch, faked):
    from taken import cli

    def fake_gql(owner, repo, number, me=None, mode="graphql", session=None):
        findings = checks.run_checks(owner, repo, number, me=me)
        findings["transport"] = "graphql"
        return findings

    monkeypatch.setattr(graphql, "run_checks_graphql", fake_gql)
    mcp_payload = mcp_server._check_one("octo", "repo", 1, mode="graphql")
    _, cli_verdict, cli_reasons, _ = cli.check_one("octo", "repo", 1, None, mode="graphql")
    assert mcp_payload["verdict"] == cli_verdict
    assert mcp_payload["reasons"] == cli_reasons
