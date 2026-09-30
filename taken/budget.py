"""Budget tiers: anonymous (lean) vs authenticated (rich).

Two tiers, not three. The tier is chosen once per process by
``activate()``, which the CLI and MCP entry points call at startup; until
then every engine entry point behaves as the anonymous tier, which is
exactly today's behavior. The docs console never activates, so it stays
lean by construction.

Anonymous: the 60-requests/hour unauthenticated budget. Caps are today's
values, so the anonymous path cannot regress.

Authenticated: a ``gh``-authenticated caller (5,000 core requests/hour).
Caps go deeper only where the extra spend is verdict-relevant (more
comment/timeline pages can surface a claimant or a linked PR that flips
a verdict) or relevance-relevant (a larger discover pool ranks more
candidates). Caps that cannot add verdict-relevant evidence stay put.

The tier only ever *raises* caps above the module baselines, never
lowers them: embedders may override baselines downward (the docs console
sets ``checks.MAX_SCAN_PAGES = 1`` for its 60/hr budget) and those
overrides keep working — see ``effective_cap``.

Accounting is local only: per-run request counters already kept by
``checks.record_api_call``. Nothing leaves the process: no telemetry, no
persistence beyond the run.

The transport-selection seam (``graphql.fetch_mode``) is separate and
untouched: GraphQL is a transport both tiers can use; the tier decides
how much to spend through it.
"""

import threading
from dataclasses import dataclass

TIER_ANONYMOUS = "anonymous"
TIER_AUTHENTICATED = "authenticated"

_ANONYMOUS_HOURLY_REQUESTS = 60
_AUTHENTICATED_HOURLY_REQUESTS = 5000


@dataclass(frozen=True)
class Budget:
    """Explicit per-tier caps. No magic numbers scattered through the engine."""

    tier: str
    hourly_requests: int
    # REST caps
    scan_pages: int  # _paged_list: issue comments + timeline
    repo_pulls_pages: int  # repo-health merged-PR scan
    repo_commits_pages: int  # repo-health contributor breadth
    # GraphQL caps (mirror the REST scan depths)
    gql_comment_pages: int
    gql_timeline_pages: int
    gql_history_pages: int
    gql_merge_pages: int
    gql_label_pages: int
    # Discover caps
    discover_pool: int  # candidates fully verified per run


_BUDGETS = {
    TIER_ANONYMOUS: Budget(
        tier=TIER_ANONYMOUS,
        hourly_requests=_ANONYMOUS_HOURLY_REQUESTS,
        scan_pages=5,
        repo_pulls_pages=2,
        repo_commits_pages=3,
        gql_comment_pages=5,
        gql_timeline_pages=5,
        gql_history_pages=3,
        gql_merge_pages=2,
        gql_label_pages=3,
        discover_pool=40,
    ),
    TIER_AUTHENTICATED: Budget(
        tier=TIER_AUTHENTICATED,
        hourly_requests=_AUTHENTICATED_HOURLY_REQUESTS,
        # A claimant past 500 comments or a linked PR past 500 timeline
        # events flips a verdict; on a 5,000/hr budget the extra pages
        # are cheap.
        scan_pages=10,
        # Pages are updated-desc, so deeper pulls pages only pay off on
        # very active repos; still verdict-relevant when they do.
        repo_pulls_pages=4,
        # Contributor breadth is documented as a lower bound; deeper is
        # a better bound.
        repo_commits_pages=5,
        gql_comment_pages=10,
        gql_timeline_pages=10,
        gql_history_pages=5,
        # The GraphQL merge loop already breaks on the health cutoff, so
        # the extra headroom is nearly free.
        gql_merge_pages=4,
        # 3 pages x 100 already covers 300 labels, beyond any realistic
        # issue: nothing to gain by raising.
        gql_label_pages=3,
        # More verified candidates = better-ranked results.
        discover_pool=80,
    ),
}

_UNSET = object()
_active_tier = None
_active_lock = threading.Lock()


def activate(identity=_UNSET):
    """Choose the budget tier once per process; sticky afterwards.

    With no argument the tier comes from the authenticated ``gh``
    identity probe (``checks._github_identity``, itself memoized):
    a known login selects the authenticated tier, an unknown one the
    anonymous tier. Pass an explicit login (or None) to choose
    directly; tests use this instead of the ambient environment.

    Safe default: before activation every engine entry point behaves
    as the anonymous tier, so library use without activation can only
    under-spend, never over-spend.
    """
    global _active_tier
    if _active_tier is not None:
        return _active_tier
    with _active_lock:
        if _active_tier is not None:
            return _active_tier
        if identity is _UNSET:
            from taken import checks

            identity = checks._github_identity()
        _active_tier = TIER_AUTHENTICATED if identity else TIER_ANONYMOUS
        return _active_tier


def reset():
    """Forget the activated tier. Tests only: engine code never calls this."""
    global _active_tier
    with _active_lock:
        _active_tier = None


def current_tier():
    """The active tier name, defaulting to anonymous before activation."""
    tier = _active_tier
    return tier if tier is not None else TIER_ANONYMOUS


def is_authenticated():
    """True only after activate() selected the authenticated tier."""
    return current_tier() == TIER_AUTHENTICATED


def current():
    """The active Budget; the anonymous one before activation."""
    return _BUDGETS[current_tier()]


def effective_cap(baseline, field):
    """The cap to enforce at a call site.

    ``baseline`` is the module constant the code used before budgets
    existed (which embedders may override downward, e.g. the docs
    console's ``checks.MAX_SCAN_PAGES = 1``); ``field`` names the
    Budget attribute holding the tier's cap. The anonymous tier always
    returns the baseline unchanged; the authenticated tier returns the
    higher of the two, so downward overrides keep working.
    """
    if is_authenticated():
        return max(baseline, getattr(current(), field))
    return baseline
