"""Candidate discovery for taken.

Piggybacks on GitHub's issue search API, the same source the web aggregators
use, for raw candidates. What the aggregators don't do (and we do): run
taken's full verification on every candidate and rank by maintainer
responsiveness, the signal that best predicts whether volunteering will go
anywhere. No aggregator filters on that.
"""

import concurrent.futures
from datetime import datetime, timedelta, timezone

from . import checks
from .verdict import GO, decide

SEARCH_LABELS = ["good first issue", "good-first-issue", "beginner friendly", "help wanted"]
SEARCH_PER_PAGE = 50
VERIFY_POOL = 40
DEFAULT_JOBS = 8

# author_association values that mean the commenter can speak for the repo.
# A random "+1" from a passerby (NONE/CONTRIBUTOR/...) is not maintainer
# engagement and must not earn the +3 "maintainer replied" points.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def build_query(label, language=None, updated_after=None):
    parts = ["is:open", "is:issue", "no:assignee", f'label:"{label}"']
    if updated_after:
        parts.append(f"updated:>={updated_after}")
    if language:
        parts.append(f"language:{language}")
    return " ".join(parts)


def repo_of(search_item):
    """Extract (owner, repo) from a search result's repository_url."""
    url = (search_item.get("repository_url") or "").rstrip("/").split("/")
    if len(url) < 2:
        return None
    return url[-2], url[-1]


def maintainer_engaged(issue, comments, me=None):
    """Heuristic: a maintainer, not the author, you, or a bot, commented.

    A maintainer reply is the strongest cheap signal that volunteering on
    the issue will get a response. Only OWNER/MEMBER/COLLABORATOR
    author_associations count; bots and the issue author never do, and
    neither does your own login (see --me).
    """
    author = issue.get("author")
    for comment in comments:
        login = (comment.get("user") or {}).get("login") or ""
        if not login or login == author or login == me or login.endswith("[bot]"):
            continue
        if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
            return True
    return False


def _days_ago(iso_ts):
    try:
        dt = datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def score_candidate(findings, updated_at, engaged):
    """Explainable score. Returns (points, [reasons])."""
    points = 0
    why = []
    if engaged:
        points += 3
        why.append("maintainer replied")
    age_days = _days_ago(updated_at)
    if age_days is not None and age_days <= 7:
        points += 2
        why.append(f"updated {age_days}d ago")
    push_days = _days_ago(findings["repo_health"].get("pushed_at") or "")
    if push_days is not None and push_days <= 7:
        points += 1
        why.append(f"repo pushed {push_days}d ago")
    if not why:
        why.append("passed verification")
    return points, why


def _verify_candidate(owner, repo, number, item, min_contributors, me):
    """Run the full check on one candidate.

    Kept separate so the pool can be verified concurrently; each call only
    does idempotent GETs through the (thread-safe) cache.

    Returns (entry, error): the ranked entry (or None when the candidate
    was filtered by a real verdict), and the TakenError when verification
    itself failed (or None). Callers use the error to tell "nothing is
    available" apart from "the tool is broken".
    """
    try:
        findings = checks.run_checks(owner, repo, number, me=me)
    except checks.TakenError as exc:
        return None, exc  # fail-closed per issue; keep scanning the rest
    verdict, reasons = decide(findings)
    if verdict != GO:
        return None, None
    if (findings["repo_health"].get("contributors") or 0) < min_contributors:
        return None, None
    comments = checks.fetch_comments(owner, repo, number)
    engaged = maintainer_engaged(findings["issue"], comments, me=me)
    points, why = score_candidate(findings, item.get("updated_at"), engaged)
    return {
        "target": f"{owner}/{repo}#{number}",
        "score": points,
        "why": why,
        "verdict": verdict,
        "reasons": reasons,
        "findings": findings,
        "updated_at": item.get("updated_at") or "",
        "friendly_labels": checks.friendly_labels(findings),
        "welcoming": checks.welcoming_signals(findings),
    }, None


class DiscoverResults(list):
    """Ranked candidates plus verification stats.

    errors: candidates that failed with TakenError instead of a verdict.
    total: candidates that entered the verify pool.
    """

    def __init__(self, items=(), *, errors=0, total=0):
        super().__init__(items)
        self.errors = errors
        self.total = total


def _collect_candidates(labels, language, updated_after):
    """Search every label and interleave the results into one deduped pool.

    The old loop broke out as soon as the first label filled VERIFY_POOL,
    so the remaining labels were never searched. Round-robin across the
    per-label result lists guarantees every label contributes.

    Returns (candidates, searched): the pool capped at VERIFY_POOL, and a
    [(label, items_returned)] list so callers can report what was searched.
    """
    per_label = []
    for lab in labels:
        query = build_query(lab, language=language, updated_after=updated_after)
        items = checks.search_issues(query, per_page=SEARCH_PER_PAGE)
        per_label.append((lab, items))
    candidates = []
    seen = set()
    index = 0
    while len(candidates) < VERIFY_POOL:
        progressed = False
        for _lab, items in per_label:
            if index >= len(items):
                continue
            progressed = True
            item = items[index]
            where = repo_of(item)
            if not where:
                continue
            owner, repo = where
            key = (owner, repo, item.get("number"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((owner, repo, item.get("number"), item))
            if len(candidates) >= VERIFY_POOL:
                break
        if not progressed:
            break
        index += 1
    searched = [(lab, len(items)) for lab, items in per_label]
    return candidates, searched


def discover(
    limit=10,
    language=None,
    label=None,
    min_contributors=0,
    me=None,
    jobs=DEFAULT_JOBS,
    on_progress=None,
    on_searched=None,
):
    """Search, verify, and rank contribution candidates.

    Candidates are verified concurrently (jobs threads). on_progress, when
    given, is called as on_progress(done, total) from the calling thread as
    each candidate finishes, so callers can drive a progress bar.
    on_searched, when given, is called as on_searched([(label, count), ...])
    after the search phase, so callers can report what was searched.

    Returns a DiscoverResults (a list of dicts sorted by score (desc),
    then recency (desc)) with .errors / .total stats, so callers can tell
    "no GO candidates" apart from "verification kept failing":
    target, score, why, verdict, reasons, findings, updated_at,
    friendly_labels (first-time-contributor labels on the issue),
    welcoming (repo-level signs contributions are welcome).
    """
    updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    labels = [label] if label else SEARCH_LABELS
    candidates, searched = _collect_candidates(labels, language, updated_after)
    if on_searched is not None:
        on_searched(searched)

    total = len(candidates)
    if on_progress is not None:
        on_progress(0, total)
    ranked = []
    errors = 0
    workers = max(1, jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_verify_candidate, owner, repo, number, item, min_contributors, me): (
                owner,
                repo,
                number,
            )
            for owner, repo, number, item in candidates
        }
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            entry, error = future.result()
            if error is not None:
                errors += 1
            elif entry is not None:
                ranked.append(entry)
    # Score desc, then recency desc: the freshest candidate wins ties.
    # (A single sort; the old double-sort accidentally left equal scores
    # oldest-first because the second stable sort preserved the first.)
    ranked.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)
    return DiscoverResults(ranked[:limit], errors=errors, total=total)
