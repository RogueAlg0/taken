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


def maintainer_engaged(issue, comments):
    """Heuristic: someone other than the author, not a bot, commented.

    A maintainer reply is the strongest cheap signal that volunteering on
    the issue will get a response. Bots don't count.
    """
    author = issue.get("author")
    for comment in comments:
        login = (comment.get("user") or {}).get("login") or ""
        if login and login != author and not login.endswith("[bot]"):
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


def _verify_candidate(owner, repo, number, item, min_stars, me):
    """Run the full check on one candidate. Returns the ranked entry or None.

    Kept separate so the pool can be verified concurrently; each call only
    does idempotent GETs through the (thread-safe) cache.
    """
    try:
        findings = checks.run_checks(owner, repo, number, me=me)
    except checks.TakenError:
        return None  # fail-closed per issue; keep scanning the rest
    verdict, reasons = decide(findings)
    if verdict != GO:
        return None
    if (findings["repo_health"].get("stars") or 0) < min_stars:
        return None
    comments = checks.fetch_comments(owner, repo, number)
    engaged = maintainer_engaged(findings["issue"], comments)
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
    }


def discover(
    limit=10, language=None, label=None, min_stars=0, me=None, jobs=DEFAULT_JOBS, on_progress=None
):
    """Search, verify, and rank contribution candidates.

    Candidates are verified concurrently (jobs threads). on_progress, when
    given, is called as on_progress(done, total) from the calling thread as
    each candidate finishes, so callers can drive a progress bar.

    Returns a list of dicts sorted by score (desc), then recency (desc):
    target, score, why, verdict, reasons, findings, updated_at,
    friendly_labels (first-time-contributor labels on the issue),
    welcoming (repo-level signs contributions are welcome).
    """
    updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    labels = [label] if label else SEARCH_LABELS
    candidates = []
    seen = set()
    for lab in labels:
        query = build_query(lab, language=language, updated_after=updated_after)
        for item in checks.search_issues(query, per_page=SEARCH_PER_PAGE):
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
        if len(candidates) >= VERIFY_POOL:
            break

    total = len(candidates)
    if on_progress is not None:
        on_progress(0, total)
    ranked = []
    workers = max(1, jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_verify_candidate, owner, repo, number, item, min_stars, me): (
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
            entry = future.result()
            if entry is not None:
                ranked.append(entry)
    # Score desc, then recency desc: the freshest candidate wins ties.
    # (A single sort; the old double-sort accidentally left equal scores
    # oldest-first because the second stable sort preserved the first.)
    ranked.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)
    return ranked[:limit]
