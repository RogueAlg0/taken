"""Limited candidate sets must not masquerade as exhaustive results (#29)."""

import json
from datetime import datetime, timezone

import pytest

from taken import checks, cli, discover, mcp_server


def entry(number):
    return dict(
        target=f"o/r#{number}",
        score=1000,
        why=[],
        verdict="GO",
        reasons=[],
        findings={},
        updated_at=datetime.now(timezone.utc).isoformat(),
        friendly_labels=[],
        welcoming=[],
    )


@pytest.fixture
def scan(monkeypatch):
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda *a, **kw: "rest")
    monkeypatch.setattr(mcp_server, "_check_one", lambda o, r, n, **kw: entry(n))
    monkeypatch.setattr(cli, "check_one", lambda o, r, n, *a, **kw: (f"o/r#{n}", "GO", [], {}))

    def setup(count, *, prs=0):
        rows = [{"number": n} for n in range(1, count + 1)]
        rows += [{"number": 1000 + n, "pull_request": {}} for n in range(prs)]
        monkeypatch.setattr(
            checks, "gh_api", lambda endpoint, params=None: rows if params["page"] == "1" else []
        )

    return setup


@pytest.mark.parametrize(
    "count,limit,truncated,considered,returned",
    [
        (3, 2, True, 3, 2),
        (2, 5, False, 2, 2),
        (2, 2, False, 2, 2),
        (100, 100, True, 100, 100),
        (0, 5, False, 0, 0),
        (2, 0, True, 0, 0),
    ],
)
def test_listing_retains_extent(scan, count, limit, truncated, considered, returned):
    scan(count)
    rows = checks.list_open_issues("o", "r", limit=limit)
    assert len(rows) == returned
    assert checks.result_metadata(rows) == {
        "truncated": truncated,
        "considered": considered,
        "returned": returned,
    }


def test_listing_does_not_count_prs_as_candidates(scan):
    scan(2, prs=3)
    result = checks.list_open_issues("o", "r", limit=5)
    assert checks.result_metadata(result) == {"truncated": False, "considered": 2, "returned": 2}


@pytest.mark.parametrize("count,limited", [(3, True), (1, False)])
def test_mcp_scan_reports_limits(scan, count, limited):
    scan(count)
    result = mcp_server.scan_repo("o", "r", limit=2)
    assert result["truncated"] is limited
    assert result["considered"] == count
    assert result["returned"] == min(count, 2)


def test_cli_repo_scan_has_metadata_envelope(scan, capsys):
    scan(3)
    assert cli.main(["--json", "--rest", "--limit", "2", "o/r"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["truncated"] is True
    assert result["considered"] == result["returned"] == 2
    assert result["scans"] == [{"target": "o/r", "truncated": True, "considered": 3, "returned": 2}]
    assert len(result["results"]) == 2


def test_cli_explicit_targets_keep_array_format(scan, capsys):
    scan(0)
    assert cli.main(["--json", "--rest", "o/r#1", "o/r#2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["target"] for row in result] == ["o/r#1", "o/r#2"]


@pytest.mark.parametrize(
    "count,limit,partial,expected",
    [
        (2, 5, False, False),
        (2, 1, False, True),
        (1, 1, False, False),
        (2, 0, False, True),
        (1, 5, True, True),
    ],
)
def test_discover_reports_search_and_verification_limits(
    monkeypatch, count, limit, partial, expected
):
    items = [
        dict(
            number=n,
            repository_url="https://api.github.com/repos/o/r",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        for n in range(count)
    ]
    monkeypatch.setattr(
        checks, "search_issues", lambda *a, **kw: checks.SearchResults(items, truncated=partial)
    )
    monkeypatch.setattr(discover, "_verify_candidate", lambda o, r, n, *a, **kw: (entry(n), None))
    result = discover.discover(discover.DiscoverOptions(limit=limit, jobs=2))
    assert result.truncated is expected
    assert result.total == count
    assert len(result) == min(count, limit)


@pytest.mark.parametrize("incomplete,total", [(True, 1), (False, 47)])
def test_search_retains_upstream_partial_signal(monkeypatch, incomplete, total):
    monkeypatch.setattr(
        checks,
        "gh_api",
        lambda *a, **kw: dict(
            items=[{"number": 1}],
            total_count=total,
            incomplete_results=incomplete,
        ),
    )
    assert checks.search_issues("query").truncated is True


@pytest.mark.parametrize("empty", [False, True])
def test_cli_discover_reports_metadata_even_when_empty(monkeypatch, capsys, empty):
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    rows = [] if empty else [entry(1)]
    monkeypatch.setattr(
        discover,
        "discover",
        lambda *a: discover.DiscoverResults(
            rows,
            total=3,
            verified=1,
            truncated=True,
        ),
    )
    assert cli.main(["--discover", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["truncated"] is True
    assert result["considered"] == 3
    assert result["returned"] == len(rows)


def test_mcp_discover_reports_metadata(monkeypatch):
    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")
    monkeypatch.setattr(
        discover,
        "discover",
        lambda *a: discover.DiscoverResults(
            [entry(1)],
            total=3,
            verified=1,
            truncated=True,
        ),
    )
    result = mcp_server.discover_candidates()
    assert result["truncated"] is True
    assert result["considered"] == 3
    assert result["returned"] == 1


@pytest.mark.parametrize("known_complete", [False, True])
def test_discovery_full_page_uses_upstream_completeness(monkeypatch, known_complete):
    items = [
        dict(
            number=n,
            repository_url="https://api.github.com/repos/o/r",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        for n in range(discover.SEARCH_PER_PAGE)
    ]
    rows = checks.SearchResults(items, truncated=False) if known_complete else items
    monkeypatch.setattr(checks, "search_issues", lambda *a, **kw: rows)
    monkeypatch.setattr(discover, "_verify_pool_size", lambda: discover.SEARCH_PER_PAGE)
    monkeypatch.setattr(discover, "_verify_candidate", lambda o, r, n, *a, **kw: (entry(n), None))
    result = discover.discover(discover.DiscoverOptions(limit=100, jobs=2))
    assert result.truncated is (not known_complete)
    assert len(result) == discover.SEARCH_PER_PAGE
