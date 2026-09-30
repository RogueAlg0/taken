"""Verdict logic for taken.

TAKEN means a hard signal says the work is spoken for: the issue is closed,
an open PR already covers it, or someone is assigned. A comment that merely
says "I'd like to take this" is a soft signal, so it yields CAUTION instead:
the person may have moved on, and a quick check with them can clear it.
"""

GO = "GO"
TAKEN = "TAKEN"
CAUTION = "CAUTION"

# Labels that mark an issue as suitable for a first-time contributor.
# Matched case-insensitively against the issue's label names. Lives here
# (rather than in checks.py) so decide() can use it without a circular
# import; checks.py imports it from this module.
FIRST_TIME_LABELS = frozenset(
    {
        "good first issue",
        "good-first-issue",
        "good second issue",
        "beginner friendly",
        "beginner-friendly",
        "beginner",
        "first-timers-only",
        "help wanted",
        "easy",
        "up-for-grabs",
        "up for grabs",
        "starter",
        "newbie",
        "newcomer friendly",
        "newcomer-friendly",
    }
)

# Labels suggesting the issue still needs design-level discussion.
# Matched case-insensitively (exact match) against the issue's label names.
DESIGN_SIGNALS = frozenset(
    {
        "needs design",
        "needs-design",
        "needs rfc",
        "needs-rfc",
        "rfc",
        "breaking change",
        "breaking-change",
        "breaking",
        "needs spec",
        "needs-spec",
        "spec needed",
        "design proposal",
        "architecture",
        "proposal",
    }
)

# A thread this long is worth skimming before starting, whatever the labels
# say. Kept high so healthy back-and-forth on a small task stays GO.
LONG_THREAD_COMMENTS = 30


def age_phrase(days):
    """Human phrase for a day count: 'today', 'yesterday', 'N days ago'.

    Lives here (rather than in checks.py) so decide() can use it without a
    circular import; checks.py imports it from this module.
    """
    if days is None:
        return "date unknown"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _truncation_reasons(findings):
    """CAUTION reasons for timeline/comment/label scans that stopped at the page cap."""
    reasons = []
    scan_truncated = findings.get("scan_truncated") or {}
    if scan_truncated.get("timeline"):
        reasons.append("timeline scan hit the page cap; a linked PR beyond the cap would be missed")
    if scan_truncated.get("comments"):
        reasons.append("comment scan hit the page cap; a claimant beyond the cap would be missed")
    if scan_truncated.get("labels"):
        reasons.append(
            "label scan hit the page cap; a design-level label beyond the cap would be missed"
        )
    return reasons


def _difficulty_fit_reasons(findings):
    """CAUTION reasons when a beginner-labeled issue shows heavier signals.

    A long discussion thread or a design-level label does not contradict the
    beginner label; it is just useful context. The reasons are phrased as a
    friendly heads-up for the contributor, never as a judgment on the labels.
    """
    reasons = []
    issue = findings["issue"]
    labels = issue.get("labels") or []
    if not any(label.lower() in FIRST_TIME_LABELS for label in labels):
        return reasons
    for label in labels:
        if label.lower() in DESIGN_SIGNALS:
            reasons.append(
                f"labeled '{label}': worth reading the linked discussion before starting"
            )
    count = issue.get("comment_count") or 0
    if count >= LONG_THREAD_COMMENTS:
        reasons.append(f"{count} comments: a long thread, worth skimming before you start")
    return reasons


def _is_complex_issue(issue):
    """Whether an issue gets the longer claimant-silence window (issue #83).

    Complex means design-level labels or a long discussion thread, reusing
    the same signals as the difficulty-fit heads-up below.
    """
    labels = issue.get("labels") or []
    if any(label.lower() in DESIGN_SIGNALS for label in labels):
        return True
    return (issue.get("comment_count") or 0) >= LONG_THREAD_COMMENTS


def _silence_window_days(findings):
    """Days one claim blocks as CAUTION: 7d on simple issues, 14d on complex."""
    thresholds = findings.get("thresholds") or {}
    if _is_complex_issue(findings["issue"]):
        return thresholds.get("claim_silence_complex_days", 14)
    return thresholds.get("claim_silence_days", 7)


def decide(findings):
    """Return (verdict, reasons). TAKEN wins over CAUTION wins over GO."""
    taken_reasons = []
    caution_reasons = []
    issue = findings["issue"]
    thresholds = findings.get("thresholds") or {}
    pr_idle_days = thresholds.get("pr_idle_days", 90)

    if issue["state"] == "closed":
        taken_reasons.append(f"issue is closed: {issue['url']}")

    for pr in findings["linked_prs"]:
        if pr["state"] == "open":
            idle = pr.get("idle_days")
            if idle is not None and idle > pr_idle_days:
                # Validated half of issue #83: an open PR with no activity
                # past the threshold is stale work, not live coverage, so
                # its TAKEN signal weakens to CAUTION. Unknown activity
                # stays TAKEN (fail closed).
                caution_reasons.append(
                    f"open PR #{pr['number']} idle {idle}d with no activity "
                    f"(past the {pr_idle_days}d threshold): treating as stale: {pr['url']}"
                )
            else:
                detail = ""
                if idle is not None:
                    detail = f" (last activity {age_phrase(idle)})"
                taken_reasons.append(
                    f"open PR #{pr['number']} already covers this{detail}: {pr['url']}"
                )
        elif pr["merged"]:
            caution_reasons.append(
                f"PR #{pr['number']} was merged but the issue is still open (stale?): {pr['url']}"
            )

    if issue["assignees"]:
        taken_reasons.append(f"assigned to: {', '.join(issue['assignees'])}")

    window = _silence_window_days(findings)
    expired_claims = 0
    for hit in findings["claimants"]:
        since = hit.get("days_since_claimant_activity")
        if since is not None and since > window:
            # Claimant-silence redesign (issue #83): a claim blocks as
            # CAUTION only while the claimant was recently active. The
            # clock resets on any claimant activity; a claim never
            # auto-closes anything, it only ever yields CAUTION.
            expired_claims += 1
            continue
        label = hit.get("age_label") or "expressed interest"
        caution_reasons.append(f'{hit["author"]} {label}: "{hit["snippet"]}" ({hit["url"]})')

    policy = findings["ai_policy"]["verdict"]
    if policy == "ban":
        caution_reasons.append("repo bans AI-generated contributions")
    elif policy == "disclosure-required":
        caution_reasons.append("repo requires AI disclosure on contributions")

    # A scan that stopped early at the page cap did not see everything.
    # Downgrade to CAUTION rather than risk a GO on incomplete evidence.
    caution_reasons.extend(_truncation_reasons(findings))

    # A beginner label alongside heavier signals is worth a heads-up, not a
    # contradiction: long threads and design labels are reported as neutral
    # context for the contributor.
    caution_reasons.extend(_difficulty_fit_reasons(findings))

    health = findings["repo_health"]
    if not health["pushed_recently"] and health["recent_merges"] == 0:
        caution_reasons.append("repo looks stale: no pushes or merges in the last 30 days")

    if taken_reasons:
        return TAKEN, taken_reasons
    if caution_reasons:
        return CAUTION, caution_reasons
    if expired_claims:
        return GO, [
            f"{expired_claims} old claim(s) of interest, but no claimant "
            f"activity in the last {window}d: treating as stale"
        ]
    return GO, ["no linked PRs, no assignees, no claimants, repo is active"]
