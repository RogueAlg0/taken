"""Tests for issue #130: Thompson-sampling repo budget allocation."""

from datetime import datetime, timedelta, timezone

import pytest

from taken import discover
from taken.checks import TakenError
from taken.discover import DiscoverOptions, _RepoBandit


def ritem(owner, repo, number, days_ago=1):
    updated = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "number": number,
        "title": f"issue {number}",
        "user": {"login": "alice"},
        "repository_url": f"https://api.github.com/repos/{owner}/{repo}",
        "updated_at": updated,
        "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
    }


class ScriptedRng:
    """Deterministic stand-in for random.Random."""

    def __init__(self, randoms=(), betas=(), choices=()):
        self._randoms = list(randoms)
        self._betas = list(betas)
        self._choices = list(choices)

    def random(self):
        return self._randoms.pop(0)

    def betavariate(self, alpha, beta):
        return self._betas.pop(0)

    def choice(self, seq):
        return self._choices.pop(0)


# _RepoBandit unit tests


def test_bandit_unseen_repos_start_at_uniform_prior():
    rng = ScriptedRng(randoms=[0.99], betas=[0.2, 0.9])  # 0.99 >= floor: Thompson path
    bandit = _RepoBandit(rng=rng)
    assert bandit.pick(["a", "b"]) == "b"  # higher Beta(1,1) sample wins
    assert bandit.arms == {}  # picking never mutates arms


def test_bandit_update_moves_posterior():
    bandit = _RepoBandit(rng=ScriptedRng())
    bandit.update("a", True)
    bandit.update("a", True)
    bandit.update("a", False)
    assert bandit.arms["a"] == (3.0, 2.0)


def test_bandit_update_moves_posterior_mean_up():
    bandit = _RepoBandit(rng=ScriptedRng())
    for _ in range(5):
        bandit.update("a", True)
    alpha, beta = bandit.arms["a"]
    assert (alpha, beta) == (6.0, 1.0)
    assert alpha / (alpha + beta) > 0.5  # learned mean beats the uniform prior


def test_bandit_explore_floor_forces_uniform_choice():
    rng = ScriptedRng(randoms=[0.0], choices=["c"])  # 0.0 < 1.0: explore path
    bandit = _RepoBandit(explore_floor=1.0, rng=rng)
    assert bandit.pick(["a", "b", "c"]) == "c"


def test_bandit_explore_floor_clamped():
    assert _RepoBandit(explore_floor=2.0).explore_floor == 1.0
    assert _RepoBandit(explore_floor=-0.5).explore_floor == 0.0


def test_bandit_defaults():
    options = DiscoverOptions()
    assert options.allocation == "bandit"
    assert options.explore_floor == 0.15


def test_discover_rejects_unknown_allocation():
    options = DiscoverOptions(allocation="bogus")
    with pytest.raises(ValueError, match="unknown allocation"):
        discover.discover(options)


# discover() integration: scripted bandit + scripted verifications


class FakeBandit:
    """Records picks/updates; prefers repoB whenever it has work."""

    instances = []

    def __init__(self, explore_floor=0.15, rng=None):
        self.explore_floor = explore_floor
        self.picks = []
        self.updates = []
        FakeBandit.instances.append(self)

    def pick(self, repos):
        self.picks.append(list(repos))
        for r in repos:
            if r[1] == "repoB":
                return r
        return repos[0]

    def update(self, repo, success):
        self.updates.append((repo, success))


def fake_search(monkeypatch, items):
    def fake(endpoint, params=None):
        assert endpoint == "search/issues"
        return {"total_count": len(items), "incomplete_results": False, "items": items}

    from taken import checks

    monkeypatch.setattr(checks, "gh_api", fake)


def scripted_verify(monkeypatch, outcomes):
    """outcomes: {(owner, repo, number): ("go"|"nogo"|"error", score)}."""

    def fake(
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
        kind, score = outcomes[(owner, repo, number)]
        if kind == "error":
            return None, TakenError("boom")
        if kind == "nogo":
            return None, None
        return {
            "target": f"{owner}/{repo}#{number}",
            "score": score,
            "why": ["test"],
            "verdict": "GO",
            "reasons": [],
            "findings": {},
            "updated_at": item.get("updated_at") or "",
            "friendly_labels": [],
            "welcoming": [],
        }, None

    monkeypatch.setattr(discover, "_verify_candidate", fake)


def test_bandit_drives_submission_order_and_learns(monkeypatch):
    FakeBandit.instances.clear()
    items = [
        ritem("o", "repoA", 1),
        ritem("o", "repoB", 2),
        ritem("o", "repoA", 3),
        ritem("o", "repoB", 4),
    ]
    fake_search(monkeypatch, items)
    scripted_verify(
        monkeypatch,
        {
            ("o", "repoA", 1): ("go", 1),
            ("o", "repoB", 2): ("go", 1),
            ("o", "repoA", 3): ("nogo", 0),
            ("o", "repoB", 4): ("error", 0),
        },
    )
    monkeypatch.setattr(discover, "_RepoBandit", FakeBandit)
    options = DiscoverOptions(limit=10, jobs=1)
    results = discover.discover(options)

    bandit = FakeBandit.instances[-1]
    # repoB is preferred while it has queued candidates; the recorded
    # pick lists show repoB offered first each time until it drains.
    assert ("o", "repoB") in bandit.picks[0]
    assert ("o", "repoB") in bandit.picks[1]
    assert ("o", "repoB") not in bandit.picks[2]
    # updates: GO -> True, clean non-GO -> False, error -> no update
    assert bandit.updates == [
        (("o", "repoB"), True),
        (("o", "repoA"), True),
        (("o", "repoA"), False),
    ]
    assert results.verified == 4
    assert results.errors == 1
    # both banked at score 1; the tie-break prefers the fresher updated_at
    assert [r["target"] for r in results] == ["o/repoB#2", "o/repoA#1"]


def test_bandit_submission_order_is_deterministic(monkeypatch):
    """With jobs=1 the fake bandit's repoB-first order is exact."""
    FakeBandit.instances.clear()
    items = [
        ritem("o", "repoA", 1),
        ritem("o", "repoB", 2),
        ritem("o", "repoA", 3),
        ritem("o", "repoB", 4),
    ]
    fake_search(monkeypatch, items)
    seen_order = []

    def fake(
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
        seen_order.append((owner, repo, number))
        return None, None

    monkeypatch.setattr(discover, "_verify_candidate", fake)
    monkeypatch.setattr(discover, "_RepoBandit", FakeBandit)
    discover.discover(DiscoverOptions(limit=10, jobs=1))
    assert seen_order == [
        ("o", "repoB", 2),
        ("o", "repoB", 4),
        ("o", "repoA", 1),
        ("o", "repoA", 3),
    ]


def test_recency_allocation_keeps_old_order(monkeypatch):
    items = [
        ritem("o", "repoA", 1, days_ago=3),
        ritem("o", "repoB", 2, days_ago=1),
        ritem("o", "repoA", 3, days_ago=2),
    ]
    fake_search(monkeypatch, items)
    seen_order = []

    def fake(
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
        seen_order.append((owner, repo, number))
        return None, None

    monkeypatch.setattr(discover, "_verify_candidate", fake)
    discover.discover(DiscoverOptions(limit=10, jobs=1, allocation="recency"))
    # search returns freshest first already; recency submits in pool order
    assert seen_order == [("o", "repoA", 1), ("o", "repoB", 2), ("o", "repoA", 3)]


def test_single_repo_bandit_matches_recency(monkeypatch):
    """Degenerate case: one repo means the bandit cannot reorder anything."""
    items = [ritem("o", "repo", n, days_ago=n) for n in (1, 2, 3)]
    outcomes = {
        ("o", "repo", 1): ("go", 2),
        ("o", "repo", 2): ("go", 5),
        ("o", "repo", 3): ("go", 1),
    }
    results = {}
    for allocation in ("bandit", "recency"):
        fake_search(monkeypatch, items)
        scripted_verify(monkeypatch, outcomes)
        results[allocation] = discover.discover(
            DiscoverOptions(limit=10, jobs=1, allocation=allocation)
        )
    assert [r["target"] for r in results["bandit"]] == [r["target"] for r in results["recency"]]
    assert [r["score"] for r in results["bandit"]] == [5, 2, 1]


def test_cli_allocation_flags_reach_options(monkeypatch, capsys):
    from taken.cli import main

    captured = {}

    def fake_discover(options):
        captured.update(vars(options))
        return discover.DiscoverResults([])

    monkeypatch.setattr(discover, "discover", fake_discover)
    assert (
        main(
            [
                "--discover",
                "--label",
                "good first issue",
                "--allocation",
                "recency",
                "--explore-floor",
                "0.5",
                "--no-progress",
            ]
        )
        == 0
    )
    assert captured["allocation"] == "recency"
    assert captured["explore_floor"] == 0.5


def test_cli_rejects_unknown_allocation(capsys):
    from taken.cli import main

    with pytest.raises(SystemExit):
        main(["--discover", "--allocation", "bogus"])
