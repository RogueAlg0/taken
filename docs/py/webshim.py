"""Web (Pyodide) driver for taken's real check pipeline.

This runs taken's ACTUAL checks.py / verdict.py in the visitor's browser.
The only thing swapped out is the transport: the real CLI shells out to
the `gh` binary, which cannot exist in a browser, so gh_api() is replaced
with a synchronous XMLHttpRequest against api.github.com. Every check,
every heuristic, every verdict rule is the real code.

Notes for maintainers:
- checks.py / verdict.py in this directory are byte-copies of main at the
  time of the Pyodide-console PR. Re-copy them when the pipeline changes.
- MAX_SCAN_PAGES is capped at 1 here to respect GitHub's unauthenticated
  budget (60 req/hour per visitor). The CLI scans deeper.
- The file cache is disabled; there is no persistent disk in the page.
"""

import json
import re
from urllib.parse import urlencode

import checks
from verdict import decide

URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")
SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")
PATH_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")


def _http_get(url):
    """Synchronous GET via XHR. Returns (status, text).

    Synchronous XHR is deprecated but still works in every major browser,
    and it is the only way to keep taken's synchronous pipeline 100%
    unmodified in Pyodide.
    """
    from js import XMLHttpRequest

    xhr = XMLHttpRequest.new()
    xhr.open("GET", url, False)
    xhr.setRequestHeader("Accept", "application/vnd.github+json")
    xhr.send()
    return xhr.status, xhr.responseText


def _web_gh_api(endpoint, params=None):
    url = "https://api.github.com/" + endpoint.lstrip("/")
    if params:
        url += "?" + urlencode(params)
    status, text = _http_get(url)
    if status == 200:
        return json.loads(text)
    if status == 404:
        raise checks.NotFoundError(f"not found: {endpoint}")
    if status == 403:
        raise checks.TakenError(
            "GitHub API rate limit reached (60/hour for visitors without login). "
            "Try again later."
        )
    raise checks.TakenError(f"GitHub API returned HTTP {status} for {endpoint}")


checks.gh_api = _web_gh_api
checks._CACHE_ENABLED = False
checks.MAX_SCAN_PAGES = 1


def format_human(findings, verdict, reasons):
    """Same human output as the CLI (copied from taken/cli.py)."""
    issue = findings["issue"]
    health = findings["repo_health"]
    policy = findings["ai_policy"]
    lines = [
        f"taken? {findings['target']}",
        f"verdict: {verdict}",
        "",
        f"  issue: {issue['state']}, \"{issue['title']}\"",
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
            lines.append(f"  linked PR: #{pr['number']} \"{pr['title']}\" ({status})")
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
                f"  claimant: {hit['author']} on {hit['date']} (matched \"{hit['pattern']}\")"
            )
            lines.append(f"            \"{hit['snippet']}\"")
    else:
        lines.append("  claimants: none found in comments")
    if policy["source"]:
        lines.append(f"  AI policy: {policy['verdict']} ({policy['source']})")
        if policy["snippet"]:
            lines.append(f"             \"{policy['snippet']}\"")
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


HELP = """usage: taken [owner/repo#123 | issue URL] [--me login]

This console runs taken's real Python code in your browser (Pyodide),
doing live checks against GitHub's public API. No login, nothing installed.

  taken owner/repo#123
  taken https://github.com/owner/repo/issues/123
  taken owner/repo#123 --me mylogin   (ignore your own comments)

offline (no API calls):
  taken --discover --limit 3   (sample output, not live)
  taken --version
  clear"""

DISCOVER_SAMPLE = """taken? --discover
offline sample from a real run, not a live check.

      6  FasterXML/jackson-datatypes-collections#2
         maintainer replied; updated 0d ago; repo pushed 1d ago
      6  padok-team/burrito#42
         maintainer replied; updated 0d ago; repo pushed 1d ago
      6  stefankueng/grepWin#618
         maintainer replied; updated 0d ago; repo pushed 0d ago

  3 GO candidates. Only GO verdicts are ranked."""


def _parse_target(text):
    text = text.strip()
    for pattern in (URL_RE, SHORT_RE, PATH_RE):
        match = pattern.match(text)
        if match:
            owner, repo, number = match.groups()
            return ("issue", owner, repo, int(number))
    return None


def run_command(line):
    """Run one console line; return the text to print."""
    line = line.strip()
    if not line:
        return ""
    low = line.lower()
    if low in ("taken --help", "help"):
        return HELP
    if low == "taken --version":
        return "taken 0.5.0 (Pyodide build: taken's real Python code, running in your browser)"
    if low.startswith("taken --discover"):
        return DISCOVER_SAMPLE
    m = re.match(r"^taken\s+(.+)$", line, re.I)
    if not m:
        return 'unknown command. Try "taken --help".'
    rest = m.group(1).strip()
    me = None
    me_match = re.search(r"--me\s+(\S+)", rest, re.I)
    if me_match:
        me = me_match.group(1)
        rest = re.sub(r"--me\s+\S+", "", rest, flags=re.I).strip()
    target = _parse_target(rest)
    if not target:
        return "need an issue target: taken owner/repo#123"
    _, owner, repo, number = target
    try:
        findings = checks.run_checks(owner, repo, number, me=me)
    except checks.TakenError as e:
        return f"error: {e}"
    verdict, reasons = decide(findings)
    return format_human(findings, verdict, reasons)
