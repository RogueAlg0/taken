"""Tests for stale-claim decay (issue #83): age labels, PR-idle weakening,
and the claimant-silence redesign. All synthetic; no network."""

from datetime import datetime, timedelta, timezone

from taken import checks
from taken.checks import find_claimant_hits
from taken.verdict import CAUTION, GO, TAKEN, decide

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_comment(body, author="volunteer", days_ago=0):
    return {
        "user": {"login": author},
        "body": body,
        "created_at": ts(days_ago),
        "html_url": "https://github.com/octo/repo/issues/1#issuecomment-1",
    }


def base_findings(**overrides):
    findings = {
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
        "thresholds": checks.default_thresholds(),
        "ai_policy": {"verdict": "none-found", "snippet": "", "source": None},
        "repo_health": {
            "pushed_at": "2026-09-25",
            "pushed_recently": True,
            "recent_merges": 5,
            "stars": 100,
        },
    }
    findings.update(overrides)
    return findings


def linked_pr(number=7, state="open", merged=False, idle_days=None):
    pr = {
        "number": number,
        "title": "Fix it",
        "state": state,
        "merged": merged,
        "author": "dev",
        "url": f"https://github.com/octo/repo/pull/{number}",
        "updated_at": ts(idle_days) if idle_days is not None else None,
        "idle_days": idle_days,
        "age_label": "",
    }
    pr["age_label"] = checks.pr_age_label(pr)
    return pr


def claimant_hit(author="volunteer", age_days=0, since_activity=0):
    return {
        "author": author,
        "date": ts(age_days)[:10],
        "pattern": "i'd like to take",
        "snippet": "I'd like to take this on",
        "url": "https://github.com/octo/repo/issues/1#issuecomment-1",
        "age_days": age_days,
        "days_since_claimant_activity": since_activity,
        "age_label": f"expressed interest {checks.age_phrase(age_days)}",
    }


# --- age labels ---------------------------------------------------------------


def test_claimant_hits_carry_age_labels():
    comments = [
        make_comment("I'd like to take this on.", days_ago=10),
        make_comment("Still interested, any update?", author="volunteer", days_ago=2),
    ]
    hits = find_claimant_hits(comments, now=NOW)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["age_days"] == 10
    # Clock resets on ANY claimant activity: latest comment was 2 days ago.
    assert hit["days_since_claimant_activity"] == 2
    assert hit["age_label"] == "expressed interest 10 days ago"


def test_claimant_hit_with_unknown_date_stays_conservative():
    comments = [
        {
            "user": {"login": "volunteer"},
            "body": "I'd like to take this on.",
            "created_at": "",
            "html_url": "u",
        }
    ]
    hits = find_claimant_hits(comments, now=NOW)
    assert hits[0]["age_days"] is None
    assert hits[0]["days_since_claimant_activity"] is None
    assert hits[0]["age_label"] == "expressed interest date unknown"


def test_linked_prs_carry_age_labels():
    pr = linked_pr(idle_days=96)
    assert pr["age_label"] == "open PR #7, last activity 96 days ago"
    fresh = linked_pr(idle_days=0)
    assert fresh["age_label"] == "open PR #7, last activity today"


# --- PR-idle weakening --------------------------------------------------------


def test_idle_open_pr_weakens_taken_to_caution():
    findings = base_findings(linked_prs=[linked_pr(idle_days=120)])
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("past the 90d threshold" in r for r in reasons)


def test_fresh_open_pr_keeps_taken():
    findings = base_findings(linked_prs=[linked_pr(idle_days=3)])
    verdict, reasons = decide(findings)
    assert verdict == TAKEN
    assert any("already covers this (last activity 3 days ago)" in r for r in reasons)


def test_open_pr_with_unknown_activity_stays_taken():
    # Fail closed: no activity timestamp means we cannot prove idleness.
    findings = base_findings(linked_prs=[linked_pr(idle_days=None)])
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_pr_idle_threshold_is_configurable():
    findings = base_findings(linked_prs=[linked_pr(idle_days=120)])
    findings["thresholds"] = {**checks.default_thresholds(), "pr_idle_days": 200}
    verdict, _ = decide(findings)
    assert verdict == TAKEN


def test_pr_idle_boundary_is_exclusive():
    findings = base_findings(linked_prs=[linked_pr(idle_days=90)])
    verdict, _ = decide(findings)
    assert verdict == TAKEN


# --- claimant-silence redesign --------------------------------------------------


def test_recent_claim_blocks_as_caution():
    findings = base_findings(claimants=[claimant_hit(age_days=2, since_activity=2)])
    verdict, reasons = decide(findings)
    assert verdict == CAUTION
    assert any("volunteer expressed interest 2 days ago" in r for r in reasons)


def test_silent_claim_no_longer_blocks():
    findings = base_findings(claimants=[claimant_hit(age_days=60, since_activity=60)])
    verdict, reasons = decide(findings)
    assert verdict == GO
    assert any("treating as stale" in r for r in reasons)


def test_claim_age_alone_never_changes_verdict():
    # Old claim, but the claimant was active yesterday: still blocks.
    findings = base_findings(claimants=[claimant_hit(age_days=400, since_activity=1)])
    verdict, _ = decide(findings)
    assert verdict == CAUTION


def test_silence_window_is_7d_on_simple_issues():
    findings = base_findings(claimants=[claimant_hit(age_days=8, since_activity=8)])
    assert decide(findings)[0] == GO
    findings = base_findings(claimants=[claimant_hit(age_days=6, since_activity=6)])
    assert decide(findings)[0] == CAUTION


def test_silence_window_is_14d_on_complex_issues():
    findings = base_findings(claimants=[claimant_hit(age_days=10, since_activity=10)])
    findings["issue"]["labels"] = ["needs design"]
    assert decide(findings)[0] == CAUTION
    findings = base_findings(claimants=[claimant_hit(age_days=20, since_activity=20)])
    findings["issue"]["labels"] = ["needs design"]
    assert decide(findings)[0] == GO


def test_long_thread_counts_as_complex():
    findings = base_findings(claimants=[claimant_hit(age_days=10, since_activity=10)])
    findings["issue"]["comment_count"] = 30
    assert decide(findings)[0] == CAUTION


def test_silence_window_is_configurable():
    findings = base_findings(claimants=[claimant_hit(age_days=10, since_activity=10)])
    findings["thresholds"] = {**checks.default_thresholds(), "claim_silence_days": 30}
    assert decide(findings)[0] == CAUTION


def test_claim_with_unknown_activity_stays_caution():
    # Fail closed: unknown activity means we cannot prove silence.
    findings = base_findings(claimants=[claimant_hit(age_days=60, since_activity=None)])
    assert decide(findings)[0] == CAUTION


def test_claim_never_yields_taken():
    findings = base_findings(claimants=[claimant_hit(age_days=0, since_activity=0)])
    verdict, _ = decide(findings)
    assert verdict == CAUTION  # never TAKEN, never auto-closes


def test_threshold_defaults():
    assert checks.default_thresholds() == {
        "pr_idle_days": 90,
        "claim_silence_days": 7,
        "claim_silence_complex_days": 14,
    }
    # decide() works when findings predate the thresholds key.
    findings = base_findings()
    del findings["thresholds"]
    findings["linked_prs"] = [linked_pr(idle_days=120)]
    assert decide(findings)[0] == CAUTION
