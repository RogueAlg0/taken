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


def _truncation_reasons(findings):
    """CAUTION reasons for timeline/comment scans that stopped at the page cap."""
    reasons = []
    scan_truncated = findings.get("scan_truncated") or {}
    if scan_truncated.get("timeline"):
        reasons.append("timeline scan hit the page cap; a linked PR beyond the cap would be missed")
    if scan_truncated.get("comments"):
        reasons.append("comment scan hit the page cap; a claimant beyond the cap would be missed")
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


def decide(findings):
    """Return (verdict, reasons). TAKEN wins over CAUTION wins over GO."""
    taken_reasons = []
    caution_reasons = []
    issue = findings["issue"]

    if issue["state"] == "closed":
        taken_reasons.append(f"issue is closed: {issue['url']}")

    for pr in findings["linked_prs"]:
        if pr["state"] == "open":
            taken_reasons.append(f"open PR #{pr['number']} already covers this: {pr['url']}")
        elif pr["merged"]:
            caution_reasons.append(
                f"PR #{pr['number']} was merged but the issue is still open (stale?): {pr['url']}"
            )

    if issue["assignees"]:
        taken_reasons.append(f"assigned to: {', '.join(issue['assignees'])}")

    for hit in findings["claimants"]:
        caution_reasons.append(
            f"{hit['author']} expressed interest on {hit['date']}: "
            f'"{hit["snippet"]}" ({hit["url"]})'
        )

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
    return GO, ["no linked PRs, no assignees, no claimants, repo is active"]
