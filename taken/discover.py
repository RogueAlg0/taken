"""Candidate discovery for taken.

Piggybacks on GitHub's issue search API, the same source the web aggregators
use, for raw candidates. What the aggregators don't do (and we do): run
taken's full verification on every candidate and rank by maintainer
responsiveness, the signal that best predicts whether volunteering will go
anywhere. No aggregator filters on that.
"""

import concurrent.futures
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from . import budget, checks, graphql
from .verdict import GO, decide

SEARCH_LABELS = ["good first issue", "good-first-issue", "beginner friendly", "help wanted"]
SEARCH_PER_PAGE = 50
# Baseline verify-pool size; the authenticated budget tier raises it
# (see taken/budget.py).
VERIFY_POOL = 40
DEFAULT_JOBS = 8


def _verify_pool_size():
    """Candidates fully verified per run: baseline, raised when logged in."""
    return budget.effective_cap(VERIFY_POOL, "discover_pool")


# author_association values that mean the commenter can speak for the repo.
# A random "+1" from a passerby (NONE/CONTRIBUTOR/...) is not maintainer
# engagement and must not earn the +3 "maintainer replied" points.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_thread_state = threading.local()


def _thread_graphql_session():
    """One persistent GraphQL session per verify-pool thread.

    graphql.get_session() shares a single keep-alive connection, which is
    not safe to use from the pool's worker threads; each thread keeps its
    own session (and its own connection) instead. The token is still read
    once per thread from `gh auth token` and held in memory only.
    """
    session = getattr(_thread_state, "graphql_session", None)
    if session is None:
        session = graphql.PersistentGraphQLSession()
        _thread_state.graphql_session = session
    return session


def build_query(labels, language=None, updated_after=None):
    """Build a search/issues query matching any of the given labels.

    Accepts a single label string or a list. Multiple labels are OR'd with
    comma-separated values inside one label: qualifier, so one search call
    covers every label instead of one call per label (GitHub's secondary
    rate limits throttle burst velocity, not budget, and the old per-label
    loop died 12.5s into a cold run with zero verdicts).
    """
    if isinstance(labels, str):
        labels = [labels]
    label_part = ",".join(f'"{lab}"' for lab in labels)
    parts = ["is:open", "is:issue", "no:assignee", f"label:{label_part}"]
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
    me_lower = (me or "").lower()
    for comment in comments:
        login = (comment.get("user") or {}).get("login") or ""
        if not login or login == author or login.endswith("[bot]"):
            continue
        # GitHub logins are case-insensitive: "RogueAlg0" is me even when
        # --me was passed as "roguealg0". Matches the claimant-scan
        # convention in checks.py.
        if me_lower and login.lower() == me_lower:
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


def _score_ceiling(updated_at):
    """Max score a candidate can still reach, from its known updated_at.

    score_candidate awards at most 3 (maintainer reply) + 2 (updated within
    7 days) + 1 (repo pushed within 7 days). The +2 recency points are gone
    for candidates updated more than 7 days ago. An unparseable timestamp
    is conservatively treated as fresh (ceiling 6): never assume stale.
    """
    age_days = _days_ago(updated_at)
    return 6 if age_days is None or age_days <= 7 else 4


def _verify_candidate(
    owner, repo, number, item, min_contributors, me, mode="rest", thresholds=None
):
    """Run the full check on one candidate.

    Kept separate so the pool can be verified concurrently; each call only
    does idempotent GETs through the (thread-safe) cache. `mode` selects
    the fetch path: "rest" (default), "graphql" (one query per issue via
    `gh api graphql`), or "persistent" (GraphQL over a per-thread
    keep-alive session).

    `thresholds` carries the stale-claim decay settings (issue #83);
    None means the defaults.

    Returns (entry, error): the ranked entry (or None when the candidate
    was filtered by a real verdict), and the TakenError when verification
    itself failed (or None). Callers use the error to tell "nothing is
    available" apart from "the tool is broken".
    """
    try:
        if mode in ("graphql", "persistent"):
            session = _thread_graphql_session() if mode == "persistent" else None
            # The wrapper falls back to REST per candidate when the GraphQL
            # transport fails, and records the fallback in the findings.
            findings = graphql.run_checks_with_fallback(
                owner, repo, number, me=me, mode=mode, session=session, thresholds=thresholds
            )
        else:
            # The search item already carries every field check_issue()
            # needs, so the per-issue GET is skipped (issue #153): one
            # fewer API call per candidate, up to VERIFY_POOL per run.
            findings = checks.run_checks(
                owner, repo, number, me=me, payload=item, thresholds=thresholds
            )
    except checks.TakenError as exc:
        return None, exc  # fail-closed per issue; keep scanning the rest
    verdict, reasons = decide(findings)
    if verdict != GO:
        return None, None
    if (findings["repo_health"].get("contributors") or 0) < min_contributors:
        return None, None
    try:
        # The verdict above already reflects the verified evidence; this
        # second fetch only scores maintainer engagement, so a truncated
        # page cap here is not a verdict risk.
        comments, _ = checks.fetch_comments(owner, repo, number)
    except checks.TakenError as exc:
        return None, exc  # one bad comments fetch must not abort the run
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
    verified: candidates whose verification was submitted to the pool
        (<= total; strictly lower when the #214 early stop fires before
        the queue drains). Counts submitted work, not consumed
        completions, so it measures the API spend the stop is meant to
        save.
    search_errors: [(label, error)] for label searches that failed during
    the per-label fallback; empty when the combined search succeeded, so
    callers can tell "partial results" apart from "everything worked".
    """

    def __init__(self, items=(), *, errors=0, total=0, verified=0, search_errors=()):
        super().__init__(items)
        self.errors = errors
        self.total = total
        self.verified = verified
        self.search_errors = list(search_errors)


def _pool_from_items(items, seen, limit):
    """Fill the verify pool from search items, freshest first.

    Items are deduplicated by (owner, repo, number) as a safety net; seen
    is shared so the per-label fallback cannot re-add an issue found under
    an earlier label. Adds at most `limit` candidates.
    """
    candidates = []
    for item in items:
        if len(candidates) >= limit:
            break
        where = repo_of(item)
        if not where:
            continue
        owner, repo = where
        key = (owner, repo, item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        candidates.append((owner, repo, item.get("number"), item))
    return candidates


def _collect_candidates(labels, language, updated_after):
    """Search once with all labels OR'd and fill the verify pool.

    A single search/issues call covers every label, so a cold discover run
    no longer fires a burst of back-to-back search calls into GitHub's
    secondary rate limit. Results arrive sorted by recency (see
    checks.search_issues), so the pool fills with the freshest candidates
    first.

    When the combined search raises (rate limit, transient failure), fall
    back to one paced search per label so the run degrades to partial
    results instead of dying with zero candidates (issue #162). When every
    label fails too, the first error is re-raised: total failure stays an
    error, never a silent empty success.

    Returns (candidates, searched, search_errors): the pool capped at
    VERIFY_POOL; a [(labels, count)] list so callers can report what was
    searched (one entry for the combined query, one per label on the
    fallback path); and a [(label, error)] list for labels whose fallback
    search failed, empty on the normal path.
    """
    query = build_query(labels, language=language, updated_after=updated_after)
    try:
        items = checks.search_issues(query, per_page=SEARCH_PER_PAGE)
    except checks.TakenError:
        return _collect_candidates_per_label(labels, language, updated_after)
    candidates = _pool_from_items(items, set(), _verify_pool_size())
    searched = [(", ".join(labels), len(items))]
    return candidates, searched, []


def _collect_candidates_per_label(labels, language, updated_after):
    """Fallback: one search per label when the combined query fails.

    Keeps the candidates from the labels that succeeded and records which
    label failed and why. Each search goes through the same pacing as the
    normal path, so the fallback cannot burst into the secondary rate
    limit it is trying to recover from.
    """
    candidates = []
    searched = []
    search_errors = []
    seen = set()
    for label in labels:
        query = build_query(label, language=language, updated_after=updated_after)
        try:
            items = checks.search_issues(query, per_page=SEARCH_PER_PAGE)
        except checks.TakenError as exc:
            search_errors.append((label, str(exc)))
            continue
        searched.append((label, len(items)))
        candidates.extend(_pool_from_items(items, seen, _verify_pool_size() - len(candidates)))
        if len(candidates) >= _verify_pool_size():
            break
    if not candidates and search_errors and not searched:
        # Every label failed: re-raise instead of returning an empty
        # success that looks like "no candidates found".
        raise checks.TakenError(search_errors[0][1])
    return candidates, searched, search_errors


def discover(
    limit=10,
    language=None,
    label=None,
    min_contributors=0,
    me=None,
    jobs=DEFAULT_JOBS,
    on_progress=None,
    on_searched=None,
    mode="rest",
    thresholds=None,
):
    """Search, verify, and rank contribution candidates.

    Candidates are verified concurrently (jobs threads). on_progress, when
    given, is called as on_progress(done, total) from the calling thread as
    each candidate finishes, so callers can drive a progress bar.
    on_searched, when given, is called as on_searched([(labels, count)])
    after the search phase, so callers can report what was searched: one
    entry carrying the comma-joined labels and the single query's result
    count.
    mode selects the verification fetch path: "rest" (default), "graphql",
    or "persistent" (see graphql.fetch_mode).
    thresholds carries the stale-claim decay settings (issue #83); None
    means the defaults (issue #238: the CLI and MCP wrappers pass the
    caller's settings through instead of silently dropping them).

    Returns a DiscoverResults (a list of dicts sorted by score (desc),
    then recency (desc)) with .errors / .total stats, so callers can tell
    "no GO candidates" apart from "verification kept failing", plus
    .search_errors [(label, error)] when the search phase fell back to
    per-label queries and some of them failed: target, score, why, verdict,
    reasons, findings, updated_at, friendly_labels (first-time-contributor
    labels on the issue), welcoming (repo-level signs contributions are
    welcome).

    Verification stops early (issue #214) once the top-`limit` ranking is
    provably decided: when `limit` banked GO candidates all score strictly
    above the highest score any not-yet-banked candidate can still reach
    (from its known updated_at), the remaining pool cannot change the
    output. Submission is rolling and bounded: at most `jobs` candidates
    are ever in flight, and the stop proof is evaluated after every
    completion before replacement work is submitted, so no candidate is
    submitted once the ranking is decided. .verified reports how many
    candidates were actually submitted, so callers can distinguish a pool
    of 80 verified in full from one cut short at 23.
    """
    # A negative limit is meaningless; clamp to 0 (empty result) instead of
    # letting ranked[:limit] silently drop the top candidates. This also
    # covers the MCP discover_candidates path, which bypasses argparse.
    limit = max(0, limit)
    updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    labels = [label] if label else SEARCH_LABELS
    candidates, searched, search_errors = _collect_candidates(labels, language, updated_after)
    if on_searched is not None:
        on_searched(searched)

    total = len(candidates)
    if on_progress is not None:
        on_progress(0, total)
    if limit == 0:
        # Nothing can make the cut; skip verification entirely.
        return DiscoverResults([], errors=0, total=total, verified=0, search_errors=search_errors)
    ranked = []
    errors = 0
    verified = 0
    banked_scores = []
    workers = max(1, jobs)
    # Rolling submission (issue #214): at most `workers` candidates are in
    # flight at any time. The stop proof is evaluated after every
    # completion and BEFORE replacement work is submitted, so no candidate
    # is ever submitted once the top-`limit` ranking is decided. (Eagerly
    # submitting the whole pool up front would let fast workers start
    # every verification before the proof can fire, doing all the API work
    # the stop exists to save.) Ceilings cover every candidate that has
    # not banked a score yet: queued and in-flight alike.
    ceilings = {
        idx: _score_ceiling(item.get("updated_at"))
        for idx, (_, _, _, item) in enumerate(candidates)
    }
    queue = deque(range(len(candidates)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        in_flight = {}

        def submit_next():
            nonlocal verified
            idx = queue.popleft()
            owner, repo, number, item = candidates[idx]
            future = pool.submit(
                _verify_candidate,
                owner,
                repo,
                number,
                item,
                min_contributors,
                me,
                mode,
                thresholds=thresholds,
            )
            in_flight[future] = idx
            # .verified counts submitted verification work, not consumed
            # completions: one submission is one _verify_candidate run.
            # Nothing is ever cancelled, so every submission runs.
            verified += 1

        def ranking_decided():
            remaining_ceiling = max(ceilings.values(), default=-1)
            return sum(1 for s in banked_scores if s > remaining_ceiling) >= limit

        while queue and len(in_flight) < workers:
            submit_next()
        done = 0
        stopped = False
        while in_flight and not stopped:
            finished, _ = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                idx = in_flight.pop(future)
                del ceilings[idx]
                entry, error = future.result()
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
                if error is not None:
                    errors += 1
                elif entry is not None:
                    ranked.append(entry)
                    banked_scores.append(entry["score"])
                if ranking_decided():
                    # The top-`limit` ranking is decided: no remaining
                    # candidate can displace the banked top-`limit`, so
                    # the rest of the queue is never submitted. In-flight
                    # futures finish during executor shutdown; their
                    # results are discarded (a bounded overrun of at most
                    # `workers - 1` extra verifications).
                    stopped = True
                    break
            if not stopped:
                while queue and len(in_flight) < workers:
                    submit_next()
    # Score desc, then recency desc: the freshest candidate wins ties.
    # (A single sort; the old double-sort accidentally left equal scores
    # oldest-first because the second stable sort preserved the first.)
    ranked.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)
    return DiscoverResults(
        ranked[:limit],
        errors=errors,
        total=total,
        verified=verified,
        search_errors=search_errors,
    )
