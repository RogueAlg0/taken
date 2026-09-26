"""Single-issue human output carries the first-time-friendly markers."""

from taken.cli import format_human


def make_findings(labels=(), contributing=False, merges=0):
    return {
        "target": "octo/repo#1",
        "issue": {
            "state": "open",
            "title": "issue 1",
            "labels": list(labels),
            "assignees": [],
            "comment_count": 0,
            "author": "alice",
            "url": "https://github.com/octo/repo/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {
            "verdict": "none-found",
            "snippet": "",
            "source": "CONTRIBUTING.md" if contributing else None,
        },
        "repo_health": {
            "pushed_at": "2026-09-26",
            "pushed_recently": True,
            "recent_merges": merges,
            "stars": 10,
        },
    }


def test_format_human_shows_friendly_and_welcoming():
    findings = make_findings(labels=["good first issue", "bug"], contributing=True, merges=2)
    out = format_human(findings, "GO", ["no reason"])
    friendly_line = next(line for line in out.splitlines() if "first-time friendly" in line)
    assert "good first issue" in friendly_line
    assert "bug" not in friendly_line
    assert "welcoming: has CONTRIBUTING.md, 2 PRs merged recently" in out


def test_format_human_hides_markers_when_absent():
    out = format_human(make_findings(), "GO", ["no reason"])
    assert "first-time friendly" not in out
    assert "welcoming" not in out


def test_format_human_shows_partial_markers():
    out = format_human(make_findings(merges=1), "GO", ["no reason"])
    assert "first-time friendly" not in out
    assert "welcoming: 1 PR merged recently" in out
