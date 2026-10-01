"""Tests for parallel tail stages (issue #213).

After the last TAKEN exit point, check_claimants / check_ai_policy /
check_repo_health are provably independent (no decide() between them), so
the authenticated tier runs them concurrently; the anonymous tier keeps the
exact sequential behavior. Inside check_repo_health the repo record, the
pulls scan, and the commits scan are likewise independent.
"""

import threading

import pytest

from taken import budget, checks
from taken.checks import NotFoundError


@pytest.fixture(autouse=True)
def _reset_budget():
    budget.reset()
    yield
    budget.reset()


def _recent_timestamp():
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_health_fake():
    """Fake gh_api serving the three health-check fetch groups."""
    calls = []

    def fake(endpoint, params=None):
        calls.append(endpoint)
        if endpoint == "repos/o/r":
            return {"pushed_at": _recent_timestamp()}
        if endpoint == "repos/o/r/pulls":
            return [{"merged_at": _recent_timestamp()}]
        if endpoint == "repos/o/r/commits":
            return [{"author": {"login": "alice"}, "commit": {"author": {}}}]
        if "/contents/" in endpoint:
            raise NotFoundError(endpoint)
        if endpoint == "repos/o/r/issues/1/comments":
            return []
        raise AssertionError(f"unexpected endpoint {endpoint}")

    fake.calls = calls
    return fake


def test_health_parallel_matches_sequential(monkeypatch):
    fake = make_health_fake()
    monkeypatch.setattr(checks, "gh_api", fake)
    budget.activate(identity=None)  # anonymous: sequential
    sequential = checks.check_repo_health("o", "r")
    seq_calls = sorted(fake.calls)
    fake.calls.clear()
    budget.activate(identity="someone")  # authenticated: parallel
    parallel = checks.check_repo_health("o", "r")
    assert parallel == sequential
    # Same calls, none added or lost by the concurrent pagination.
    assert sorted(fake.calls) == seq_calls
    assert parallel["recent_merges"] == 1
    assert parallel["contributors"] == 1
    assert parallel["pushed_recently"] is True


def test_health_groups_run_concurrently(monkeypatch):
    started = []
    lock = threading.Lock()
    go = threading.Event()

    def fake(endpoint, params=None):
        group = endpoint.split("?")[0]
        with lock:
            if group not in started:
                started.append(group)
            if len(started) == 3:
                go.set()
        # If the groups ran sequentially this times out and fails: proof
        # of overlap without any timing assertions.
        assert go.wait(timeout=10), f"health groups did not overlap: {started}"
        return make_health_fake()(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    budget.activate(identity="someone")
    result = checks.check_repo_health("o", "r")
    assert result["recent_merges"] == 1
    assert len(started) == 3


def _stub_early_stages(monkeypatch):
    monkeypatch.setattr(checks, "check_issue", lambda *a, **k: {"number": 1})
    monkeypatch.setattr(checks, "check_timeline", lambda *a, **k: ([], False))
    monkeypatch.setattr(checks, "decide", lambda findings: ("GO", []))


def test_tail_stages_run_concurrently(monkeypatch):
    started = []
    lock = threading.Lock()
    go = threading.Event()

    def gate(name):
        with lock:
            started.append(name)
            if len(started) == 3:
                go.set()
        assert go.wait(timeout=10), f"tail stages did not overlap: {started}"

    def fake_claimants(*a, **k):
        gate("claimants")
        return ([], [], False) if k.get("return_comments") else ([], False)

    def fake_policy(*a, **k):
        gate("policy")
        return {"verdict": "none-found", "snippet": "", "source": None}

    def fake_health(*a, **k):
        gate("health")
        return {
            "pushed_at": "2026-01-01",
            "pushed_recently": True,
            "recent_merges": 0,
            "contributors": 0,
            "contributors_window_days": 90,
            "skipped": False,
        }

    _stub_early_stages(monkeypatch)
    monkeypatch.setattr(checks, "check_claimants", fake_claimants)
    monkeypatch.setattr(checks, "check_ai_policy", fake_policy)
    monkeypatch.setattr(checks, "check_repo_health", fake_health)
    budget.activate(identity="someone")
    findings = checks.run_checks("o", "r", 1)
    assert sorted(started) == ["claimants", "health", "policy"]
    assert findings["stages_skipped"] == []
    assert findings["claimants"] == []


def test_tail_findings_parity_parallel_vs_sequential(monkeypatch):
    fake = make_health_fake()
    monkeypatch.setattr(checks, "gh_api", fake)
    _stub_early_stages(monkeypatch)
    budget.activate(identity=None)
    sequential = checks.run_checks("o", "r", 1)
    budget.activate(identity="someone")
    parallel = checks.run_checks("o", "r", 1)
    assert parallel == sequential
    assert parallel["ai_policy"]["verdict"] == "none-found"
    assert parallel["repo_health"]["contributors"] == 1


def test_anonymous_tail_never_leaves_main_thread(monkeypatch):
    main_thread = threading.get_ident()
    seen = []

    def fake_claimants(*a, **k):
        seen.append(threading.get_ident())
        return ([], [], False) if k.get("return_comments") else ([], False)

    def fake_policy(*a, **k):
        seen.append(threading.get_ident())
        return {"verdict": "none-found", "snippet": "", "source": None}

    def fake_health(*a, **k):
        seen.append(threading.get_ident())
        return {"pushed_recently": True}

    _stub_early_stages(monkeypatch)
    monkeypatch.setattr(checks, "check_claimants", fake_claimants)
    monkeypatch.setattr(checks, "check_ai_policy", fake_policy)
    monkeypatch.setattr(checks, "check_repo_health", fake_health)
    budget.activate(identity=None)  # anonymous: sequential
    checks.run_checks("o", "r", 1)
    assert seen == [main_thread] * 3
