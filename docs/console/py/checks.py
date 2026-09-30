"""Read-only GitHub checks used by taken.

All access goes through the `gh` CLI, so the tool uses the invoker's own
authentication and never sees, stores, or handles any token.
"""

import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from taken import budget
from taken.verdict import FIRST_TIME_LABELS, TAKEN, decide

API_TIMEOUT = 60
HEALTH_WINDOW_DAYS = 30
CONTRIBUTORS_WINDOW_DAYS = 90
CACHE_TTL_SECONDS = 3600

# Endpoints become positional arguments to the `gh api` subprocess, so keep
# them to a safe alphabet: no leading dash (which `gh` would parse as a
# flag), no whitespace or control characters. Every endpoint the codebase
# builds (repos/..., search/..., graphql, contents/...) fits this shape.
_ENDPOINT_SAFE_RE = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_./-]*")


def _require_safe_endpoint(endpoint):
    """Reject an endpoint string that could not be a plain API path."""
    if not isinstance(endpoint, str) or not _ENDPOINT_SAFE_RE.fullmatch(endpoint):
        raise TakenError(f"refusing to call unsafe API endpoint: {endpoint!r}")


# Timeline and comment scans page through the API instead of trusting the
# first 100 results: on a busy issue a linked PR or a claimant comment can
# hide on a later page, which would silently flip a verdict to GO.
# This is the baseline cap; the authenticated budget tier may raise it
# (see taken/budget.py), and embedders may lower it (the docs console
# sets it to 1 for its 60/hr budget).
MAX_SCAN_PAGES = 5

# Repo-health scan depths: merged-PR pages and commit pages per repo.
# Baselines; the authenticated budget tier may raise them.
_REPO_PULLS_PAGES = 2
_REPO_COMMITS_PAGES = 3

# Set to False (via --no-cache) to bypass the response cache.
_CACHE_ENABLED = True

# API usage stats for --verbose: per-endpoint subprocess call counts plus
# cache hits/misses. Recorded in gh_api (REST) and graphql._cached_or_fetch,
# the two funnels every GitHub request passes through. Guarded by a lock
# because --discover verifies candidates from a thread pool.
#
# --debug extends this with timing and cost stats: response bytes, retry
# counts, backoff sleep time, and per-phase timings ("cache", "rest",
# "graphql"). Only counts, timings, and sizes are recorded: never headers,
# tokens, or response bodies.
_API_STATS: dict[str, Any] = {
    "calls": {},
    "cache_hits": 0,
    "cache_misses": 0,
    "bytes_total": 0,
    "retries": 0,
    "backoff_seconds": 0.0,
    "phases": {},
}
_API_STATS_LOCK = threading.Lock()


def reset_api_stats() -> None:
    """Zero the --verbose/--debug API usage counters (called once per CLI run)."""
    with _API_STATS_LOCK:
        _API_STATS["calls"].clear()
        _API_STATS["cache_hits"] = 0
        _API_STATS["cache_misses"] = 0
        _API_STATS["bytes_total"] = 0
        _API_STATS["retries"] = 0
        _API_STATS["backoff_seconds"] = 0.0
        _API_STATS["phases"].clear()


def record_api_call(endpoint: str) -> None:
    """Count one real `gh api` subprocess call against an endpoint."""
    with _API_STATS_LOCK:
        calls = _API_STATS["calls"]
        calls[endpoint] = calls.get(endpoint, 0) + 1


def record_cache_result(hit: bool) -> None:
    """Count one cache lookup as a hit or a miss."""
    with _API_STATS_LOCK:
        if hit:
            _API_STATS["cache_hits"] += 1
        else:
            _API_STATS["cache_misses"] += 1


def record_bytes(count: int) -> None:
    """Add response body bytes to the --debug total (sizes only, never bodies)."""
    with _API_STATS_LOCK:
        _API_STATS["bytes_total"] += max(0, count)


def record_retry(backoff_seconds: float) -> None:
    """Count one backoff retry and accumulate the seconds slept for it."""
    with _API_STATS_LOCK:
        _API_STATS["retries"] += 1
        _API_STATS["backoff_seconds"] += max(0.0, backoff_seconds)


def record_phase(name: str, seconds: float) -> None:
    """Accumulate wall-clock seconds spent in a phase ("cache"/"rest"/"graphql").

    Sums across threads: with --discover's worker pool the total can exceed
    wall-clock time, which is the point (it measures API time, not elapsed).
    """
    with _API_STATS_LOCK:
        phases = _API_STATS["phases"]
        phases[name] = phases.get(name, 0.0) + max(0.0, seconds)


def api_stats_data() -> dict[str, Any]:
    """A lock-protected copy of the raw stats for the --debug JSON report."""
    with _API_STATS_LOCK:
        return {
            "calls": dict(_API_STATS["calls"]),
            "cache_hits": _API_STATS["cache_hits"],
            "cache_misses": _API_STATS["cache_misses"],
            "bytes_total": _API_STATS["bytes_total"],
            "retries": _API_STATS["retries"],
            "backoff_seconds": _API_STATS["backoff_seconds"],
            "phases": dict(_API_STATS["phases"]),
        }


def api_stats_summary() -> str:
    """One short --verbose report: tier, totals, per-endpoint call counts."""
    b = budget.current()
    with _API_STATS_LOCK:
        calls = dict(_API_STATS["calls"])
        hits = _API_STATS["cache_hits"]
        misses = _API_STATS["cache_misses"]
    total = sum(calls.values())
    lines = [
        f"Budget tier: {b.tier} ({b.hourly_requests:,} requests/hour); {total} used this run",
        f"API usage: {total} call{'s' if total != 1 else ''}, "
        f"{hits} cache hit{'s' if hits != 1 else ''}, "
        f"{misses} cache miss{'es' if misses != 1 else ''}",
    ]
    for endpoint in sorted(calls):
        count = calls[endpoint]
        lines.append(f"  {endpoint}: {count} call{'s' if count != 1 else ''}")
    return "\n".join(lines)


def budget_report() -> dict[str, Any]:
    """JSON-serializable per-run budget accounting for --json envelopes.

    Local only: built from the in-process request counters. Nothing
    leaves the process.
    """
    b = budget.current()
    return {
        "tier": b.tier,
        "hourly_budget": b.hourly_requests,
        "requests_used": sum(api_stats_data()["calls"].values()),
    }


def budget_line() -> str:
    """One concise stderr line: tier and requests used this run."""
    report = budget_report()
    return (
        f"budget: {report['tier']} tier, "
        f"{report['requests_used']} request{'s' if report['requests_used'] != 1 else ''} used "
        f"({report['hourly_budget']:,}/hour)"
    )


def _rate_limit_epoch_to_iso(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def rate_limit_snapshot() -> dict[str, Any] | None:
    """Best-effort GitHub rate-limit state for the --debug report.

    Runs `gh api rate_limit` outside the response cache (caching would freeze
    the numbers) but records the call under the "rate_limit" endpoint so the
    totals stay honest. Never raises: returns None when unavailable.
    """
    try:
        start = time.perf_counter()
        record_api_call("rate_limit")
        proc = subprocess.run(
            ["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=API_TIMEOUT
        )
        record_phase("rest", time.perf_counter() - start)
        if proc.returncode != 0:
            return None
        record_bytes(len((proc.stdout or "").encode("utf-8")))
        resources = (json.loads(proc.stdout or "{}") or {}).get("resources") or {}
        core = resources.get("core") or {}
        search = resources.get("search") or {}
        return {
            "core_remaining": core.get("remaining"),
            "core_limit": core.get("limit"),
            "core_reset": _rate_limit_epoch_to_iso(core.get("reset")),
            "search_remaining": search.get("remaining"),
            "search_limit": search.get("limit"),
            "search_reset": _rate_limit_epoch_to_iso(search.get("reset")),
        }
    except Exception:
        return None


def debug_report(
    total_seconds: float, rate_start: dict[str, Any] | None, rate_end: dict[str, Any] | None
) -> str:
    """Machine-readable --debug report: timings, cost, retries, rate limits.

    Safe by construction: only counts, timings, sizes, and rate-limit
    numbers. No request/response bodies, headers, or tokens ever pass
    through these counters.
    """
    data = api_stats_data()
    report = {
        "total_seconds": round(total_seconds, 3),
        "phases": {name: round(secs, 3) for name, secs in sorted(data["phases"].items())},
        "api_calls": sum(data["calls"].values()),
        "cache_hits": data["cache_hits"],
        "cache_misses": data["cache_misses"],
        "endpoints": dict(sorted(data["calls"].items())),
        "bytes_total": data["bytes_total"],
        "retries": data["retries"],
        "backoff_seconds": round(data["backoff_seconds"], 3),
        "rate_limit": {"start": rate_start, "end": rate_end},
    }
    return json.dumps(report, indent=2)


# In-process cache in front of the file cache: within one run, repeated
# reads of the same key (e.g. repo health for several issues in one repo)
# never touch disk at all.
_MEM_CACHE: dict[str, dict[str, Any]] = {}
_MEM_LOCK = threading.Lock()

PR_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")

CLAIMANT_PATTERNS = [
    "assign me",
    "can i work on",
    "i'd like to take",
    "i would like to work",
    "please assign",
    "working on this",
    "i'll take this",
    "i will take",
    "i'd love to take",
    "i'd love to work on",
    "could you assign",
    "assign this issue to me",
]

BAN_PHRASES = [
    "does not accept ai",
    "do not accept ai",
    "will not accept ai",
    "no ai-generated",
]

DISCLOSURE_PHRASES = [
    "assisted-by",
    "ai-assisted",
    "disclose",
    "generative ai",
]

CONTRIBUTING_PATHS = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
]

# FIRST_TIME_LABELS lives in taken/verdict.py so decide() can use it without
# a circular import; it is imported at the top of this module.


def friendly_labels(findings):
    """Issue labels marking it as first-time-contributor friendly.

    Returns the matching label names in their original casing.
    """
    labels = (findings.get("issue") or {}).get("labels") or []
    return [label for label in labels if label.lower() in FIRST_TIME_LABELS]


def welcoming_signals(findings):
    """Repo-level signs that outside contributions are welcome.

    Cheap: both signals come from data the check suite already fetches,
    so this adds no extra API calls.
    """
    signals = []
    source = (findings.get("ai_policy") or {}).get("source")
    if source:
        signals.append(f"has {source.split('/')[-1]}")
    merges = (findings.get("repo_health") or {}).get("recent_merges") or 0
    if merges:
        noun = "PR" if merges == 1 else "PRs"
        signals.append(f"{merges} {noun} merged recently")
    return signals


class TakenError(Exception):
    """Something went wrong talking to GitHub."""


class NotFoundError(TakenError):
    """A GitHub resource did not exist (HTTP 404)."""


class RateLimitError(TakenError):
    """GitHub API rate limit hit after retries: wait for the reset."""


# Retry policy for the `gh` subprocess. Transient 5xx failures and rate-limit
# responses both get a bounded number of retries with backoff and jitter; the
# jitter keeps parallel discover workers from retrying in lockstep and
# multiplying budget burn. Throttles are retried (not a hard stop) because a
# brief pause rides out GitHub's secondary limits, which are about request
# velocity rather than spent budget; `Retry-After` is honored when the
# response carries one.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
# Longest we will sleep for a single Retry-After directive: a huge value
# would hang the CLI, so cap it and let the final error surface instead.
MAX_RETRY_AFTER_DELAY = 120.0

# Minimum gap between search/issues calls. GitHub's secondary rate limits
# throttle request velocity, not budget, and they stay invisible to
# `gh api rate_limit`: a cold discover run died 12.5s in on back-to-back
# searches. Kept a module constant (not a flag) until --debug data argues
# for tuning it.
SEARCH_MIN_INTERVAL = 2.0

_search_lock = threading.Lock()
_last_search_at = 0.0


def _wait_search_pace():
    """Sleep until SEARCH_MIN_INTERVAL has passed since the last search.

    The caller must already hold _search_lock. Keeping the pace wait
    under the same lock that guards the search subprocess is what caps
    in-flight search requests at one: a thread cannot even start pacing
    its next search until the previous search (pace wait, subprocess,
    and retries) has fully finished.
    """
    global _last_search_at
    wait = SEARCH_MIN_INTERVAL - (time.monotonic() - _last_search_at)
    if wait > 0:
        time.sleep(wait)
    _last_search_at = time.monotonic()


def _pace_search():
    """Wait until SEARCH_MIN_INTERVAL has passed since the last search call."""
    with _search_lock:
        _wait_search_pace()


_TRANSIENT_5XX_RE = re.compile(
    r"\b50[0234]\b|internal server error|bad gateway|service unavailable|gateway timeout",
    re.IGNORECASE,
)
_SECONDARY_RATE_LIMIT_RE = re.compile(r"secondary rate limit", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry-?after[:\s]+(\d+)", re.IGNORECASE)
# A bare "404" substring also matches IDs like 40412; require a word boundary
# so only a real HTTP 404 status is treated as not-found.
_HTTP_404_RE = re.compile(r"\b404\b")


def _retry_after_seconds(err):
    """Parse a Retry-After directive (seconds) from an error message, if any."""
    match = _RETRY_AFTER_RE.search(err or "")
    if not match:
        return None
    try:
        seconds = int(match.group(1))
    except ValueError:
        return None
    return max(0.0, min(float(seconds), MAX_RETRY_AFTER_DELAY))


def _require_dict(value, endpoint):
    """Fail closed: a check that got a non-object response must error, not guess."""
    if not isinstance(value, dict):
        raise TakenError(f"`gh api {endpoint}` returned an unexpected response")
    return value


def search_issues(query, per_page=50):
    """Search issues via the GitHub search API.

    This is the same source the web aggregators use; we piggyback on it for
    candidates and do our own verification and ranking on top.
    """
    data = _require_dict(
        gh_api(
            "search/issues",
            {"q": query, "per_page": str(per_page), "sort": "updated", "order": "desc"},
        ),
        "search/issues",
    )
    items = data.get("items")
    if not isinstance(items, list):
        raise TakenError("search/issues returned an unexpected response")
    return items


def _require_list(value, endpoint):
    """Fail closed: a check that got a non-list response must error, not guess."""
    if not isinstance(value, list):
        raise TakenError(f"`gh api {endpoint}` returned an unexpected response")
    return value


def _cache_dir():
    """Location of the API response cache. Overridable via TAKEN_CACHE_DIR."""
    raw = os.environ.get("TAKEN_CACHE_DIR")
    if raw:
        # Expand ~ for convenience and to make safety checks reliable.
        return os.path.expanduser(raw)
    return os.path.join(os.path.expanduser("~"), ".cache", "taken")


def _cache_path():
    """Legacy single-file cache location (kept for one migration step)."""
    return os.path.join(_cache_dir(), "api_cache.json")


def _cache_file(key):
    """One file per cache key.

    The old design kept every entry in a single JSON file, so each write
    under threads re-read and re-wrote tens of megabytes while holding a
    lock, serializing all workers. Per-key files with atomic replace need
    no lock at all: concurrent writers to different keys never conflict,
    and same-key races resolve to last-writer-wins with a valid file.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), "v2", digest + ".json")


def _cache_key(endpoint, params):
    parts = [endpoint.lstrip("/")]
    for key in sorted(params or {}):
        parts.append(f"{key}={(params or {})[key]}")
    return "|".join(parts)


_IDENTITY = None
_IDENTITY_FETCHED = False


def _github_identity():
    """Return the authenticated `gh` login (lowercased), memoized per process.

    Cache entries are namespaced by identity so that switching identities
    (`gh auth switch`, or a shared TAKEN_CACHE_DIR on a shared machine)
    cannot serve one identity's cached data to another. Returns None when
    the identity cannot be determined; callers fail closed in that case.
    """
    global _IDENTITY, _IDENTITY_FETCHED
    if _IDENTITY_FETCHED:
        return _IDENTITY
    _IDENTITY_FETCHED = True
    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=API_TIMEOUT,
        )
        login = (proc.stdout or "").strip().lower()
        _IDENTITY = login if proc.returncode == 0 and login else None
    except Exception:
        _IDENTITY = None
    return _IDENTITY


def _namespaced_key(key):
    """Bind a cache key to the authenticated GitHub identity.

    Returns None when the identity is unknown; the cache then behaves as a
    permanent miss (fail closed) rather than risk cross-identity leakage.
    """
    identity = _github_identity()
    if identity is None:
        return None
    return f"github-user:{identity}|{key}"


def _mem_get(key):
    with _MEM_LOCK:
        entry = _MEM_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["fetched_at"] > CACHE_TTL_SECONDS:
        return None
    return entry["data"]


def _mem_put(key, entry):
    with _MEM_LOCK:
        _MEM_CACHE[key] = entry


def _sweep_expired():
    """Best-effort removal of stale cache files, run probabilistically."""
    try:
        now = time.time()
        for name in os.listdir(os.path.join(_cache_dir(), "v2")):
            if not name.endswith(".json"):
                continue
            path = os.path.join(_cache_dir(), "v2", name)
            try:
                if now - os.path.getmtime(path) > CACHE_TTL_SECONDS:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def _looks_like_taken_cache(cache_dir):
    """Return True if every file under cache_dir looks like a taken cache file.

    taken's on-disk cache contains only api_cache.json at the top level and
    v2/<digest>.json entry files. If anything else is present, the directory
    is not (only) a taken cache and must not be deleted wholesale.
    """
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            if name == "api_cache.json":
                continue
            if name.endswith(".json") and os.path.basename(root) == "v2":
                continue
            return False
    return True


def _is_cache_dir_safe(cache_dir):
    """Return True only if cache_dir is safe to delete wholesale.

    Accepts the default cache location (and paths inside it) outright. Any
    other path is accepted only if the directory actually looks like a taken
    cache — containing nothing but our own cache files. This fails closed: a
    malicious or accidental TAKEN_CACHE_DIR value (e.g. "/" or "/etc") can
    never cause unrelated directories to be deleted via shutil.rmtree().
    """
    try:
        resolved = os.path.realpath(cache_dir)
    except (OSError, ValueError):
        return False

    # Block the most dangerous cases — these directories should never be deleted.
    if resolved in ("/", os.path.sep):
        return False
    if resolved == os.path.expanduser("~"):
        return False
    # Also block /home itself (but not subdirectories like /home/user/src).
    if resolved == "/home":
        return False

    # Allow the default cache location and any explicitly set absolute path
    # that lives inside it.
    default_cache = os.path.realpath(os.path.join(os.path.expanduser("~"), ".cache", "taken"))
    if resolved == default_cache or resolved.startswith(default_cache + os.path.sep):
        return True

    # Anything else must prove it is a taken cache: it must exist and contain
    # nothing but our own cache files. A missing directory has nothing to
    # clear, and refusing surfaces a misconfigured TAKEN_CACHE_DIR instead of
    # silently reporting "cleared 0".
    return os.path.isdir(resolved) and _looks_like_taken_cache(resolved)


def clear_cache():
    """Delete the on-disk API response cache.

    Counts the cache entry files first so callers can report what was
    removed. Also drops the in-memory cache. Never raises: cache
    problems must not break the tool.

    Returns the number of cache entry files removed, or -1 if the path
    was rejected as unsafe (no deletion performed).
    """
    cache_dir = _cache_dir()
    if not _is_cache_dir_safe(cache_dir):
        _MEM_CACHE.clear()
        return -1
    removed = 0
    for _root, _dirs, files in os.walk(cache_dir):
        removed += sum(1 for name in files if name.endswith(".json"))
    try:
        shutil.rmtree(cache_dir)
    except OSError:
        pass
    _MEM_CACHE.clear()
    return removed


def _cache_read(key):
    key = _namespaced_key(key)
    if key is None:
        return None
    cached = _mem_get(key)
    if cached is not None:
        return cached
    try:
        with open(_cache_file(key), encoding="utf-8") as fh:
            entry = json.load(fh)
        if time.time() - entry["fetched_at"] > CACHE_TTL_SECONDS:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    _mem_put(key, entry)
    return entry["data"]


def _cache_write(key, data):
    key = _namespaced_key(key)
    if key is None:
        return
    entry = {"fetched_at": time.time(), "data": data}
    _mem_put(key, entry)
    try:
        path = _cache_file(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic write: readers never see a half-written file.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cache-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        # Drop the legacy single-file cache the first time we write new style.
        try:
            os.unlink(_cache_path())
        except OSError:
            pass
        if random.random() < 0.05:
            _sweep_expired()
    except OSError:
        pass  # the cache must never break the tool


def _is_rate_limited(err):
    """Detect rate-limit signals in `gh` stderr (HTTP 429 / 403 rate limit)."""
    lowered = err.lower()
    return "rate limit" in lowered or "429" in lowered or "too many requests" in lowered


def _rate_limit_message(endpoint, err):
    """Dedicated rate-limit message, with the reset time when gh reports one.

    Secondary-limit throttles get their own wording: GitHub's reset-time
    advice does not apply to them, and `gh api rate_limit` does not show
    them, so pointing the user there would be misleading.
    """
    if _SECONDARY_RATE_LIMIT_RE.search(err or ""):
        return (
            f"GitHub API secondary rate limit hit for `gh api {endpoint}`. "
            "GitHub asks clients to wait a few minutes before retrying; this "
            "limit is not shown by `gh api rate_limit`. "
            "No verdict was recorded."
        )
    reset = None
    match = re.search(r"reset\D*?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", err, re.IGNORECASE)
    if match:
        reset = match.group(1).replace("T", " ")
    else:
        match = re.search(
            r"(?:try again in|retry after|resets? in)\s+([^\n.]{1,40})", err, re.IGNORECASE
        )
        if match:
            reset = "in " + match.group(1).strip()
    when = (
        f" Rate limit resets {reset}."
        if reset
        else " Check `gh api rate_limit` for the reset time."
    )
    return (
        f"GitHub API rate limit exceeded for `gh api {endpoint}`.{when} "
        "No verdict was recorded: wait for the reset instead of retrying."
    )


def _gh_api_run(cmd, endpoint, paced):
    """Run one `gh api` call through the retry loop; return parsed JSON.

    When paced is True, every attempt starts with the search pace wait.
    The caller must hold _search_lock for the whole call, so the pace
    wait, the subprocess, and any retry backoff are all serialized and
    two search subprocesses can never be in flight at once.
    """
    attempt = 0
    while True:
        if paced:
            _wait_search_pace()
        try:
            record_api_call(endpoint)
            rest_start = time.perf_counter()
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=API_TIMEOUT)
            record_phase("rest", time.perf_counter() - rest_start)
        except FileNotFoundError:
            raise TakenError("the `gh` CLI is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise TakenError(f"`gh api {endpoint}` timed out after {API_TIMEOUT}s") from None
        if proc.returncode == 0:
            break
        err = (proc.stderr or "").strip()
        # Rate-limit signals first: a throttled response may cite numeric IDs
        # (e.g. installation 40412) that must not be misread as HTTP 404 below.
        if _is_rate_limited(err):
            attempt += 1
            if attempt >= RETRY_ATTEMPTS:
                raise RateLimitError(_rate_limit_message(endpoint, err))
            # Throttled: back off with jitter so parallel discover workers
            # don't retry in lockstep. Honor Retry-After when the response
            # carries one; a brief pause rides out secondary limits, which
            # are about request velocity rather than spent budget.
            delay = _retry_after_seconds(err)
            if delay is None:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            record_retry(delay)
            time.sleep(delay)
            continue
        if _HTTP_404_RE.search(err) or "Not Found" in err:
            raise NotFoundError(f"not found: {endpoint}")
        attempt += 1
        if not _TRANSIENT_5XX_RE.search(err) or attempt >= RETRY_ATTEMPTS:
            raise TakenError(f"`gh api {endpoint}` failed: {err[:300]}")
        # Transient 5xx: back off with jitter so parallel discover workers
        # don't retry in lockstep.
        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        record_retry(delay)
        time.sleep(delay)
    record_bytes(len((proc.stdout or "").encode("utf-8")))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise TakenError(f"`gh api {endpoint}` did not return JSON") from None
    return data


def gh_api(endpoint, params=None):
    """GET a GitHub API endpoint via `gh api` and return parsed JSON."""
    _require_safe_endpoint(endpoint)
    key = _cache_key(endpoint, params)
    if _CACHE_ENABLED:
        cache_start = time.perf_counter()
        cached = _cache_read(key)
        record_phase("cache", time.perf_counter() - cache_start)
        if cached is not None:
            record_cache_result(True)
            return cached
        record_cache_result(False)
    cmd = ["gh", "api", "--method", "GET", endpoint.lstrip("/")]
    # Pin the method explicitly: stock `gh` switches to POST whenever -f
    # parameters are added, which would turn reads into writes (e.g. POST
    # /repos/{o}/{r}/issues reads as "create an issue"). The GraphQL path
    # builds its own command and intentionally keeps the auto-POST.
    for key_param, value in (params or {}).items():
        cmd.extend(["-f", f"{key_param}={value}"])
    if endpoint.lstrip("/").startswith("search/"):
        # Search pacing: hold the lock through the pace wait AND the entire
        # retry loop, so parallel discover workers and retry bursts can never
        # have two search subprocesses in flight at once. Non-search
        # endpoints never touch this lock and stay fully parallel.
        with _search_lock:
            data = _gh_api_run(cmd, endpoint, paced=True)
    else:
        data = _gh_api_run(cmd, endpoint, paced=False)
    if _CACHE_ENABLED:
        _cache_write(key, data)
    return data


def _paged_list(endpoint, params=None):
    """GET every page of a list endpoint, up to the page cap.

    Returns (items, truncated). truncated is True when the loop fetched a
    full final page at the page cap, meaning more items may exist that were
    never scanned. Stops early on a short page. Each page goes through
    _require_list, so a bad page errors out instead of silently truncating
    the scan.

    The cap is MAX_SCAN_PAGES, raised by the authenticated budget tier
    (taken/budget.py) and never lowered by it, so embedder overrides
    such as the docs console's MAX_SCAN_PAGES = 1 keep working.
    """
    max_pages = budget.effective_cap(MAX_SCAN_PAGES, "scan_pages")
    items = []
    truncated = False
    for page in range(1, max_pages + 1):
        batch = _require_list(
            gh_api(endpoint, {**(params or {}), "per_page": "100", "page": str(page)}),
            endpoint,
        )
        items.extend(batch)
        if len(batch) < 100:
            break
        if page == max_pages:
            # Full page at the cap: the API may hold more items we did not fetch.
            truncated = True
    return items, truncated


# Raw fields check_issue() reads from an issue payload. A search/issues
# result item carries every one of them, so the discover path can skip the
# redundant per-issue GET when they are all present (issue #153).
_ISSUE_PAYLOAD_FIELDS = (
    "state",
    "title",
    "labels",
    "assignees",
    "comments",
    "user",
    "html_url",
    "created_at",
)


def _issue_facts_from_payload(payload, number):
    """Build check_issue()'s facts dict from a search/issues item.

    Returns None when the payload is missing anything check_issue() would
    extract, so the caller falls back to the plain GET instead of
    verdicting on incomplete evidence.
    """
    if not isinstance(payload, dict):
        return None
    try:
        if any(payload.get(field) is None for field in _ISSUE_PAYLOAD_FIELDS):
            return None
        return {
            "number": number,
            "state": payload["state"],
            "title": payload["title"],
            "labels": [label["name"] for label in payload["labels"]],
            "assignees": [user["login"] for user in payload["assignees"]],
            "comment_count": payload["comments"],
            "author": (payload["user"] or {}).get("login"),
            "url": payload["html_url"],
            "created_at": payload["created_at"],
        }
    except (KeyError, TypeError, AttributeError):
        # Malformed payload: not evidence of anything. The caller falls
        # back to the plain GET.
        return None


def check_issue(owner, repo, number, payload=None):
    """Fetch the basic facts about an issue.

    When `payload` is a pre-fetched search/issues item carrying every field
    above, the redundant GET is skipped. An incomplete payload falls back
    to the GET, so the plain path behaves exactly as before.
    """
    if payload is not None:
        facts = _issue_facts_from_payload(payload, number)
        if facts is not None:
            return facts
    endpoint = f"repos/{owner}/{repo}/issues/{number}"
    data = _require_dict(gh_api(endpoint), endpoint)
    return {
        "number": number,
        "state": data.get("state"),
        "title": data.get("title"),
        "labels": [label["name"] for label in data.get("labels", [])],
        "assignees": [user["login"] for user in data.get("assignees", [])],
        "comment_count": data.get("comments", 0),
        "author": (data.get("user") or {}).get("login"),
        "url": data.get("html_url"),
        "created_at": data.get("created_at"),
    }


def check_timeline(owner, repo, number):
    """Find PRs linked to the issue via timeline cross-reference events.

    These events never appear in the issue comments, which is the main
    reason this tool exists.

    Returns (linked, truncated): truncated is True when the timeline scan
    hit the page cap with a full final page, so a linked PR beyond the cap
    may have been missed.
    """
    endpoint = f"repos/{owner}/{repo}/issues/{number}/timeline"
    events, truncated = _paged_list(endpoint)
    linked = []
    seen = set()
    for event in events:
        if event.get("event") not in ("cross-referenced", "connected"):
            continue
        src = (event.get("source") or {}).get("issue") or {}
        match = PR_URL_RE.match(src.get("html_url") or "")
        if not match:
            continue
        pr_owner, pr_repo, pr_number = match.groups()
        key = (pr_owner, pr_repo, pr_number)
        if key in seen:
            continue
        seen.add(key)
        pr = gh_api(f"repos/{pr_owner}/{pr_repo}/pulls/{pr_number}")
        linked.append(
            {
                "number": int(pr_number),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "merged": bool(pr.get("merged_at")),
                "author": (pr.get("user") or {}).get("login"),
                "url": pr.get("html_url"),
            }
        )
    return linked, truncated


def find_claimant_hits(comments, me=None):
    """Scan comment bodies for claimant language, skipping the given login."""
    if not isinstance(comments, list):
        raise TakenError("comment scan got an unexpected response")
    hits = []
    me_lower = (me or "").lower()
    for comment in comments:
        author = (comment.get("user") or {}).get("login", "")
        if me_lower and author.lower() == me_lower:
            continue
        body = comment.get("body") or ""
        lowered = body.lower()
        matched = next((p for p in CLAIMANT_PATTERNS if p in lowered), None)
        if matched is None:
            continue
        snippet = " ".join(body.split())
        hits.append(
            {
                "author": author,
                "date": (comment.get("created_at") or "")[:10],
                "pattern": matched,
                "snippet": snippet[:160],
                "url": comment.get("html_url"),
            }
        )
    return hits


def fetch_comments(owner, repo, number):
    """Fetch raw issue comments (cached like everything else).

    Returns (comments, truncated): truncated is True when the comment scan
    hit the page cap with a full final page, so comments beyond the cap
    were never scanned.
    """
    endpoint = f"repos/{owner}/{repo}/issues/{number}/comments"
    return _paged_list(endpoint)


def check_claimants(owner, repo, number, me=None):
    """Fetch issue comments and scan them for claimant language.

    Returns (hits, truncated): truncated is True when the comment scan hit
    the page cap, so a claimant comment beyond the cap may have been missed.
    """
    comments, truncated = fetch_comments(owner, repo, number)
    return find_claimant_hits(comments, me=me), truncated


def _first_line_with(text, phrase):
    for line in text.splitlines():
        if phrase in line.lower():
            return line.strip()[:160]
    return ""


def classify_policy(text):
    """Classify a CONTRIBUTING-style document: ban, disclosure-required, or none-found."""
    for phrase in BAN_PHRASES:
        snippet = _first_line_with(text, phrase)
        if snippet:
            return "ban", snippet
    for phrase in DISCLOSURE_PHRASES:
        snippet = _first_line_with(text, phrase)
        if snippet:
            return "disclosure-required", snippet
    return "none-found", ""


def check_ai_policy(owner, repo):
    """Look for an AI contribution policy in CONTRIBUTING files."""
    for path in CONTRIBUTING_PATHS:
        endpoint = f"repos/{owner}/{repo}/contents/{path}"
        try:
            data = gh_api(endpoint)
        except NotFoundError:
            continue
        # A path that exists but is unreadable is a real failure, not "no policy".
        _require_dict(data, endpoint)
        try:
            raw = base64.b64decode(data.get("content") or "")
        except Exception as exc:
            raise TakenError(f"could not decode {path}: {exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        verdict, snippet = classify_policy(text)
        return {"verdict": verdict, "snippet": snippet, "source": path}
    return {"verdict": "none-found", "snippet": "", "source": None}


def list_open_issues(owner, repo, limit=20, label=None):
    """List open issues (not PRs) for a repo, most recently updated first.

    Returns the raw issue items (dicts), so callers can pass them as
    `payload=` into run_checks() and skip the per-issue refetch of data
    the listing already returned (issue #211). Items lacking any field
    check_issue() needs are still safe to pass: the payload is rejected
    and the plain GET runs instead.
    """
    endpoint = f"repos/{owner}/{repo}/issues"
    params = {"state": "open", "per_page": "100", "sort": "updated", "direction": "desc"}
    if label:
        params["labels"] = label
    found = []
    page = 1
    while len(found) < limit:
        params["page"] = str(page)
        items = _require_list(gh_api(endpoint, params), endpoint)
        if not items:
            break
        for item in items:
            if "pull_request" in item:
                continue
            found.append(item)
            if len(found) >= limit:
                break
        if len(items) < 100:
            break
        page += 1
    return found


def count_recent_contributors(owner, repo, days=CONTRIBUTORS_WINDOW_DAYS):
    """Count distinct people who landed commits in the last `days` days.

    Bots are excluded. Only the most recent 300 commits are scanned, so for
    very active repos this is a lower bound, not an exact census. Contributor
    breadth is a healthier signal than star count: stars accumulate forever,
    while contributors show who is actually landing changes right now.
    """
    endpoint = f"repos/{owner}/{repo}/commits"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    authors = set()
    commits_pages = budget.effective_cap(_REPO_COMMITS_PAGES, "repo_commits_pages")
    for page in range(1, commits_pages + 1):
        commits = _require_list(
            gh_api(endpoint, {"since": since, "per_page": "100", "page": str(page)}),
            endpoint,
        )
        if not commits:
            break
        for commit in commits:
            author = commit.get("author") or {}
            login = author.get("login") or ""
            if login:
                if login.endswith("[bot]"):
                    continue
                authors.add(login.lower())
            else:
                email = ((commit.get("commit") or {}).get("author") or {}).get("email") or ""
                if email:
                    authors.add(email.lower())
        if len(commits) < 100:
            break
    return len(authors)


def check_repo_health(owner, repo, window_days=HEALTH_WINDOW_DAYS):
    """Check recent pushes, merged PRs, and contributor breadth as activity signals."""
    endpoint = f"repos/{owner}/{repo}"
    data = _require_dict(gh_api(endpoint), endpoint)
    pushed_at = data.get("pushed_at") or ""
    pushed_recently = False
    if pushed_at:
        pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        pushed_recently = datetime.now(timezone.utc) - pushed_dt <= timedelta(days=window_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    recent_merges = 0
    pulls_pages = budget.effective_cap(_REPO_PULLS_PAGES, "repo_pulls_pages")
    for page in range(1, pulls_pages + 1):
        pulls_endpoint = f"repos/{owner}/{repo}/pulls"
        prs = _require_list(
            gh_api(
                pulls_endpoint,
                {
                    "state": "closed",
                    "per_page": "50",
                    "page": str(page),
                    "sort": "updated",
                    "direction": "desc",
                },
            ),
            pulls_endpoint,
        )
        if not prs:
            break
        for pr in prs:
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            if merged_dt >= cutoff:
                recent_merges += 1
        if len(prs) < 50:
            break
    return {
        "pushed_at": pushed_at[:10],
        "pushed_recently": pushed_recently,
        "recent_merges": recent_merges,
        "contributors": count_recent_contributors(owner, repo),
        "contributors_window_days": CONTRIBUTORS_WINDOW_DAYS,
    }


def run_checks(owner, repo, number, me=None, payload=None):
    """Run the full read-only check suite on one issue; return findings.

    `payload` is an optional pre-fetched search/issues item: when it
    carries every field check_issue() needs, the per-issue GET is skipped
    (issue #153). The plain path passes nothing and behaves exactly as
    before.

    Fetches run cheapest-decisive-first and stop early as soon as decide()
    reports TAKEN: the issue call alone settles closed and assigned issues,
    and the timeline settles issues with an open linked PR, so the expensive
    comment, policy, and health scans only run while the verdict is still
    open. Stages after the stop point keep neutral placeholders so the
    findings shape never changes, and "stages_skipped" names them so
    format_human() reports skipped stages as not checked, never as
    observed facts.

    The early stop is sound because decide() itself is the stop condition,
    evaluated after each stage: the stages after the last check (claimants,
    policy, health) can only append CAUTION reasons, never overturn a
    TAKEN. Every TAKEN-capable signal (issue state/assignees, then linked
    PRs) is fully fetched before its decide() check runs (issue #125).
    """
    issue = check_issue(owner, repo, number, payload=payload)
    findings = {
        "target": f"{owner}/{repo}#{number}",
        "issue": issue,
        "linked_prs": [],
        "claimants": [],
        # Neutral placeholders for stages not yet fetched: decide() reads
        # "not-checked" / a skipped-healthy repo as no signal either way.
        # recent_merges is 0 (not 1) so welcoming_signals() stays silent;
        # pushed_recently=True keeps decide() neutral on its own.
        "ai_policy": {
            "verdict": "not-checked",
            "snippet": "",
            "source": None,
        },
        "repo_health": {
            "pushed_at": None,
            "pushed_recently": True,
            "recent_merges": 0,
            "contributors": 0,
            "contributors_window_days": CONTRIBUTORS_WINDOW_DAYS,
            "skipped": True,
        },
        # Which evidence scans stopped early at the page cap. decide() uses
        # this to avoid a silent GO on incomplete evidence. The REST issue
        # payload carries every label, so "labels" is always False here;
        # the key exists for shape parity with the GraphQL findings.
        "scan_truncated": {
            "timeline": False,
            "comments": False,
            "labels": False,
        },
        # Stages never fetched because decide() already reported TAKEN.
        # format_human() renders these as "not checked" so a skipped stage
        # is never presented as an observed fact.
        "stages_skipped": ["timeline", "claimants", "ai_policy", "repo_health"],
    }
    if decide(findings)[0] == TAKEN:
        return findings
    linked_prs, timeline_truncated = check_timeline(owner, repo, number)
    findings["linked_prs"] = linked_prs
    findings["scan_truncated"]["timeline"] = timeline_truncated
    findings["stages_skipped"] = ["claimants", "ai_policy", "repo_health"]
    if decide(findings)[0] == TAKEN:
        return findings
    claimants, comments_truncated = check_claimants(owner, repo, number, me=me)
    findings["claimants"] = claimants
    findings["scan_truncated"]["comments"] = comments_truncated
    findings["ai_policy"] = check_ai_policy(owner, repo)
    findings["repo_health"] = check_repo_health(owner, repo)
    findings["stages_skipped"] = []
    return findings
