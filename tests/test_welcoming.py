"""Unit tests for the first-time-friendly / contribution-welcome helpers."""

from taken import checks


def findings(labels=(), ai_source=None, merges=0):
    return {
        "issue": {"labels": list(labels)},
        "ai_policy": {"source": ai_source},
        "repo_health": {"recent_merges": merges},
    }


def test_friendly_labels_matches_case_insensitively():
    got = checks.friendly_labels(findings(["Good First Issue", "bug"]))
    assert got == ["Good First Issue"]


def test_friendly_labels_keeps_original_casing():
    got = checks.friendly_labels(findings(["HELP WANTED"]))
    assert got == ["HELP WANTED"]


def test_friendly_labels_ignores_other_labels():
    assert checks.friendly_labels(findings(["bug", "enhancement"])) == []


def test_friendly_labels_tolerates_missing_findings():
    assert checks.friendly_labels({}) == []
    assert checks.friendly_labels({"issue": {}}) == []


def test_welcoming_signals_contributing_guide():
    got = checks.welcoming_signals(findings(ai_source=".github/CONTRIBUTING.md"))
    assert got == ["has CONTRIBUTING.md"]


def test_welcoming_signals_recent_merges():
    got = checks.welcoming_signals(findings(merges=3))
    assert got == ["3 PRs merged recently"]
    assert checks.welcoming_signals(findings(merges=1)) == ["1 PR merged recently"]


def test_welcoming_signals_empty_when_nothing_found():
    assert checks.welcoming_signals(findings()) == []
    assert checks.welcoming_signals({}) == []


def test_welcoming_signals_combines_both():
    got = checks.welcoming_signals(findings(ai_source="CONTRIBUTING.md", merges=2))
    assert got == ["has CONTRIBUTING.md", "2 PRs merged recently"]
