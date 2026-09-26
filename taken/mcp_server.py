"""MCP server for taken.

Exposes taken's issue checks as tools so coding agents can ask
"is this issue taken?" before volunteering for work.

Run with the ``taken-mcp`` console script (stdio transport). The server
inherits the invoker's environment, so ``gh`` must be installed and
authenticated, exactly like the ``taken`` CLI.

Needs the optional ``mcp`` dependency (``pip install taken-gh[mcp]``).
Without it this module still imports cleanly, but ``main()`` prints
guidance instead of starting a server.

Never print to stdout here: it carries the JSON-RPC stream. Logs go to
stderr only.
"""

import sys
from typing import Annotated

try:
    from pydantic import Field
except ImportError:  # pydantic is only present with the optional MCP dependency

    def Field(**kwargs):
        return kwargs


from taken import __version__, checks, discover
from taken.verdict import decide


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


def scan_repo(
    owner: str,
    repo: str,
    limit: Annotated[int, Field(description="Max open issues to check. Default: 20.")] = 20,
    label: Annotated[
        str | None,
        Field(
            description="Only consider open issues carrying this label. Default: no label filter."
        ),
    ] = None,
    me: Annotated[
        str | None,
        Field(description="Your GitHub login; your own comments are ignored. Default: none."),
    ] = None,
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
    effective_parameters = {"limit": limit, "label": label, "me": me}
    try:
        issues = checks.list_open_issues(owner, repo, limit=limit, label=label)
    except checks.TakenError as exc:
        return {
            "target": f"{owner}/{repo}",
            "effective_parameters": effective_parameters,
            "error": str(exc),
        }
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
        "effective_parameters": effective_parameters,
        "results": results,
        "recommendations": [r["target"] for r in results if r.get("verdict") == "GO"],
        "summary": summary,
    }


def discover_candidates(
    limit: Annotated[int, Field(description="Max candidates to return. Default: 10.")] = 10,
    language: Annotated[
        str | None,
        Field(
            description="Only consider repositories in this language. Default: no language filter."
        ),
    ] = None,
    label: Annotated[
        str | None,
        Field(
            description="Issue label to search. Default: good first issue, good-first-issue, "
            "beginner friendly, and help wanted."
        ),
    ] = None,
    min_contributors: Annotated[
        int,
        Field(
            description="Only consider repositories with at least this many "
            "contributors in the last 90 days. Default: 0."
        ),
    ] = 0,
    me: Annotated[
        str | None,
        Field(description="Your GitHub login; your own comments are ignored. Default: none."),
    ] = None,
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
        min_contributors: only consider repos with at least this many contributors
            in the last 90 days
        me: your GitHub login; your own comments are ignored in the claimant scan
    """
    effective_parameters = {
        "limit": limit,
        "language": language,
        "labels": [label] if label else list(discover.SEARCH_LABELS),
        "min_contributors": min_contributors,
        "me": me,
    }
    try:
        results = discover.discover(
            limit=limit,
            language=language,
            label=label,
            min_contributors=min_contributors,
            me=me,
            jobs=discover.DEFAULT_JOBS,
            on_progress=None,
        )
    except checks.TakenError as exc:
        return {"effective_parameters": effective_parameters, "error": str(exc)}
    return {
        "effective_parameters": effective_parameters,
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
        ],
    }


def _create_server():
    """Build the MCP server and register taken's tools.

    Imported lazily so the ``taken`` CLI installs and runs without the
    optional ``mcp`` dependency.
    """
    from mcp.server import MCPServer

    server = MCPServer(
        "taken",
        title="taken",
        description="Check whether a GitHub issue is already taken before volunteering for it.",
        version=__version__,
    )
    server.tool()(check_issue)
    server.tool()(scan_repo)
    server.tool()(discover_candidates)
    return server


try:
    mcp = _create_server()
except ImportError:  # optional `mcp` dependency not installed
    mcp = None


def main():
    """Entry point for the ``taken-mcp`` console script."""
    if mcp is None:
        print(
            "taken-mcp needs the MCP SDK, which is an optional dependency: "
            'install it with pip install "taken-gh[mcp]"',
            file=sys.stderr,
        )
        return 2
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    print("taken-mcp speaks JSON-RPC on stdio; launch it from an MCP client.", file=sys.stderr)
    sys.exit(2)
