"""Stale-claim decay thresholds reach every verification path (issue #238)."""

from datetime import datetime, timedelta, timezone

from taken import checks, discover
from taken.cli import main
from taken.mcp_server import discover_candidates


def search_item(number, days_ago):
    updated = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "number": number,
        "title": f"issue {number}",
        "user": {"login": "alice"},
        "repository_url": "https://api.github.com/repos/octo/repo",
        "updated_at": updated,
        "html_url": f"https://github.com/octo/repo/issues/{number}",
    }


def make_fake(items):
    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        if "/issues/" in endpoint:
            if endpoint.endswith("/comments") or endpoint.endswith("/timeline"):
                return []
            return {
                "state": "open",
                "title": "issue 1",
                "labels": [],
                "assignees": [],
                "comments": 0,
                "user": {"login": "alice"},
                "html_url": "https://github.com/octo/repo/issues/1",
                "created_at": "2026-01-01T00:00:00Z",
            }
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


def test_discover_forwards_thresholds_to_run_checks(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake([search_item(1, 1)]))
    seen = []
    real_run_checks = checks.run_checks

    def spy(owner, repo, number, me=None, payload=None, thresholds=None):
        seen.append(thresholds)
        return real_run_checks(owner, repo, number, me=me, payload=payload, thresholds=thresholds)

    monkeypatch.setattr(checks, "run_checks", spy)
    thresholds = {"pr_idle_days": 30, "claim_silence_days": 5, "claim_silence_complex_days": 9}
    results = discover.discover(discover.DiscoverOptions(limit=5, thresholds=thresholds))
    assert seen, "expected run_checks to be called during verification"
    assert all(t == thresholds for t in seen)
    assert [r["target"] for r in results] == ["octo/repo#1"]


def test_discover_passes_none_thresholds_by_default(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake([search_item(1, 1)]))
    seen = []
    real_run_checks = checks.run_checks

    def spy(owner, repo, number, me=None, payload=None, thresholds=None):
        seen.append(thresholds)
        return real_run_checks(owner, repo, number, me=me, payload=payload, thresholds=thresholds)

    monkeypatch.setattr(checks, "run_checks", spy)
    discover.discover(discover.DiscoverOptions(limit=5))
    assert seen, "expected run_checks to be called during verification"
    assert all(t is None for t in seen)


def _capture_discover(monkeypatch):
    captured = {}

    def fake_discover(options=None):
        captured["options"] = options
        return discover.DiscoverResults([], errors=0, total=0, verified=0)

    monkeypatch.setattr(discover, "discover", fake_discover)
    return captured


def test_discover_cli_flags_reach_discover(monkeypatch):
    captured = _capture_discover(monkeypatch)
    assert main(["--discover", "--pr-idle-days", "30"]) == 0
    assert captured["options"].thresholds == {
        "pr_idle_days": 30,
        "claim_silence_days": checks.DEFAULT_CLAIM_SILENCE_DAYS,
        "claim_silence_complex_days": checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS,
    }


def test_discover_cli_uses_default_thresholds_without_flags(monkeypatch):
    captured = _capture_discover(monkeypatch)
    assert main(["--discover"]) == 0
    assert captured["options"].thresholds == checks.default_thresholds()


def test_discover_candidates_forwards_thresholds(monkeypatch):
    captured = _capture_discover(monkeypatch)
    payload = discover_candidates(pr_idle_days=30)
    thresholds = captured["options"].thresholds
    assert thresholds["pr_idle_days"] == 30
    assert thresholds["claim_silence_days"] == checks.DEFAULT_CLAIM_SILENCE_DAYS
    assert thresholds["claim_silence_complex_days"] == checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS
    assert payload["effective_parameters"]["thresholds"] == thresholds


def test_discover_candidates_defaults_thresholds(monkeypatch):
    captured = _capture_discover(monkeypatch)
    payload = discover_candidates()
    assert captured["options"].thresholds == checks.default_thresholds()
    assert payload["effective_parameters"]["thresholds"] == checks.default_thresholds()
