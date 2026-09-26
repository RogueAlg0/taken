"""Command-line interface for taken."""

import argparse
import json
import re
import sys

from taken import __version__, checks, discover
from taken.verdict import CAUTION, GO, TAKEN, decide

EXIT_CODES = {GO: 0, TAKEN: 1, CAUTION: 2}

URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")
PATH_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
REPO_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)$")


def parse_target(text):
    """Parse a target into ("issue", owner, repo, number) or ("repo", owner, repo).

    Accepts owner/repo#123, GitHub issue URLs, or a bare owner/repo
    (scan mode: check the repo's open issues automatically).
    """
    text = text.strip()
    for pattern in (URL_RE, SHORT_RE, PATH_RE):
        match = pattern.match(text)
        if match:
            owner, repo, number = match.groups()
            return ("issue", owner, repo, int(number))
    match = REPO_RE.match(text)
    if match:
        owner, repo = match.groups()
        return ("repo", owner, repo)
    return None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="taken",
        description="Check whether a GitHub issue is already taken before you volunteer for it.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="owner/repo#123 or GitHub issue URLs (one or more)",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help="read targets from a file, one per line (blank lines and # comments ignored)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="max open issues to check per owner/repo scan target (default: 20)",
    )
    parser.add_argument(
        "--label",
        metavar="LABEL",
        default=None,
        help="scan mode: only consider open issues carrying this label",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="discover top candidates via GitHub issue search, verified and ranked",
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        default=None,
        help="discover: only consider repos in this language",
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=0,
        metavar="N",
        help="discover: only consider repos with at least N stars",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=discover.DEFAULT_JOBS,
        metavar="N",
        help="discover: verify candidates with N parallel workers "
        f"(default: {discover.DEFAULT_JOBS})",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="discover: hide the progress bar",
    )
    parser.add_argument("--json", action="store_true", help="print the full findings as JSON")
    parser.add_argument(
        "--me",
        metavar="LOGIN",
        default=None,
        help="your GitHub login; your own comments are ignored in the claimant scan",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the API response cache (~/.cache/taken, 1h TTL)",
    )
    parser.add_argument("--version", action="version", version=f"taken {__version__}")
    return parser


def format_human(findings, verdict, reasons):
    issue = findings["issue"]
    health = findings["repo_health"]
    policy = findings["ai_policy"]
    lines = [
        f"taken? {findings['target']}",
        f"verdict: {verdict}",
        "",
        f'  issue: {issue["state"]}, "{issue["title"]}"',
        f"         {issue['url']} ({issue['comment_count']} comments)",
    ]
    if findings["linked_prs"]:
        for pr in findings["linked_prs"]:
            if pr["state"] == "open":
                status = "open"
            elif pr["merged"]:
                status = "merged"
            else:
                status = "closed"
            lines.append(f'  linked PR: #{pr["number"]} "{pr["title"]}" ({status})')
            lines.append(f"             {pr['url']}")
    else:
        lines.append("  linked PRs: none found in timeline")
    if issue["assignees"]:
        lines.append(f"  assignees: {', '.join(issue['assignees'])}")
    else:
        lines.append("  assignees: none")
    if findings["claimants"]:
        for hit in findings["claimants"]:
            lines.append(
                f'  claimant: {hit["author"]} on {hit["date"]} (matched "{hit["pattern"]}")'
            )
            lines.append(f'            "{hit["snippet"]}"')
    else:
        lines.append("  claimants: none found in comments")
    if policy["source"]:
        lines.append(f"  AI policy: {policy['verdict']} ({policy['source']})")
        if policy["snippet"]:
            lines.append(f'             "{policy["snippet"]}"')
    else:
        lines.append("  AI policy: none found (no CONTRIBUTING file)")
    lines.append(
        f"  repo health: pushed {health['pushed_at'] or 'unknown'}, "
        f"{health['recent_merges']} PRs merged in last 30 days, "
        f"{health['stars']} stars"
    )
    lines.append("")
    lines.append("why:")
    for reason in reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_cache:
        checks._CACHE_ENABLED = False
    targets = list(args.targets)
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        targets.append(line)
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 3
    if args.discover:
        if args.targets or args.file:
            parser.error("--discover takes no targets")
        return run_discover(args)
    if not targets:
        parser.error("need at least one target, --file, or --discover")
    if len(targets) == 1:
        return run_single(targets[0], args)
    return run_batch(targets, args)


def format_discover_line(result):
    why = "; ".join(result["why"])
    markers = "".join(f" [{m}]" for m in result["friendly_labels"] + result["welcoming"])
    return f"{result['score']:3}  {result['target']}  {why}{markers}"


def run_discover(args):
    """Search, verify, and rank the top candidates."""
    if args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return 3
    show_progress = not args.no_progress and sys.stderr.isatty()
    bar = None
    if show_progress:
        from tqdm import tqdm

        bar = tqdm(
            total=0,
            desc="verifying candidates",
            unit="issue",
            file=sys.stderr,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}",
        )

    def on_progress(done, total):
        if bar is not None:
            bar.total = total
            bar.n = done
            bar.refresh()

    try:
        results = discover.discover(
            limit=args.limit,
            language=args.language,
            label=args.label,
            min_stars=args.min_stars,
            me=args.me,
            jobs=args.jobs,
            on_progress=on_progress if bar is not None else None,
        )
    except checks.TakenError as exc:
        if bar is not None:
            bar.close()
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if bar is not None:
        bar.close()
    if not results:
        print("no candidates passed verification", file=sys.stderr)
        return 0
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "target": r["target"],
                        "score": r["score"],
                        "why": r["why"],
                        "verdict": r["verdict"],
                        "reasons": r["reasons"],
                        "findings": r["findings"],
                        "friendly_labels": r["friendly_labels"],
                        "welcoming": r["welcoming"],
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        for r in results:
            print(format_discover_line(r))
    return 0


def check_one(owner, repo, number, me):
    """Run the full check on one issue. Returns (target, verdict, reasons, findings)."""
    findings = checks.run_checks(owner, repo, number, me=me)
    verdict, reasons = decide(findings)
    return f"{owner}/{repo}#{number}", verdict, reasons, findings


def run_single(text, args):
    parsed = parse_target(text)
    if not parsed:
        print(
            f"error: could not parse {text!r}; "
            "use owner/repo#123, an issue URL, or owner/repo to scan",
            file=sys.stderr,
        )
        return 3
    if parsed[0] == "repo":
        return run_batch([text], args)
    _, owner, repo, number = parsed
    try:
        target, verdict, reasons, findings = check_one(owner, repo, number, args.me)
    except checks.TakenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(
            json.dumps(
                {"target": target, "verdict": verdict, "reasons": reasons, "findings": findings},
                indent=2,
            )
        )
    else:
        print(format_human(findings, verdict, reasons))
    return EXIT_CODES[verdict]


def format_batch_line(target, verdict, reasons):
    first = reasons[0] if reasons else ""
    return f"{verdict:7} {target}  {first}"


def run_batch(targets, args):
    """Check many targets; print one verdict line each.

    A bare owner/repo target is scanned automatically: its open issues
    (up to --limit, optionally filtered by --label) are each checked.
    When any repo was scanned, a GO-candidate recommendation summary is
    printed at the end.
    Returns 0 when every target produced a verdict, 3 when any target
    failed to parse or its checks errored.
    """
    results = []
    failed = False
    scanned_repo = False
    for text in targets:
        parsed = parse_target(text)
        if not parsed:
            print(
                f"error: could not parse {text!r}; "
                "use owner/repo#123, an issue URL, or owner/repo to scan",
                file=sys.stderr,
            )
            failed = True
            continue
        if parsed[0] == "repo":
            scanned_repo = True
            _, owner, repo = parsed
            try:
                issues = checks.list_open_issues(owner, repo, limit=args.limit, label=args.label)
            except checks.TakenError as exc:
                print(f"error: {text}: {exc}", file=sys.stderr)
                failed = True
                continue
            if not issues:
                print(f"note: {text}: no open issues found", file=sys.stderr)
            for issue_owner, issue_repo, number in issues:
                try:
                    results.append(check_one(issue_owner, issue_repo, number, args.me))
                except checks.TakenError as exc:
                    print(f"error: {issue_owner}/{issue_repo}#{number}: {exc}", file=sys.stderr)
                    failed = True
        else:
            _, owner, repo, number = parsed
            try:
                results.append(check_one(owner, repo, number, args.me))
            except checks.TakenError as exc:
                print(f"error: {text}: {exc}", file=sys.stderr)
                failed = True
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "target": target,
                        "verdict": verdict,
                        "reasons": reasons,
                        "findings": findings,
                    }
                    for target, verdict, reasons, findings in results
                ],
                indent=2,
            )
        )
    else:
        for target, verdict, reasons, _findings in results:
            print(format_batch_line(target, verdict, reasons))
        if scanned_repo and results:
            gos = [
                (target, checks.friendly_labels(findings))
                for target, verdict, _r, findings in results
                if verdict == GO
            ]
            # First-time-friendly issues first: the safest ones to adopt.
            gos.sort(key=lambda item: (not item[1], item[0]))
            print()
            if gos:
                noun = "candidate" if len(gos) == 1 else "candidates"
                parts = [
                    f"{target} ({', '.join(labels)})" if labels else target
                    for target, labels in gos
                ]
                print(f"{len(gos)} GO {noun}: " + ", ".join(parts))
            else:
                print("no GO candidates in this scan.")
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
