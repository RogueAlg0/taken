"""MCP server for taken.

Exposes taken's issue checks as tools so coding agents can ask
"is this issue taken?" before volunteering for work.

Run with the ``taken-mcp`` console script (stdio transport). The server
inherits the invoker's environment, so ``gh`` must be installed and
authenticated, exactly like the ``taken`` CLI.

Never print to stdout here: it carries the JSON-RPC stream. Logs go to
stderr only.
"""

import sys

from mcp.server import MCPServer

from taken import __version__, checks, discover
from taken.verdict import decide

mcp = MCPServer(
    "taken",
    title="taken",
    description="Check whether a GitHub issue is already taken before volunteering for it.",
    version=__version__,
)


def _check_one(owner, repo, number, me=None):
    """Run the full check suite on one issue; return the tool payload."""
    findings = checks.run_checks(owner, repo, number, me=me)
    verdict, reasons = decide(findings)
    return {
        "target": f"{owner}/{repo}#{number}",
        "verdict": verdict,
        "reasons": reasons,
        "findings": findings,
        "friendly_labels": checks.friendly_labels(findings),
        "welcoming": checks.welcoming_signals(findings),
    }


# Verdict ordering for scan_repo: best candidates first.
_VERDICT_RANK = {"GO": 0, "CAUTION": 1, "TAKEN": 2}


@mcp.tool()
def check_issue(owner: str, repo: str, issue_number: int, me: str | None = None) -> dict:
    """Check whether a GitHub issue is already taken.

    Verdicts: GO (free to volunteer), TAKEN (spoken for: closed, open PR,
    or assignee), CAUTION (soft signal: someone expressed interest, the repo
    bans AI contributions, or the repo looks stale).

    The payload also carries `friendly_labels` (first-time-contributor
    labels on the issue) and `welcoming` (repo-level signs contributions
    are welcome), the same markers `scan_repo` returns.

    Args:
        owner: repository owner login
        repo: repository name
        issue_number: issue number to check
        me: your GitHub login; your own comments are ignored in the claimant scan
    """
    try:
        return _check_one(owner, repo, issue_number, me=me)
    except checks.TakenError as exc:
        return {"target": f"{owner}/{repo}#{issue_number}", "error": str(exc)}


@mcp.tool()
def scan_repo(
    owner: str,
    repo: str,
    limit: int = 20,
    label: str | None = None,
    me: str | None = None,
) -> dict:
    """Scan a repository's open issues and recommend the GO ones.

    Results are ordered GO first, then CAUTION, then TAKEN, so the best
    candidates to volunteer for come first. `recommendations` lists just
    the GO targets; `summary` counts each verdict. Each result carries
    `friendly_labels` (first-time-contributor labels on the issue) and
    `welcoming` (repo-level signs contributions are welcome), so an agent
    can prefer the safest issues to adopt.

    Args:
        owner: repository owner login
        repo: repository name
        limit: max open issues to check (default 20)
        label: only consider open issues carrying this label
        me: your GitHub login; your own comments are ignored in the claimant scan
    """
    try:
        issues = checks.list_open_issues(owner, repo, limit=limit, label=label)
    except checks.TakenError as exc:
        return {"target": f"{owner}/{repo}", "error": str(exc)}
    results = []
    for issue_owner, issue_repo, number in issues:
        try:
            payload = _check_one(issue_owner, issue_repo, number, me=me)
            findings = payload["findings"]
            results.append(
                {
                    "target": payload["target"],
                    "verdict": payload["verdict"],
                    "reasons": payload["reasons"],
                    "friendly_labels": checks.friendly_labels(findings),
                    "welcoming": checks.welcoming_signals(findings),
                }
            )
        except checks.TakenError as exc:
            results.append({"target": f"{issue_owner}/{issue_repo}#{number}", "error": str(exc)})
    results.sort(key=lambda r: _VERDICT_RANK.get(r.get("verdict"), 3))
    summary = {"GO": 0, "CAUTION": 0, "TAKEN": 0, "errors": 0}
    for item in results:
        verdict = item.get("verdict")
        if verdict in summary:
            summary[verdict] += 1
        else:
            summary["errors"] += 1
    return {
        "target": f"{owner}/{repo}",
        "results": results,
        "recommendations": [r["target"] for r in results if r.get("verdict") == "GO"],
        "summary": summary,
    }


@mcp.tool()
def discover_candidates(
    limit: int = 10,
    language: str | None = None,
    label: str | None = None,
    min_stars: int = 0,
    me: str | None = None,
) -> dict:
    """Discover top open-source contribution candidates.

    Searches GitHub for good-first-issue style issues, runs taken's full
    verification on each, and returns the ranked candidates. Each result
    carries `friendly_labels` (first-time-contributor labels on the issue)
    and `welcoming` (repo-level signs contributions are welcome).

    Args:
        limit: max candidates to return (default 10)
        language: only consider repos in this language
        label: issue label to search (defaults to good-first-issue style labels)
        min_stars: only consider repos with at least this many stars
        me: your GitHub login; your own comments are ignored in the claimant scan
    """
    try:
        results = discover.discover(
            limit=limit,
            language=language,
            label=label,
            min_stars=min_stars,
            me=me,
            jobs=discover.DEFAULT_JOBS,
            on_progress=None,
        )
    except checks.TakenError as exc:
        return {"error": str(exc)}
    return {
        "results": [
            {
                "target": item["target"],
                "score": item["score"],
                "why": item["why"],
                "verdict": item["verdict"],
                "reasons": item["reasons"],
                "friendly_labels": item["friendly_labels"],
                "welcoming": item["welcoming"],
            }
            for item in results
        ]
    }


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    print("taken-mcp speaks JSON-RPC on stdio; launch it from an MCP client.", file=sys.stderr)
    sys.exit(2)
