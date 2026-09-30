"""Tests for the verdict logic."""

from taken.verdict import CAUTION, GO, TAKEN, decide


def base_findings():
    return {
        "target": "octo/repo#1",
        "issue": {
            "number": 1,
            "state": "open",
            "title": "Some issue",
            "labels": [],
            "assignees": [],
            "comment_count": 0,
            "author": "someone",
            "url": "https://github.com/octo/repo/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "linked_prs": [],
        "claimants": [],
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-25",
            "pushed_recently": True,
            "recent_merges": 5,
            "stars": 100,
        },
    }


def open_pr(number=7, merged=False):
    return {
        "number": number,
        "title": "Fix it",
        "state": "open",
        "merged": merged,
        "author": "dev",
        "url": f"https://github.com/octo/repo/pull/{number}",
    }


def test_go_when_everything_clean():
    verdict, _ = decide(base_findings())
    assert verdict == GO


def test_taken_when_issue_closed():
    findings = base_findings()
    findings["issue"]["state"] = "closed"
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_taken_when_open_pr_linked():
    findings = base_findings()
    findings["linked_prs"] = [open_pr()]
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_taken_when_assignee_present():
    findings = base_findings()
    findings["issue"]["assignees"] = ["dk5488"]
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_caution_when_claimant_found():
    findings = base_findings()
    findings["claimants"] = [
        {
            "author": "dk5488",
            "date": "2026-09-20",
            "pattern": "please assign",
            "snippet": "please assign this to me, thanks",
            "url": "https://github.com/octo/repo/issues/1#issuecomment-1",
        }
    ]
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_caution_when_pr_merged_but_issue_open():
    findings = base_findings()
    pr = open_pr(number=9)
    pr["state"] = "closed"
    pr["merged"] = True
    findings["linked_prs"] = [pr]
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_closed_unmerged_pr_is_not_a_signal():
    findings = base_findings()
    pr = open_pr(number=11)
    pr["state"] = "closed"
    pr["merged"] = False
    findings["linked_prs"] = [pr]
    verdict, _ = decide(findings)
    assert verdict == GO


def test_caution_when_ai_ban():
    findings = base_findings()
    findings["ai_policy"] = {
        "verdict": "ban",
        "snippet": "we do not accept ai-generated contributions",
        "source": "CONTRIBUTING.md",
    }
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_caution_when_ai_disclosure_required():
    findings = base_findings()
    findings["ai_policy"] = {
        "verdict": "disclosure-required",
        "snippet": "please disclose ai assistance",
        "source": "CONTRIBUTING.md",
    }
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_caution_when_repo_stale():
    findings = base_findings()
    findings["repo_health"] = {
        "pushed_at": "2025-01-01",
        "pushed_recently": False,
        "recent_merges": 0,
        "stars": 3,
    }
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_taken_beats_caution():
    findings = base_findings()
    findings["issue"]["assignees"] = ["dk5488"]
    findings["claimants"] = [
        {
            "author": "other",
            "date": "2026-09-20",
            "pattern": "assign me",
            "snippet": "assign me please",
            "url": "https://github.com/octo/repo/issues/1#issuecomment-2",
        }
    ]
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_caution_when_beginner_label_with_long_thread():
    findings = base_findings()
    findings["issue"]["labels"] = ["good first issue"]
    findings["issue"]["comment_count"] = 42
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("42 comments" in reason for reason in reasons)


def test_long_thread_threshold_boundary():
    findings = base_findings()
    findings["issue"]["labels"] = ["good first issue"]
    findings["issue"]["comment_count"] = 29
    assert decide(findings)[0] == GO
    findings["issue"]["comment_count"] = 30
    assert decide(findings)[0] == CAUTION


def test_go_when_beginner_label_with_short_thread():
    findings = base_findings()
    findings["issue"]["labels"] = ["good first issue"]
    findings["issue"]["comment_count"] = 12
    assert decide(findings)[0] == GO


def test_go_when_long_thread_without_beginner_label():
    findings = base_findings()
    findings["issue"]["labels"] = ["bug"]
    findings["issue"]["comment_count"] = 42
    assert decide(findings)[0] == GO


def test_caution_when_beginner_label_with_design_label():
    findings = base_findings()
    findings["issue"]["labels"] = ["good first issue", "needs design"]
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("needs design" in reason for reason in reasons)


def test_go_when_design_label_without_beginner_label():
    findings = base_findings()
    findings["issue"]["labels"] = ["needs design"]
    assert decide(findings)[0] == GO


def test_taken_beats_difficulty_fit_caution():
    findings = base_findings()
    findings["issue"]["state"] = "closed"
    findings["issue"]["labels"] = ["good first issue", "needs design"]
    findings["issue"]["comment_count"] = 42
    assert decide(findings)[0] == TAKEN
