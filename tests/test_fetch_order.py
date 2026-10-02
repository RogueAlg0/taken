"""Fetch-ordering tests: cheapest-decisive-first with early stop (issue #125)."""

import base64
from datetime import datetime, timedelta, timezone

import pytest

from taken import checks
from taken.cli import format_human
from taken.verdict import CAUTION, GO, TAKEN, decide


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_fake(
    *,
    state="open",
    assignees=(),
    timeline_events=(),
    prs=None,
    comments=(),
    contributing_text=None,
    pushed_at=None,
):
    """Fake gh_api for one issue; records every endpoint hit in order."""
    prs = prs or {}
    calls = []

    def fake(endpoint, params=None):
        calls.append(endpoint)
        if endpoint == "repos/octo/repo/issues/1":
            return {
                "state": state,
                "title": "issue 1",
                "labels": [],
                "assignees": [{"login": a} for a in assignees],
                "comments": len(comments),
                "user": {"login": "alice"},
                "html_url": "https://github.com/octo/repo/issues/1",
                "created_at": "2026-01-01T00:00:00Z",
            }
        if endpoint == "repos/octo/repo/issues/1/timeline":
            return list(timeline_events)
        if endpoint == "repos/octo/repo/issues/1/comments":
            return list(comments)
        if endpoint.startswith("repos/octo/repo/pulls/"):
            number = endpoint.rsplit("/", 1)[1]
            return prs[number]
        if endpoint == "repos/octo/repo/pulls":
            return []
        if endpoint == "repos/octo/repo/commits":
            return []
        if endpoint == "repos/octo/repo":
            return {"pushed_at": pushed_at or _now(), "stargazers_count": 4}
        if "/contents/" in endpoint:
            if contributing_text and endpoint.endswith("/CONTRIBUTING.md"):
                return {"content": base64.b64encode(contributing_text.encode()).decode()}
            raise checks.NotFoundError(endpoint)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    fake.calls = calls
    return fake


def cross_ref(pr_number):
    return {
        "event": "cross-referenced",
        "source": {"issue": {"html_url": f"https://github.com/octo/repo/pull/{pr_number}"}},
    }


def pr_payload(number, state="open", merged_at=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "merged_at": merged_at,
        "user": {"login": "bob"},
        "html_url": f"https://github.com/octo/repo/pull/{number}",
        "updated_at": _now(),
    }


def comment(login, body):
    return {
        "user": {"login": login},
        "body": body,
        "author_association": "CONTRIBUTOR",
        "created_at": "2026-09-20T00:00:00Z",
        "html_url": "https://github.com/octo/repo/issues/1#issuecomment-1",
    }


def old_run_checks(owner, repo, number, me=None):
    """The pre-#125 fixed fetch order, for verdict-parity tests."""
    issue = checks.check_issue(owner, repo, number)
    linked_prs, timeline_truncated = checks.check_timeline(owner, repo, number)
    claimants, _, comments_truncated = checks.check_claimants(owner, repo, number, me=me)
    return {
        "target": f"{owner}/{repo}#{number}",
        "issue": issue,
        "linked_prs": linked_prs,
        "claimants": claimants,
        "ai_policy": checks.check_ai_policy(owner, repo),
        "repo_health": checks.check_repo_health(owner, repo),
        "scan_truncated": {
            "timeline": timeline_truncated,
            "comments": comments_truncated,
        },
    }


def test_closed_issue_stops_after_issue_call(monkeypatch):
    fake = make_fake(state="closed")
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert fake.calls == ["repos/octo/repo/issues/1"]
    assert decide(findings)[0] == TAKEN
    assert findings["linked_prs"] == []
    assert findings["ai_policy"]["verdict"] == "not-checked"
    assert findings["repo_health"]["skipped"] is True
    assert findings["stages_skipped"] == [
        "timeline",
        "claimants",
        "ai_policy",
        "repo_health",
    ]


def test_assigned_issue_stops_after_issue_call(monkeypatch):
    fake = make_fake(assignees=("dk5488",))
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert fake.calls == ["repos/octo/repo/issues/1"]
    assert decide(findings)[0] == TAKEN


def test_open_linked_pr_stops_after_timeline(monkeypatch):
    fake = make_fake(
        timeline_events=[cross_ref(7)],
        prs={"7": pr_payload(7, state="open")},
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert "repos/octo/repo/issues/1/comments" not in fake.calls
    assert not any("/contents/" in c for c in fake.calls)
    assert decide(findings)[0] == TAKEN
    assert findings["linked_prs"][0]["number"] == 7
    assert findings["stages_skipped"] == ["claimants", "ai_policy", "repo_health"]


def test_no_signals_fetches_everything_in_order(monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert decide(findings)[0] == GO
    # Cheapest-decisive-first: issue, then timeline, then the expensive scans.
    assert fake.calls[0] == "repos/octo/repo/issues/1"
    assert fake.calls[1] == "repos/octo/repo/issues/1/timeline"
    assert fake.calls[2] == "repos/octo/repo/issues/1/comments"
    assert findings["ai_policy"]["verdict"] == "none-found"
    assert "skipped" not in findings["repo_health"]
    assert findings["stages_skipped"] == []


def test_stale_repo_still_fetches_health(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = make_fake(pushed_at=old)
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    assert decide(findings)[0] == CAUTION
    assert any("repo looks stale" in r for r in decide(findings)[1])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": "closed"},
        {"assignees": ("dk5488",)},
        {"timeline_events": [cross_ref(7)], "prs": {"7": pr_payload(7, state="open")}},
        {
            "timeline_events": [cross_ref(8)],
            "prs": {"8": pr_payload(8, state="closed", merged_at="2026-09-01T00:00:00Z")},
        },
        {"comments": [comment("carol", "I'd like to take this on, please")]},
        {"contributing_text": "# Contributing\nWe does not accept ai-generated code."},
        {},
    ],
    ids=[
        "closed",
        "assigned",
        "open-pr",
        "merged-pr",
        "claimant",
        "ai-ban",
        "clean-go",
    ],
)
def test_verdict_matches_fixed_order(monkeypatch, kwargs):
    """Zero verdict changes versus the old fixed fetch order."""
    for run in (checks.run_checks, old_run_checks):
        fake = make_fake(**kwargs)
        monkeypatch.setattr(checks, "gh_api", fake)
        findings = run("octo", "repo", 1)
        verdict = decide(findings)[0]
        if run is checks.run_checks:
            new_verdict = verdict
        else:
            old_verdict = verdict
    assert new_verdict == old_verdict


NOT_CHECKED = "not checked (verdict already decided)"


def test_human_output_marks_all_stages_not_checked_on_stage1_stop(monkeypatch):
    """Skipped stages are never presented as observed facts."""
    fake = make_fake(state="closed")
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    verdict, reasons = decide(findings)
    out = format_human(findings, verdict, reasons)
    assert f"linked PRs: {NOT_CHECKED}" in out
    assert f"claimants: {NOT_CHECKED}" in out
    assert f"AI policy: {NOT_CHECKED}" in out
    assert f"repo health: {NOT_CHECKED}" in out
    # The old dishonest lines must be gone.
    assert "none found in timeline" not in out
    assert "none found in comments" not in out
    assert "no CONTRIBUTING file" not in out
    assert "PRs merged in last 30 days" not in out
    assert "welcoming:" not in out


def test_human_output_marks_claimants_not_checked_on_stage2_stop(monkeypatch):
    fake = make_fake(
        timeline_events=[cross_ref(7)],
        prs={"7": pr_payload(7, state="open")},
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    verdict, reasons = decide(findings)
    out = format_human(findings, verdict, reasons)
    # Timeline ran: the linked PR is shown.
    assert 'linked PR: #7 "PR 7" (open)' in out
    # Claimants, policy, health did not.
    assert f"claimants: {NOT_CHECKED}" in out
    assert f"AI policy: {NOT_CHECKED}" in out
    assert f"repo health: {NOT_CHECKED}" in out


def test_human_output_unchanged_on_full_run(monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(checks, "gh_api", fake)
    findings = checks.run_checks("octo", "repo", 1)
    verdict, reasons = decide(findings)
    out = format_human(findings, verdict, reasons)
    assert NOT_CHECKED not in out
    assert "claimants: none found in comments" in out
    assert "repo health: pushed" in out
