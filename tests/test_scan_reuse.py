"""Scan-mode payload reuse (issue #211): the repo listing already holds every
issue it will check, so the per-issue refetch must be skipped on the REST
path. Verdicts must be identical with and without the reuse.
"""

from datetime import datetime, timezone
from unittest import mock

from taken import checks, graphql
from taken.cli import check_one, main
from taken.mcp_server import scan_repo


def complete_item(number, kind="go", labels=()):
    """A listing item exactly as the real issues endpoint returns it."""
    return {
        "number": number,
        "state": "closed" if kind == "closed" else "open",
        "title": f"issue {number}",
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
        "comments": 0,
        "user": {"login": "someone"},
        "html_url": f"https://github.com/octo/repo/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_counting_fake(calls, states, labels_map=None):
    """Fake gh_api that records every endpoint hit.

    The listing returns complete items, mirroring the real API, so the
    payload path and the plain-GET path see identical issue facts.
    """

    def fake(endpoint, params=None):
        calls.append(endpoint)
        if endpoint == "repos/octo/repo/issues":
            return [
                complete_item(n, states.get(n, "go"), (labels_map or {}).get(n, ()))
                for n in sorted(states)
            ]
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            if endpoint.endswith("/comments"):
                return []
            if endpoint.endswith("/timeline"):
                return []
            return complete_item(
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


def issue_gets(calls):
    """Per-issue GET endpoints (not the listing, timeline, or comments)."""
    return [
        c
        for c in calls
        if c.startswith("repos/octo/repo/issues/")
        and not c.endswith("/timeline")
        and not c.endswith("/comments")
    ]


def test_list_open_issues_returns_items(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "go", 2: "taken"}))
    items = checks.list_open_issues("octo", "repo", limit=10)
    assert [item["number"] for item in items] == [1, 2]
    assert all(isinstance(item, dict) for item in items)
    assert items[1]["assignees"] == [{"login": "dk5488"}]


def test_scan_skips_per_issue_refetch_on_rest(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "taken", 2: "go"}))
    assert main(["--rest", "octo/repo"]) == 0
    assert calls.count("repos/octo/repo/issues") == 1
    assert issue_gets(calls) == []
    out = capsys.readouterr().out
    assert "TAKEN   octo/repo#1" in out
    assert "GO      octo/repo#2" in out


def test_verdict_parity_payload_vs_plain_get(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "taken", 2: "go"}))
    for number, kind in ((1, "taken"), (2, "go")):
        item = complete_item(number, kind)
        target_p, verdict_p, reasons_p, findings_p = check_one(
            "octo", "repo", number, None, mode="rest", payload=item
        )
        target_g, verdict_g, reasons_g, findings_g = check_one(
            "octo", "repo", number, None, mode="rest"
        )
        assert (target_p, verdict_p, reasons_p) == (target_g, verdict_g, reasons_g)
        assert findings_p == findings_g


def test_incomplete_payload_falls_back_to_get(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "go"}))
    # A listing item missing fields check_issue() needs: the GET must run.
    target, verdict, _reasons, _findings = check_one(
        "octo", "repo", 1, None, mode="rest", payload={"number": 1, "title": "issue 1"}
    )
    assert target == "octo/repo#1"
    assert verdict == "GO"
    assert "repos/octo/repo/issues/1" in calls


def test_graphql_path_ignores_payload():
    # The GraphQL path issues one combined query per issue; a REST listing
    # item cannot substitute for it, so the payload is ignored, not crashed on.
    findings = {"issue": {"number": 1}}
    with mock.patch.object(graphql, "run_checks_graphql", return_value=dict(findings)) as rc:
        out = graphql.run_checks_with_fallback(
            "octo", "repo", 1, mode="graphql", payload=complete_item(1)
        )
    assert rc.call_count == 1
    assert out["transport"] == "graphql"


def test_graphql_fallback_to_rest_uses_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "go"}))
    with mock.patch.object(graphql, "run_checks_graphql", side_effect=checks.TakenError("boom")):
        findings = graphql.run_checks_with_fallback(
            "octo", "repo", 1, mode="graphql", payload=complete_item(1)
        )
    assert findings["transport"] == "rest"
    assert "transport_fallback" in findings
    assert issue_gets(calls) == []


def test_mcp_scan_repo_skips_refetch(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, "gh_api", make_counting_fake(calls, {1: "go", 2: "taken"}))
    payload = scan_repo("octo", "repo", limit=10)
    by_target = {r["target"]: r["verdict"] for r in payload["results"]}
    assert by_target == {"octo/repo#1": "GO", "octo/repo#2": "TAKEN"}
    assert calls.count("repos/octo/repo/issues") == 1
    assert issue_gets(calls) == []


def test_gh_api_rejects_unsafe_endpoint():
    from taken import checks

    for bad in ("--help", "-X", "repos/a/b; rm -rf", "repos/a/b\nc", "", " repos/a/b", 123):
        try:
            checks.gh_api(bad)
        except checks.TakenError:
            pass
        else:
            raise AssertionError(f"gh_api accepted unsafe endpoint: {bad!r}")


def test_gh_api_accepts_plain_endpoints():
    from taken import checks

    for good in (
        "repos/o/r/issues/1",
        "search/issues",
        "graphql",
        "repos/o/r/contents/docs/CONTRIBUTING.md",
        "repos/o/r/issues/1/timeline",
    ):
        assert checks._require_safe_endpoint(good) is None
