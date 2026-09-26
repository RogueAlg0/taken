"""Discover tests: search -> verify -> rank."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from taken import checks, discover
from taken.cli import main


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


def comment(login, body="looks good, thanks", author_association="MEMBER"):
    return {
        "user": {"login": login},
        "body": body,
        "author_association": author_association,
        "created_at": "2026-09-20T00:00:00Z",
        "html_url": "https://github.com/octo/repo/issues/1#issuecomment-1",
    }


def make_fake(items, states, comments_map, labels_map=None, contributing=False):
    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            if endpoint.endswith("/comments"):
                return comments_map.get(number, [])
            if endpoint.endswith("/timeline"):
                return []
            kind = states.get(number, "go")
            labels = [{"name": name} for name in (labels_map or {}).get(number, [])]
            return {
                "state": "open",
                "title": f"issue {number}",
                "labels": labels,
                "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
                "comments": len(comments_map.get(number, [])),
                "user": {"login": "alice"},
                "html_url": f"https://github.com/octo/repo/issues/{number}",
                "created_at": "2026-01-01T00:00:00Z",
            }
        if "/contents/" in endpoint:
            if contributing and endpoint.endswith("/CONTRIBUTING.md"):
                text = base64.b64encode(b"# Contributing\nBe kind, write tests.").decode()
                return {"content": text}
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
    items = [search_item(1, 1), search_item(2, 2), search_item(3, 40)]
    states = {1: "go", 2: "taken", 3: "go"}
    comments_map = {1: [comment("maintainer-bob")]}
    monkeypatch.setattr(checks, "gh_api", make_fake(items, states, comments_map))


def test_discover_ranks_verified_candidates(faked, capsys):
    assert main(["--discover", "--label", "good first issue"]) == 0
    lines = capsys.readouterr().out.strip("\n").splitlines()
    assert len(lines) == 2  # #2 is TAKEN, filtered out
    assert lines[0].startswith("  6  octo/repo#1")
    assert "maintainer replied" in lines[0]
    assert "updated 1d ago" in lines[0]
    assert lines[1].startswith("  1  octo/repo#3")


def test_discover_searches_all_labels_by_default(faked, capsys):
    assert main(["--discover"]) == 0
    lines = capsys.readouterr().out.strip("\n").splitlines()
    assert len(lines) == 2  # deduped across the label searches


def test_discover_min_contributors_filters_everything(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--min-contributors", "100"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "no candidates passed verification" in out.err


def test_discover_json(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(r["target"], r["score"]) for r in data] == [
        ("octo/repo#1", 6),
        ("octo/repo#3", 1),
    ]
    assert all(r["verdict"] == "GO" for r in data)
    assert all(r["friendly_labels"] == [] for r in data)
    assert all(r["welcoming"] == [] for r in data)


def test_discover_marks_friendly_and_welcoming(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(
        items,
        {1: "go"},
        {},
        labels_map={1: ["good first issue", "bug"]},
        contributing=True,
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 0
    line = capsys.readouterr().out.strip("\n").splitlines()[0]
    assert "[good first issue]" in line
    assert "[has CONTRIBUTING.md]" in line
    assert "[bug]" not in line


def test_discover_json_carries_friendly_and_welcoming(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(
        items,
        {1: "go"},
        {},
        labels_map={1: ["Help Wanted"]},
        contributing=True,
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["friendly_labels"] == ["Help Wanted"]
    assert data[0]["welcoming"] == ["has CONTRIBUTING.md"]


def test_discover_tie_break_prefers_recent(monkeypatch):
    # Two equal-score candidates: the more recently updated one ranks first.
    items = [search_item(1, 1), search_item(2, 2)]
    monkeypatch.setattr(checks, "gh_api", make_fake(items, {1: "go", 2: "go"}, {}))
    results = discover.discover(label="good first issue")
    assert [r["target"] for r in results] == ["octo/repo#1", "octo/repo#2"]
    assert results[0]["score"] == results[1]["score"]


def test_discover_searches_every_label(monkeypatch, capsys):
    # The first label alone returns enough items to fill the whole pool;
    # the second label must still contribute candidates. jobs=1 keeps the
    # ranking deterministic (parallel completion order is arbitrary).
    label_one = [search_item(n, 1) for n in range(1, 51)]
    label_two = [search_item(n, 1) for n in range(101, 106)]
    for item in label_one + label_two:
        item["updated_at"] = "2026-09-25T00:00:00Z"  # identical: ties keep pool order
    base = make_fake([], {}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            q = (params or {}).get("q", "")
            if 'label:"good first issue"' in q:
                items = label_one
            elif 'label:"good-first-issue"' in q:
                items = label_two
            else:
                items = []
            return {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    searched = []
    # limit=50 returns the whole verify pool: this test is about which
    # labels contribute candidates to the pool, not about ranking ties,
    # so it must not depend on top-N tie-break order.
    results = discover.discover(jobs=1, limit=50, on_searched=searched.append)
    targets = [r["target"] for r in results]
    assert len(targets) <= discover.VERIFY_POOL
    assert "octo/repo#1" in targets  # first label contributed
    assert "octo/repo#101" in targets  # second label contributed too
    assert ("good first issue", 50) in searched[0]
    assert ("good-first-issue", 5) in searched[0]

    # The CLI reports the searched labels and counts on stderr.
    assert main(["--discover"]) == 0
    err = capsys.readouterr().err
    assert "good first issue (50)" in err
    assert "good-first-issue (5)" in err


def test_discover_with_targets_is_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--discover", "octo/repo#1"])
    assert exc.value.code == 2


def test_build_query():
    q = discover.build_query("good first issue", language="python", updated_after="2026-08-26")
    assert q == (
        'is:open is:issue no:assignee label:"good first issue" updated:>=2026-08-26 language:python'
    )
    assert discover.build_query("help wanted") == 'is:open is:issue no:assignee label:"help wanted"'


def test_maintainer_engaged():
    issue = {"author": "alice"}
    assert discover.maintainer_engaged(issue, [comment("maintainer-bob")]) is True
    assert discover.maintainer_engaged(issue, [comment("alice")]) is False
    assert discover.maintainer_engaged(issue, [comment("some[bot]")]) is False
    assert discover.maintainer_engaged(issue, []) is False


def test_maintainer_engaged_uses_author_association():
    issue = {"author": "alice"}
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert (
            discover.maintainer_engaged(issue, [comment("pat", author_association=assoc)]) is True
        )
    for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", None):
        assert (
            discover.maintainer_engaged(issue, [comment("pat", author_association=assoc)]) is False
        )


def test_maintainer_engaged_ignores_me():
    issue = {"author": "alice"}
    mine = [comment("me", author_association="MEMBER")]
    assert discover.maintainer_engaged(issue, mine, me="me") is False
    assert discover.maintainer_engaged(issue, mine) is True
    assert (
        discover.maintainer_engaged(
            issue, mine + [comment("owner-amy", author_association="OWNER")], me="me"
        )
        is True
    )


def test_discover_me_comment_not_counted_as_maintainer(monkeypatch, capsys):
    items = [search_item(1, 1)]
    fake = make_fake(items, {1: "go"}, {1: [comment("me", author_association="MEMBER")]})
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue", "--me", "me"]) == 0
    line = capsys.readouterr().out.strip("\n").splitlines()[0]
    assert "maintainer replied" not in line


def test_discover_all_errors_reported_distinctly(monkeypatch, capsys):
    items = [search_item(1, 1), search_item(2, 2)]
    base = make_fake(items, {1: "go", 2: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 2, "incomplete_results": False, "items": items}
        if "/issues/" in endpoint:
            raise checks.TakenError("network down")
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 0
    err = capsys.readouterr().err
    assert "no candidates passed verification: all 2 errored" in err
    assert "gh auth status" in err


def test_discover_partial_errors_reported_with_counts(monkeypatch, capsys):
    items = [search_item(1, 1), search_item(2, 2)]
    base = make_fake(items, {1: "taken", 2: "go"}, {})

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 2, "incomplete_results": False, "items": items}
        if endpoint == "repos/octo/repo/issues/2":
            raise checks.TakenError("network down")
        return base(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--discover", "--label", "good first issue"]) == 0
    err = capsys.readouterr().err
    assert "no candidates passed verification (1 of 2 candidates failed with errors)" in err


def test_discover_results_carry_error_stats(monkeypatch):
    items = [search_item(1, 1)]

    def fake(endpoint, params=None):
        if endpoint == "search/issues":
            return {"total_count": 1, "incomplete_results": False, "items": items}
        raise checks.TakenError("network down")

    monkeypatch.setattr(checks, "gh_api", fake)
    results = discover.discover(label="good first issue", jobs=1)
    assert results == []
    assert results.errors == 1
    assert results.total == 1


def test_repo_of():
    assert discover.repo_of({"repository_url": "https://api.github.com/repos/octo/repo"}) == (
        "octo",
        "repo",
    )
    assert discover.repo_of({}) is None


def test_search_issues_rejects_unexpected(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", lambda endpoint, params=None: {"items": "nope"})
    with pytest.raises(checks.TakenError):
        checks.search_issues("is:open")


def test_score_candidate_explains(monkeypatch):
    findings = {"repo_health": {"pushed_at": "2026-09-26"}}
    monkeypatch.setattr(discover, "_days_ago", lambda ts: 1)
    points, why = discover.score_candidate(findings, "2026-09-25T00:00:00Z", True)
    assert points == 6
    assert why == ["maintainer replied", "updated 1d ago", "repo pushed 1d ago"]


def test_discover_parallel_matches_sequential(faked):
    seq = discover.discover(label="good first issue", jobs=1)
    par = discover.discover(label="good first issue", jobs=8)
    assert [(r["target"], r["score"]) for r in par] == [(r["target"], r["score"]) for r in seq]
    assert len(par) == 2


def test_discover_progress_callback(faked):
    calls = []

    def track(done, total):
        calls.append((done, total))

    results = discover.discover(label="good first issue", jobs=4, on_progress=track)
    assert results  # sanity: the fake still yields candidates
    total = calls[0][1]
    assert total == 3  # three candidates enter the pool
    assert calls[0] == (0, total)
    dones = [done for done, _ in calls[1:]]
    assert sorted(dones) == [1, 2, 3]
    assert all(t == total for _, t in calls)


def test_discover_jobs_flag_rejected_when_zero(faked, capsys):
    assert main(["--discover", "--label", "good first issue", "--jobs", "0"]) == 3
    assert "--jobs must be at least 1" in capsys.readouterr().err
