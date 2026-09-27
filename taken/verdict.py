"""Verdict logic for taken.

TAKEN means a hard signal says the work is spoken for: the issue is closed,
an open PR already covers it, or someone is assigned. A comment that merely
says "I'd like to take this" is a soft signal, so it yields CAUTION instead:
the person may have moved on, and a quick check with them can clear it.
"""

GO = "GO"
TAKEN = "TAKEN"
CAUTION = "CAUTION"


def _truncation_reasons(findings):
    """CAUTION reasons for timeline/comment scans that stopped at the page cap."""
    reasons = []
    scan_truncated = findings.get("scan_truncated") or {}
    if scan_truncated.get("timeline"):
        reasons.append("timeline scan hit the page cap; a linked PR beyond the cap would be missed")
    if scan_truncated.get("comments"):
        reasons.append("comment scan hit the page cap; a claimant beyond the cap would be missed")
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

    health = findings["repo_health"]
    if not health["pushed_recently"] and health["recent_merges"] == 0:
        caution_reasons.append("repo looks stale: no pushes or merges in the last 30 days")

    if taken_reasons:
        return TAKEN, taken_reasons
    if caution_reasons:
        return CAUTION, caution_reasons
    return GO, ["no linked PRs, no assignees, no claimants, repo is active"]
