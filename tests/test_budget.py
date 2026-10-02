"""Tests for the budget-aware engine (issue #210).

Two tiers: anonymous (lean, today's exact behavior) vs authenticated
(richer, deeper). The tier is chosen once per process by
budget.activate(); before activation everything behaves as anonymous.
"""

import subprocess
from datetime import datetime, timezone

import pytest

from taken import budget, checks, discover


@pytest.fixture(autouse=True)
def _reset_budget():
    budget.reset()
    yield
    budget.reset()


def _full_page(n=100):
    return [{"id": i} for i in range(n)]


# --- tier selection -----------------------------------------------------


def test_default_tier_is_anonymous_before_activation():
    assert budget.current_tier() == budget.TIER_ANONYMOUS
    assert not budget.is_authenticated()


def test_activate_with_login_selects_authenticated():
    assert budget.activate(identity="someone") == budget.TIER_AUTHENTICATED
    assert budget.is_authenticated()
    assert budget.current().tier == budget.TIER_AUTHENTICATED


def test_activate_with_none_selects_anonymous():
    assert budget.activate(identity=None) == budget.TIER_ANONYMOUS
    assert not budget.is_authenticated()


def test_activate_probes_gh_identity(monkeypatch):
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")
    assert budget.activate() == budget.TIER_AUTHENTICATED
    budget.reset()
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    assert budget.activate() == budget.TIER_ANONYMOUS


def test_activate_is_sticky_and_probes_once(monkeypatch):
    calls = []

    def probe():
        calls.append(1)
        return "someone"

    monkeypatch.setattr(checks, "_github_identity", probe)
    assert budget.activate() == budget.TIER_AUTHENTICATED
    assert budget.activate(identity=None) == budget.TIER_AUTHENTICATED
    assert len(calls) == 1


# --- caps ---------------------------------------------------------------


def test_anonymous_caps_match_todays_behavior():
    budget.activate(identity=None)
    b = budget.current()
    assert b.hourly_requests == 60
    assert b.scan_pages == 5
    assert b.repo_pulls_pages == 1
    assert b.repo_commits_pages == 1
    assert b.gql_comment_pages == 5
    assert b.gql_timeline_pages == 5
    assert b.gql_history_pages == 3
    assert b.gql_merge_pages == 2
    assert b.gql_label_pages == 3
    assert b.discover_pool == 40


def test_authenticated_caps_spend_deeper():
    budget.activate(identity="someone")
    b = budget.current()
    assert b.hourly_requests == 5000
    assert b.scan_pages == 10
    assert b.repo_pulls_pages == 4
    assert b.repo_commits_pages == 5
    assert b.gql_comment_pages == 10
    assert b.gql_timeline_pages == 10
    assert b.gql_history_pages == 5
    assert b.gql_merge_pages == 4
    assert b.gql_label_pages == 3  # 300 labels already covers everything
    assert b.discover_pool == 80


def test_effective_cap_never_lowers_a_downward_override(monkeypatch):
    # The docs console sets checks.MAX_SCAN_PAGES = 1 for its 60/hr
    # budget; the tier may raise caps but must never lower that override.
    monkeypatch.setattr(checks, "MAX_SCAN_PAGES", 1)
    budget.activate(identity=None)
    assert budget.effective_cap(checks.MAX_SCAN_PAGES, "scan_pages") == 1
    budget.reset()
    budget.activate(identity="someone")
    assert budget.effective_cap(checks.MAX_SCAN_PAGES, "scan_pages") == 10


# --- cap enforcement ----------------------------------------------------


def _counting_pages(monkeypatch, full_pages):
    calls = []

    def fake_gh_api(endpoint, params=None):
        calls.append((endpoint, (params or {}).get("page")))
        if (params or {}).get("page") and int(params["page"]) <= full_pages:
            return _full_page()
        return []

    monkeypatch.setattr(checks, "gh_api", fake_gh_api)
    return calls


def test_paged_list_stops_at_anonymous_cap(monkeypatch):
    calls = _counting_pages(monkeypatch, full_pages=12)
    budget.activate(identity=None)
    items, truncated = checks._paged_list("repos/o/r/issues/1/comments")
    assert len(calls) == 5
    assert len(items) == 500
    assert truncated is True


def test_paged_list_spends_deeper_when_authenticated(monkeypatch):
    calls = _counting_pages(monkeypatch, full_pages=12)
    budget.activate(identity="someone")
    items, truncated = checks._paged_list("repos/o/r/issues/1/comments")
    assert len(calls) == 10
    assert len(items) == 1000
    assert truncated is True


def test_repo_health_pulls_pages_follow_tier(monkeypatch):
    pulls_calls = []

    def fake_gh_api(endpoint, params=None):
        if endpoint == "repos/o/r":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"pushed_at": now}
        if endpoint == "repos/o/r/pulls":
            pulls_calls.append(params.get("page"))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return [{"merged_at": now} for _ in range(50)]
        if endpoint == "repos/o/r/commits":
            return []
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(checks, "gh_api", fake_gh_api)
    budget.activate(identity=None)
    checks.check_repo_health("o", "r")
    assert pulls_calls == ["1"]
    budget.reset()
    pulls_calls.clear()
    budget.activate(identity="someone")
    checks.check_repo_health("o", "r")
    assert pulls_calls == ["1", "2", "3", "4"]


def test_discover_pool_grows_when_authenticated(monkeypatch):
    items = [
        {"number": i, "repository_url": "https://api.github.com/repos/o/r"} for i in range(100)
    ]
    monkeypatch.setattr(checks, "search_issues", lambda *a, **k: items)
    budget.activate(identity=None)
    candidates, _, _ = discover._collect_candidates(["good first issue"], None, "2026-01-01")
    assert len(candidates) == 40
    budget.reset()
    budget.activate(identity="someone")
    candidates, _, _ = discover._collect_candidates(["good first issue"], None, "2026-01-01")
    assert len(candidates) == 80


# --- accounting ---------------------------------------------------------


def test_accounting_counts_requests_per_run():
    budget.activate(identity="someone")
    checks.reset_api_stats()
    checks.record_api_call("repos/o/r/issues/1")
    checks.record_api_call("repos/o/r/issues/1")
    checks.record_api_call("graphql")
    report = checks.budget_report()
    assert report == {"tier": "authenticated", "hourly_budget": 5000, "requests_used": 3}


def test_budget_line_format():
    budget.activate(identity=None)
    checks.reset_api_stats()
    checks.record_api_call("repos/o/r/issues/1")
    assert checks.budget_line() == "budget: anonymous tier, 1 request used (60/hour)"


def test_api_stats_summary_names_the_tier():
    budget.activate(identity="someone")
    checks.reset_api_stats()
    summary = checks.api_stats_summary()
    assert "Budget tier: authenticated (5,000 requests/hour); 0 used this run" in summary
    assert "API usage: 0 calls" in summary


def test_reporting_never_shells_out(monkeypatch):
    # No telemetry: accounting and reporting must not touch the network
    # or spawn subprocesses. The single identity probe at activate()
    # time is the only subprocess the budget system ever causes.
    def boom(*a, **k):
        raise AssertionError("must not shell out")

    monkeypatch.setattr(subprocess, "run", boom)
    budget.activate(identity="someone")
    checks.reset_api_stats()
    checks.record_api_call("repos/o/r/issues/1")
    assert checks.budget_report()["requests_used"] == 1
    assert "authenticated" in checks.budget_line()
    assert "Budget tier: authenticated" in checks.api_stats_summary()
