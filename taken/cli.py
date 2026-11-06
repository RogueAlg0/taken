"""Command-line interface for taken."""

import argparse
import concurrent.futures
import functools
import json
import re
import sys
import time

from taken import __version__, budget, checks, discover, graphql, health
from taken.verdict import CAUTION, GO, TAKEN, decide

EXIT_CODES = {GO: 0, TAKEN: 1, CAUTION: 2}

URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")
PATH_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
REPO_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)$")


def parse_target(text):
    """Parse a target into ("issue", owner, repo, number) or ("repo", owner, repo, None).

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
        return ("repo", owner, repo, None)
    return None


def _parse_error(text):
    """Print the standard unparseable-target error; return exit code 3."""
    print(
        f"error: could not parse {text!r}; use owner/repo#123, an issue URL, or owner/repo to scan",
        file=sys.stderr,
    )
    return 3


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _effective_limit(args, default):
    """Explicit --limit, or the mode's default when the flag was not passed.

    The parser default is None so each mode can apply its own default:
    an explicit --limit always wins, whatever the mode.
    """
    return args.limit if args.limit is not None else default


def _probability(value):
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit 3, not argparse's default 2.

    Exit 2 means CAUTION, so a usage error must not share it (#338).
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def build_parser():
    """Build and return the argument parser for the taken CLI.

    Takes no arguments. Configures command-line flags and options for
    positional issue targets, file inputs (--file), repository scanning
    (--limit, --label), candidate discovery (--discover), maintainer health
    reports (--health), cache management, output formatting (--json, --verbose,
    --debug), and stale-claim decay thresholds.
    """
    parser = _Parser(
        prog="taken",
        description="Check whether a GitHub issue is already taken before you volunteer for it.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="owner/repo#123 or GitHub issue URLs (one or more).",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help="read targets from a file, one per line (blank lines and # comments ignored).",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help=(
            "max open issues to check per scan target, max candidates to return "
            "in --discover mode, or max issues listed in --health mode "
            "(default: 20; 100 in --health mode)."
        ),
    )
    parser.add_argument(
        "--label",
        metavar="LABEL",
        default=None,
        help="scan mode: only consider open issues carrying this label.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="discover top candidates via GitHub issue search, verified and ranked.",
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        default=None,
        help="discover: only consider repos in this language.",
    )
    parser.add_argument(
        "--min-contributors",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="discover: only consider repos with at least N contributors in the last 90 days.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=discover.DEFAULT_JOBS,
        metavar="N",
        help="discover: verify candidates with N parallel workers "
        f"(default: {discover.DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="discover: hide the progress bar.",
    )
    parser.add_argument(
        "--allocation",
        choices=["bandit", "recency"],
        default="bandit",
        help="discover: how verify budget is split across repos: 'bandit' "
        "spends it by Thompson sampling on each repo's observed GO yield "
        "(default), 'recency' verifies freshest first.",
    )
    parser.add_argument(
        "--explore-floor",
        type=_probability,
        default=0.15,
        metavar="P",
        help="discover: probability a bandit pick explores uniformly "
        "instead of following sampled yields (default: 0.15).",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="report maintainer-facing repo health for one owner/repo target "
        "(claims waiting, stale PRs, stale beginner labels, untriaged issues); "
        "read-only, no verdicts.",
    )
    parser.add_argument(
        "--claim-wait-days",
        type=_non_negative_int,
        default=health.DEFAULT_CLAIM_WAIT_DAYS,
        metavar="N",
        help="health: a claim with no maintainer reply counts as waiting after N days; "
        "a claim gone quiet after a maintainer reply counts after N days "
        f"(default: {health.DEFAULT_CLAIM_WAIT_DAYS}).",
    )
    parser.add_argument(
        "--pr-stale-days",
        type=_non_negative_int,
        default=health.DEFAULT_PR_STALE_DAYS,
        metavar="N",
        help="health: an open PR counts as stale after N days without activity "
        f"(default: {health.DEFAULT_PR_STALE_DAYS}).",
    )
    parser.add_argument(
        "--gfi-stale-days",
        type=_non_negative_int,
        default=health.DEFAULT_GFI_STALE_DAYS,
        metavar="N",
        help="health: a good first issue or hacktoberfest label counts as stale "
        f"after N days untouched (default: {health.DEFAULT_GFI_STALE_DAYS}).",
    )
    parser.add_argument("--json", action="store_true", help="print the full findings as JSON.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print an API usage summary (calls per endpoint, cache hits/misses) "
        "to stderr at the end of the run.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a machine-readable JSON debug report (wall-clock timings "
        "per phase, rate-limit state before/after, retries, backoff time, "
        "API usage) to stderr at the end of the run; implies --verbose. "
        "Only counts, timings, and sizes: never tokens or response bodies.",
    )
    parser.add_argument(
        "--me",
        metavar="LOGIN",
        default=None,
        help="your GitHub login; your own comments are ignored in the claimant scan.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the API response cache (~/.cache/taken, 1h TTL).",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the API response cache (~/.cache/taken) and exit.",
    )
    parser.add_argument(
        "--graphql",
        action="store_true",
        help="force the GraphQL fetch path even when not logged in to GitHub "
        "(falls back to REST on failure); the default for logged-in users.",
    )
    parser.add_argument(
        "--rest",
        action="store_true",
        help="force the REST fetch path even when logged in to GitHub; "
        "escape hatch for the GraphQL default (or TAKEN_REST=1).",
    )
    parser.add_argument(
        "--persistent-session",
        action="store_true",
        help="GraphQL over one persistent HTTPS connection for the process; "
        "token from `gh auth token` is held in memory only. Opt-in "
        "(or TAKEN_PERSISTENT_SESSION=1); --rest still wins.",
    )
    parser.add_argument(
        "--pr-idle-days",
        type=_non_negative_int,
        default=checks.DEFAULT_PR_IDLE_DAYS,
        metavar="N",
        help="stale-claim decay: an open linked PR with no activity for longer than N days "
        f"weakens from TAKEN to CAUTION (default: {checks.DEFAULT_PR_IDLE_DAYS}).",
    )
    parser.add_argument(
        "--claim-silence-days",
        type=_non_negative_int,
        default=checks.DEFAULT_CLAIM_SILENCE_DAYS,
        metavar="N",
        help="stale-claim decay: days one claim blocks as CAUTION on a simple issue; "
        "the clock resets on any claimant activity "
        f"(default: {checks.DEFAULT_CLAIM_SILENCE_DAYS}).",
    )
    parser.add_argument(
        "--claim-silence-complex-days",
        type=_non_negative_int,
        default=checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS,
        metavar="N",
        help="stale-claim decay: days one claim blocks as CAUTION on a complex issue "
        f"(default: {checks.DEFAULT_CLAIM_SILENCE_COMPLEX_DAYS}).",
    )
    parser.add_argument("--version", action="version", version=f"taken {__version__}")
    return parser
