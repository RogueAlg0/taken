"""Discover early-stop tests (issue #214).

The pool is verified freshest-first, and every candidate's updated_at is
known upfront, so its maximum achievable score is known before any API
call is spent on it. Once the top-`limit` ranking is provably decided,
verification stops early with zero change to the output.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from taken import checks, discover


def _item(number, days_ago):
    updated = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"number": number, "updated_at": updated}


def _entry(number, score, updated_at):
    return {
        "target": f"o/r#{number}",
        "score": score,
        "why": ["test"],
        "verdict": "GO",
        "reasons": [],
        "findings": {},
        "updated_at": updated_at,
        "friendly_labels": [],
        "welcoming": [],
    }


def _stub(monkeypatch, plan):
    """Stub _collect_candidates and _verify_candidate.

    plan: [(number, days_ago, outcome)] in pool order, where outcome is a
    GO score (int), "error" (TakenError), or "filtered" (not a GO verdict).
    Returns the numbers _verify_candidate was called with, in call order.
    """
    calls = []

    def fake_collect(labels, language, updated_after):
        pool = [("o", "r", n, _item(n, d)) for n, d, _ in plan]
        return (pool, [(",".join(labels), len(pool))], [])

    def fake_verify(
        owner,
        repo,
        number,
        item,
        min_contributors,
        me,
        mode="rest",
        thresholds=None,
        repo_memo=None,
    ):
        calls.append(number)
        outcome = next(o for n, _, o in plan if n == number)
        if outcome == "error":
            return None, checks.TakenError("boom")
        if outcome == "filtered":
            return None, None
        return _entry(number, outcome, item["updated_at"]), None

    monkeypatch.setattr(discover, "_collect_candidates", fake_collect)
    monkeypatch.setattr(discover, "_verify_candidate", fake_verify)
    return calls


def test_score_ceiling_boundaries():
    now = datetime.now(timezone.utc)

    def ts(days):
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert discover._score_ceiling(ts(0)) == 6
    assert discover._score_ceiling(ts(7)) == 6
    assert discover._score_ceiling(ts(8)) == 4
    assert discover._score_ceiling(ts(400)) == 4
    # Unparseable timestamps are conservatively treated as fresh.
    assert discover._score_ceiling("not-a-date") == 6
    assert discover._score_ceiling(None) == 6
    assert discover._score_ceiling("") == 6


def test_early_stop_fires_when_top_limit_decided(monkeypatch):
    # Two fresh candidates bank 5 each; the tail is stale (ceiling 4) and
    # can never displace them. Rolling submission evaluates the stop proof
    # before submitting replacement work, so candidates 3-5 are never
    # submitted at all: the submission list below is deterministic, which
    # is exactly what the old eager design could not guarantee.
    calls = _stub(
        monkeypatch,
        [(1, 1, 5), (2, 2, 5), (3, 40, 6), (4, 40, 6), (5, 40, 6)],
    )
    results = discover.discover(discover.DiscoverOptions(limit=2, jobs=1))
    assert [r["target"] for r in results] == ["o/r#1", "o/r#2"]
    assert [r["score"] for r in results] == [5, 5]
    assert calls == [1, 2]
    assert results.verified == 2
    assert results.total == 5
    assert results.errors == 0


def test_no_early_stop_while_fresh_candidate_remains(monkeypatch):
    # Candidate 3 is fresh (ceiling 6): it could still score 6 and displace
    # a banked 5, so the stop must not fire until it is verified.
    calls = _stub(
        monkeypatch,
        [(1, 1, 5), (2, 2, 5), (3, 3, 1), (4, 40, 1), (5, 50, 1)],
    )
    results = discover.discover(discover.DiscoverOptions(limit=2, jobs=1))
    assert calls == [1, 2, 3]
    assert results.verified == 3
    assert results.total == 5
    assert [r["target"] for r in results] == ["o/r#1", "o/r#2"]


def test_early_stop_matches_full_verification(monkeypatch):
    plan = [
        (1, 1, 6),
        (2, 2, 4),
        (3, 40, 3),
        (4, 4, "error"),
        (5, 50, "filtered"),
        (6, 3, 5),
        (7, 60, 2),
    ]
    _stub(monkeypatch, plan)
    full = discover.discover(discover.DiscoverOptions(limit=10, jobs=1))
    assert full.verified == full.total == 7
    assert full.errors == 1

    _stub(monkeypatch, plan)
    early = discover.discover(discover.DiscoverOptions(limit=2, jobs=1))
    assert early.verified < early.total
    assert [r["target"] for r in early] == [r["target"] for r in full[:2]]
    assert [r["score"] for r in early] == [r["score"] for r in full[:2]]


def test_limit_zero_verifies_nothing(monkeypatch):
    calls = _stub(monkeypatch, [(1, 1, 6), (2, 2, 6)])
    results = discover.discover(discover.DiscoverOptions(limit=0, jobs=1))
    assert results == []
    assert results.verified == 0
    assert results.total == 2
    assert calls == []


def test_verified_counts_everything_when_stop_never_fires(monkeypatch):
    calls = _stub(monkeypatch, [(1, 1, 2), (2, 40, 1), (3, 3, "error")])
    results = discover.discover(discover.DiscoverOptions(limit=10, jobs=1))
    assert [r["target"] for r in results] == ["o/r#1", "o/r#2"]
    assert results.verified == 3
    assert results.total == 3
    assert results.errors == 1
    assert calls == [1, 2, 3]


def test_no_submission_after_stop_with_blocking_verifier(monkeypatch):
    # jobs=2 with a verifier that blocks until released: candidate 1's
    # completion alone decides the top-1 ranking (limit=1, every candidate
    # stale with ceiling 4, banked 5 > 4), so candidate 3 must never be
    # submitted even though a worker sits free. The old eager design
    # submitted the whole pool up front and fails this test immediately
    # (all three candidates are submitted during priming).
    scores = {1: 5, 2: 6, 3: 6}
    gates = {n: threading.Event() for n in scores}
    calls = []
    calls_lock = threading.Lock()
    primed = threading.Event()

    def fake_collect(labels, language, updated_after):
        pool = [("o", "r", n, _item(n, 40)) for n in scores]
        return (pool, [(",".join(labels), len(pool))], [])

    def fake_verify(
        owner,
        repo,
        number,
        item,
        min_contributors,
        me,
        mode="rest",
        thresholds=None,
        repo_memo=None,
    ):
        with calls_lock:
            calls.append(number)
            if len(calls) == 2:
                primed.set()
        assert gates[number].wait(timeout=30), f"verifier for #{number} never released"
        return _entry(number, scores[number], item["updated_at"]), None

    monkeypatch.setattr(discover, "_collect_candidates", fake_collect)
    monkeypatch.setattr(discover, "_verify_candidate", fake_verify)

    results_holder = {}

    def run():
        results_holder["results"] = discover.discover(discover.DiscoverOptions(limit=1, jobs=2))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert primed.wait(timeout=30), "pool was never primed with 2 submissions"
        # Release only candidate 1: its completion banks 5, above every
        # remaining ceiling (4), so the stop proof holds from here on.
        gates[1].set()
        # Quiet window: if the main thread were going to submit candidate
        # 3, it would do so as soon as it consumed candidate 1's result.
        time.sleep(1.0)
        with calls_lock:
            assert sorted(calls) == [1, 2], f"submitted after the stop proof held: {calls}"
    finally:
        # Release everything so discover() can always finish; a failure
        # above must fail the test, never hang the suite.
        gates[1].set()
        gates[2].set()
        worker.join(timeout=30)
        assert not worker.is_alive(), "discover() did not finish"

    results = results_holder["results"]
    assert [r["target"] for r in results] == ["o/r#1"]
    # Both submitted candidates ran (candidate 2's result was discarded);
    # candidate 3 was never submitted.
    assert sorted(calls) == [1, 2]
    assert results.verified == 2
    assert results.total == 3
