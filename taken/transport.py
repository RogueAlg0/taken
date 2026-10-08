"""In-process httpx transport for the GitHub REST API.

Phase 1 of the httpx migration: replaces one `gh api` subprocess per call
with a single persistent pooled httpx.Client per process. Phase 2 adds
ETag conditional requests: when the caller supplies an ETag, the transport
sends If-None-Match and surfaces 304 Not Modified for the caller
(taken/checks.py) to resolve against its response cache. taken keeps its
"never sees raw tokens" posture: authentication resolves once per process
to an in-process surrogate that the egress proxy swaps for the real
credential, exactly as the `gh` shim does today. The surrogate lives in
memory only; it is never logged, never persisted.

Selection: TAKEN_TRANSPORT=httpx opts into this transport. Anything else
(including unset) keeps the historical subprocess path while the soak
runs. When httpx is selected but no credential is available, api_get
raises TransportAuthError and the caller falls back to the subprocess
path: degraded, not dead.

Pacing: every request first goes through the shared token-bucket pacer
(~/workspace/bin/gh_throttle.py) when it is available, so old and new
transports share one budget during rollout. A second, additive layer
reads X-RateLimit-Remaining / X-RateLimit-Reset on every response and
sleeps preemptively when the budget is nearly spent.
"""

import atexit
import os
import sys
import threading
import time
import urllib.parse

from taken import __version__

API_BASE = "https://api.github.com/"
DEFAULT_TIMEOUT = 60.0
_USER_AGENT = f"taken/{__version__} (httpx)"

# Well-known locations for this environment's credential broker and the
# shared token-bucket pacer (the same paths the project's own `gh` shim
# uses). Both are optional: when absent, auth falls back to
# GH_TOKEN/GITHUB_TOKEN (then to the subprocess path) and pacing relies
# on the header-aware backoff alone.
_CREDENTIAL_BROKER_DIR = "/opt/hatch/skills/skill-creator/bin"
_THROTTLE_BIN_DIR = os.path.expanduser("~/workspace/bin")

# Preemptive rate-limit sleep: when the last response showed this many (or
# fewer) requests remaining, wait for the reset window before sending more.
_PREEMPTIVE_REMAINING_THRESHOLD = 10
# Longest preemptive sleep: beyond this, fail fast with TransportRateLimited
# instead of hanging the CLI (same philosophy as MAX_RETRY_AFTER_DELAY).
_MAX_PREEMPTIVE_SLEEP = 120.0


class TransportError(Exception):
    """Base class for httpx transport failures."""


class TransportAuthError(TransportError):
    """No credential available: the caller should use the subprocess path."""


class TransportTimeout(TransportError):
    """The request timed out."""


class TransportRateLimited(TransportError):
    """Preemptive backoff: the last response showed an exhausted budget."""

    def __init__(self, reset_epoch):
        self.reset_epoch = reset_epoch
        super().__init__(f"rate limit budget exhausted; resets at {reset_epoch}")


def use_httpx():
    """True when TAKEN_TRANSPORT selects the httpx transport.

    Opt-in during the soak: only the explicit value "httpx" (any case)
    enables it. Anything else, including unset, keeps the historical
    subprocess path, which is the safe direction for a typo'd value.
    """
    return os.environ.get("TAKEN_TRANSPORT", "").strip().lower() == "httpx"


def quote_proxy_url(raw):
    """Rebuild a proxy URL with the userinfo percent-quoted.

    httpx rejects proxy URLs whose userinfo contains characters that
    urllib tolerates (raises InvalidURL on the raw form); quoting the
    username and password fixes it. URLs without userinfo pass through
    unchanged, as does input too malformed to parse (httpx will raise its
    own error for those).
    """
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port  # force the parse now, while we can fall back
    except ValueError:
        return raw
    if not parts.username:
        return raw
    userinfo = urllib.parse.quote(parts.username, safe="")
    if parts.password:
        userinfo += ":" + urllib.parse.quote(parts.password, safe="")
    host = parts.hostname or ""
    if port:
        host += f":{port}"
    netloc = f"{userinfo}@{host}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _raw_proxy_url():
    for var in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        raw = os.environ.get(var)
        if raw:
            return raw
    return None


def _verify_arg():
    """CA bundle for TLS verification.

    With trust_env disabled httpx does not read SSL_CERT_FILE, so pass
    the bundle explicitly when the environment provides one (this
    environment's egress proxy intercepts TLS with an internal CA).
    Otherwise True (httpx/certifi default).
    """
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            return path
    return True


# httpx is imported lazily: the default subprocess transport path must work
# without the dependency installed, so nothing at module import time may
# require it. Only the httpx code path below touches it.
def _httpx():
    try:
        import httpx
    except ImportError as exc:
        raise TransportError("the httpx transport needs the 'httpx' package installed") from exc
    return httpx


def __getattr__(name):
    # PEP 562: keep `transport.httpx` working for existing consumers while
    # the real import stays lazy (see _httpx above).
    if name == "httpx":
        return _httpx()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _client_kwargs():
    kwargs = {
        "headers": {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
        "timeout": DEFAULT_TIMEOUT,
        "verify": _verify_arg(),
        "limits": _httpx().Limits(max_connections=20, max_keepalive_connections=20),
    }
    raw_proxy = _raw_proxy_url()
    if raw_proxy is not None:
        # Explicit proxy with a quoted userinfo. trust_env must be off or
        # httpx would re-read (and choke on) the raw env URL itself.
        kwargs["proxy"] = quote_proxy_url(raw_proxy)
        kwargs["trust_env"] = False
    return kwargs


_client = None
_client_lock = threading.Lock()


def _get_client():
    """One persistent pooled client per process (httpx.Client is thread-safe)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _httpx().Client(**_client_kwargs())
                atexit.register(_close_client)
    return _client


def _close_client():
    global _client
    client, _client = _client, None
    if client is not None:
        client.close()


def _broker_entry_fn():
    """Return dynamic_credential_entry, or None when the broker is absent."""
    try:
        from dynamic_credentials import dynamic_credential_entry  # type: ignore[import-not-found]

        return dynamic_credential_entry
    except ImportError:
        pass
    if os.path.isdir(_CREDENTIAL_BROKER_DIR) and _CREDENTIAL_BROKER_DIR not in sys.path:
        # Same well-known broker path the project's own `gh` shim uses.
        sys.path.insert(0, _CREDENTIAL_BROKER_DIR)
        try:
            from dynamic_credentials import (
                dynamic_credential_entry,
            )

            return dynamic_credential_entry
        except ImportError:
            return None
    return None


_surrogate = None
_surrogate_lock = threading.Lock()


def _resolve_bearer():
    """Return the Authorization header value, or None when unavailable.

    Surrogate first (this environment's authd; the egress proxy swaps it
    for the real credential, so taken never sees a raw token), then the
    standard GH_TOKEN/GITHUB_TOKEN env vars. The value is never logged.
    """
    global _surrogate
    if _surrogate is not None:
        return "Bearer " + _surrogate
    with _surrogate_lock:
        if _surrogate is not None:
            return "Bearer " + _surrogate
        entry_fn = _broker_entry_fn()
        if entry_fn is not None:
            try:
                _surrogate = entry_fn("custom.github", "access_token")["surrogate"]
                return "Bearer " + _surrogate
            except Exception:
                _surrogate = None
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            token = os.environ.get(var)
            if token:
                return "Bearer " + token
    return None


def _clear_surrogate():
    global _surrogate
    with _surrogate_lock:
        _surrogate = None


_throttle_fn = None
_throttle_checked = False


def _load_throttle():
    """Return gh_throttle.throttle, or None when the pacer is absent."""
    global _throttle_fn, _throttle_checked
    if _throttle_checked:
        return _throttle_fn
    _throttle_checked = True
    try:
        from gh_throttle import throttle  # type: ignore[import-not-found]

        _throttle_fn = throttle
        return _throttle_fn
    except ImportError:
        pass
    if os.path.isdir(_THROTTLE_BIN_DIR) and _THROTTLE_BIN_DIR not in sys.path:
        sys.path.insert(0, _THROTTLE_BIN_DIR)
        try:
            from gh_throttle import throttle

            _throttle_fn = throttle
        except ImportError:
            _throttle_fn = None
    return _throttle_fn


def _pace(path):
    """Apply the shared token-bucket pace when the pacer is available."""
    throttle = _load_throttle()
    if throttle is not None:
        throttle(path)


_rate_limit_state = {"remaining": None, "reset": None}
_rate_limit_lock = threading.Lock()


def _note_rate_limit_headers(headers):
    try:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        with _rate_limit_lock:
            _rate_limit_state["remaining"] = int(remaining) if remaining is not None else None
            _rate_limit_state["reset"] = int(reset) if reset is not None else None
    except (TypeError, ValueError):
        pass


def _preemptive_sleep():
    """Sleep before sending when the last response showed a spent budget.

    Additive second layer under the token bucket: when remaining is at or
    below the threshold and the reset window is still in the future, wait
    it out instead of burning a request that would 403. A reset further
    out than _MAX_PREEMPTIVE_SLEEP raises TransportRateLimited so the CLI
    fails fast with a clear message instead of hanging.
    """
    with _rate_limit_lock:
        remaining = _rate_limit_state["remaining"]
        reset = _rate_limit_state["reset"]
    if remaining is None or reset is None or remaining > _PREEMPTIVE_REMAINING_THRESHOLD:
        return
    wait = reset - time.time()
    if wait <= 0:
        return
    if wait > _MAX_PREEMPTIVE_SLEEP:
        raise TransportRateLimited(reset)
    time.sleep(wait + 1.0)


def _single_get(endpoint, params, bearer, timeout, etag=None):
    client = _get_client()
    headers = {"Authorization": bearer}
    if etag is not None:
        headers["If-None-Match"] = etag
    try:
        resp = client.get(
            API_BASE + endpoint.lstrip("/"),
            params=params or {},
            headers=headers,
            timeout=timeout,
        )
    except _httpx().TimeoutException as exc:
        raise TransportTimeout(f"GET {endpoint} timed out after {timeout}s") from exc
    except _httpx().HTTPError as exc:
        raise TransportError(f"GET {endpoint} failed: {exc}") from exc
    _note_rate_limit_headers(resp.headers)
    return resp.status_code, resp.text, dict(resp.headers)


def api_get(endpoint, params=None, timeout=DEFAULT_TIMEOUT, etag=None):
    """GET one GitHub API path; return (status_code, body_text, headers).

    Single attempt: token-bucket pacing, preemptive rate-limit sleep, and
    one surrogate refresh on 401 all happen here. Retry policy stays with
    the caller (taken/checks.py). Raises TransportAuthError when no
    credential is available, TransportTimeout on timeout,
    TransportRateLimited when the budget is spent.

    When etag is given, the request is conditional (If-None-Match) and a
    304 Not Modified comes back as status 304 with an empty body; the
    caller resolves it against its cache. The etag parameter defaults to
    None so existing callers are unaffected.
    """
    _pace(endpoint)
    _preemptive_sleep()
    bearer = _resolve_bearer()
    if bearer is None:
        raise TransportAuthError("no credential available for the httpx transport")
    status, body, headers = _single_get(endpoint, params, bearer, timeout, etag=etag)
    if status == 401 and _surrogate is not None:
        # The surrogate may have expired: refresh once, retry once.
        _clear_surrogate()
        bearer = _resolve_bearer()
        if bearer is None:
            raise TransportAuthError("credential refresh failed for the httpx transport")
        status, body, headers = _single_get(endpoint, params, bearer, timeout, etag=etag)
    return status, body, headers
