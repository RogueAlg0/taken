"""Unit tests for taken/transport.py and the httpx retry path in checks.

All hermetic: no network, no ambient credentials. Anything touching the
environment (env vars, sys.modules, sys.path, module globals) is
monkeypatched or saved and restored per test.
"""

import sys
import time
import types

import pytest

from taken import checks, transport


@pytest.fixture
def clean_transport(monkeypatch):
    """Isolate transport module globals and the environment per test."""
    monkeypatch.setattr(transport, "_surrogate", None)
    monkeypatch.setattr(transport, "_client", None)
    monkeypatch.setattr(transport, "_rate_limit_state", {"remaining": None, "reset": None})
    monkeypatch.setattr(transport, "_throttle_checked", False)
    monkeypatch.setattr(transport, "_throttle_fn", None)
    # Point the well-known broker/pacer dirs at missing paths so tests
    # control auth and pacing explicitly; strip them from sys.path too in
    # case an earlier test caused an import.
    monkeypatch.setattr(transport, "_CREDENTIAL_BROKER_DIR", "/nonexistent-broker-dir")
    monkeypatch.setattr(transport, "_THROTTLE_BIN_DIR", "/nonexistent-throttle-dir")
    for var in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "TAKEN_TRANSPORT",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delitem(sys.modules, "dynamic_credentials", raising=False)
    monkeypatch.delitem(sys.modules, "gh_throttle", raising=False)
    monkeypatch.setattr(
        sys,
        "path",
        [p for p in sys.path if "skill-creator" not in p and "workspace/bin" not in p],
    )
    return transport


def _install_fake_broker(monkeypatch, calls):
    mod = types.ModuleType("dynamic_credentials")

    def fake_entry(service, kind):
        calls.append((service, kind))
        return {"surrogate": "hsurr:test123"}

    mod.dynamic_credential_entry = fake_entry
    monkeypatch.setitem(sys.modules, "dynamic_credentials", mod)


# --- transport selection -------------------------------------------------


def test_use_httpx_opt_in_only(clean_transport, monkeypatch):
    assert transport.use_httpx() is False
    monkeypatch.setenv("TAKEN_TRANSPORT", "httpx")
    assert transport.use_httpx() is True
    monkeypatch.setenv("TAKEN_TRANSPORT", "HTTPX")
    assert transport.use_httpx() is True
    monkeypatch.setenv("TAKEN_TRANSPORT", "subprocess")
    assert transport.use_httpx() is False
    monkeypatch.setenv("TAKEN_TRANSPORT", "bogus")
    assert transport.use_httpx() is False


# --- proxy URL quoting ---------------------------------------------------


def test_quote_proxy_url_quotes_password():
    raw = "http://user:p@ss:word@proxy.example:8080"
    assert transport.quote_proxy_url(raw) == "http://user:p%40ss%3Aword@proxy.example:8080"


def test_quote_proxy_url_malformed_passthrough():
    # Unparseable input passes through unchanged; httpx raises its own
    # error for it instead of the quoting helper crashing.
    raw = "http://user:pw@proxy.example:notaport"
    assert transport.quote_proxy_url(raw) == raw


def test_quote_proxy_url_no_userinfo_passthrough():
    raw = "http://proxy.example:8080"
    assert transport.quote_proxy_url(raw) == raw


def test_client_kwargs_uses_quoted_proxy(clean_transport, monkeypatch):
    monkeypatch.setenv("https_proxy", "http://user:p@ss@proxy.example:3128")
    kwargs = transport._client_kwargs()
    assert kwargs["proxy"] == "http://user:p%40ss@proxy.example:3128"
    assert kwargs["trust_env"] is False


def test_client_kwargs_no_proxy_env(clean_transport):
    kwargs = transport._client_kwargs()
    assert "proxy" not in kwargs
    assert "trust_env" not in kwargs
    assert kwargs["verify"] is True
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"


def test_client_kwargs_uses_ssl_cert_file(clean_transport, monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("fake-bundle")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    kwargs = transport._client_kwargs()
    assert kwargs["verify"] == str(bundle)


# --- auth ----------------------------------------------------------------


def test_resolve_bearer_surrogate_cached(clean_transport, monkeypatch):
    calls = []
    _install_fake_broker(monkeypatch, calls)
    first = transport._resolve_bearer()
    second = transport._resolve_bearer()
    assert first == "Bearer hsurr:test123"
    assert second == first
    assert calls == [("custom.github", "access_token")]  # fetched once per process


def test_resolve_bearer_env_fallback(clean_transport, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gho_envtoken")
    assert transport._resolve_bearer() == "Bearer gho_envtoken"
    assert transport._surrogate is None  # env path never mints a surrogate


def test_resolve_bearer_none_available(clean_transport):
    assert transport._resolve_bearer() is None


def test_api_get_raises_auth_error_without_credential(clean_transport, monkeypatch):
    monkeypatch.setattr(transport, "_pace", lambda path: None)
    with pytest.raises(transport.TransportAuthError):
        transport.api_get("rate_limit")


# --- request mechanics ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status, body="", headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "params": params})
        return self._responses.pop(0)


def _surrogate_resolve_factory(bearers):
    def fake_resolve():
        transport._surrogate = "active"
        return bearers.pop(0)

    return fake_resolve


def test_api_get_refreshes_surrogate_once_on_401(clean_transport, monkeypatch):
    monkeypatch.setattr(transport, "_pace", lambda path: None)
    fake = _FakeClient([_FakeResponse(401, "Bad credentials"), _FakeResponse(200, '{"ok": true}')])
    monkeypatch.setattr(transport, "_get_client", lambda: fake)
    monkeypatch.setattr(
        transport, "_resolve_bearer", _surrogate_resolve_factory(["Bearer hsurr:old", "Bearer x"])
    )
    status, body, _headers = transport.api_get("repos/o/r")
    assert status == 200
    assert body == '{"ok": true}'
    assert len(fake.requests) == 2
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer hsurr:old"
    assert fake.requests[1]["headers"]["Authorization"] == "Bearer x"
    assert fake.requests[0]["url"] == "https://api.github.com/repos/o/r"


def test_api_get_returns_second_401_without_looping(clean_transport, monkeypatch):
    monkeypatch.setattr(transport, "_pace", lambda path: None)
    fake = _FakeClient([_FakeResponse(401, "Bad"), _FakeResponse(401, "Bad")])
    monkeypatch.setattr(transport, "_get_client", lambda: fake)
    monkeypatch.setattr(
        transport,
        "_resolve_bearer",
        _surrogate_resolve_factory(["Bearer a", "Bearer b", "Bearer c"]),
    )
    status, _body, _headers = transport.api_get("repos/o/r")
    assert status == 401
    assert len(fake.requests) == 2  # refresh + retry once, then stop


def test_single_get_timeout_maps(clean_transport, monkeypatch):
    class _TimeoutClient:
        def get(self, *args, **kwargs):
            raise transport.httpx.TimeoutException("timed out")

    monkeypatch.setattr(transport, "_get_client", lambda: _TimeoutClient())
    with pytest.raises(transport.TransportTimeout):
        transport._single_get("repos/o/r", None, "Bearer x", 60)


def test_single_get_http_error_maps(clean_transport, monkeypatch):
    class _BrokenClient:
        def get(self, *args, **kwargs):
            raise transport.httpx.ConnectError("refused")

    monkeypatch.setattr(transport, "_get_client", lambda: _BrokenClient())
    with pytest.raises(transport.TransportError):
        transport._single_get("repos/o/r", None, "Bearer x", 60)


# --- pacing and preemptive rate-limit sleep --------------------------------


def test_pace_calls_shared_throttle(clean_transport, monkeypatch):
    calls = []
    monkeypatch.setattr(transport, "_throttle_fn", lambda path: calls.append(path))
    monkeypatch.setattr(transport, "_throttle_checked", True)
    transport._pace("search/issues")
    assert calls == ["search/issues"]


def test_pace_noop_without_throttle(clean_transport):
    transport._pace("repos/o/r")  # must not raise


def test_note_rate_limit_headers(clean_transport):
    transport._note_rate_limit_headers(
        {"x-ratelimit-remaining": "42", "x-ratelimit-reset": "1893456000"}
    )
    assert transport._rate_limit_state == {"remaining": 42, "reset": 1893456000}
    transport._note_rate_limit_headers({})
    assert transport._rate_limit_state == {"remaining": None, "reset": None}


def test_preemptive_sleep_waits_for_reset(clean_transport, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    transport._rate_limit_state.update({"remaining": 0, "reset": time.time() + 30})
    transport._preemptive_sleep()
    assert len(sleeps) == 1
    assert 29.0 < sleeps[0] <= 31.0


def test_preemptive_sleep_skips_when_budget_healthy(clean_transport, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    transport._rate_limit_state.update({"remaining": 5000, "reset": time.time() + 3000})
    transport._preemptive_sleep()
    assert sleeps == []


def test_preemptive_sleep_raises_when_reset_far(clean_transport):
    transport._rate_limit_state.update({"remaining": 0, "reset": time.time() + 3600})
    with pytest.raises(transport.TransportRateLimited) as exc:
        transport._preemptive_sleep()
    assert exc.value.reset_epoch > 0


# --- checks.py httpx retry path -------------------------------------------


def test_is_http_rate_limited():
    assert checks._is_http_rate_limited(429, "", {}) is True
    assert checks._is_http_rate_limited(403, "", {"x-ratelimit-remaining": "0"}) is True
    assert checks._is_http_rate_limited(403, "API rate limit exceeded", {}) is True
    assert checks._is_http_rate_limited(403, "Too many requests", {}) is True
    assert checks._is_http_rate_limited(403, "Resource not accessible", {}) is False
    assert checks._is_http_rate_limited(200, "", {}) is False
    assert checks._is_http_rate_limited(500, "", {}) is False


def test_http_maybe_retry_not_found():
    with pytest.raises(checks.NotFoundError):
        checks._http_maybe_retry("repos/o/r", 404, "Not Found", {}, 0)


def test_http_maybe_retry_rate_limit_backs_off(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    nxt = checks._http_maybe_retry("search/issues", 429, "", {"retry-after": "5"}, 0)
    assert nxt == 1


def test_http_maybe_retry_rate_limit_exhausts(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(checks.RateLimitError):
        checks._http_maybe_retry("search/issues", 429, "", {}, checks.RETRY_ATTEMPTS - 1)


def test_http_maybe_retry_transient_5xx(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert checks._http_maybe_retry("repos/o/r", 503, "", {}, 0) == 1


def test_http_maybe_retry_terminal_4xx():
    with pytest.raises(checks.TakenError):
        checks._http_maybe_retry("repos/o/r", 400, "Bad request", {}, 0)


def test_http_backoff_honors_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    checks._http_backoff({"retry-after": "7"}, 1)
    assert sleeps == [7.0]


def test_http_backoff_caps_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    checks._http_backoff({"retry-after": "99999"}, 1)
    assert sleeps == [checks.MAX_RETRY_AFTER_DELAY]


def test_http_backoff_exponential_without_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(checks.random, "uniform", lambda a, b: 0.0)
    checks._http_backoff({}, 1)
    assert sleeps == [checks.RETRY_BASE_DELAY]


def test_http_rate_limit_message_secondary():
    msg = checks._http_rate_limit_message("search/issues", secondary=True)
    assert "secondary rate limit" in msg
    assert "No verdict was recorded" in msg


def test_http_rate_limit_message_reset():
    msg = checks._http_rate_limit_message("repos/o/r", reset_epoch=1893456000)
    assert "2030-01-01 00:00" in msg


def test_http_rate_limit_message_fallback():
    msg = checks._http_rate_limit_message("repos/o/r")
    assert "gh api rate_limit" in msg


def _fake_api_get(responses):
    def fake(endpoint, params=None, timeout=60):
        return responses.pop(0)

    return fake


def test_gh_api_run_httpx_success(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"ok": true}', {})]))
    assert checks._gh_api_run_httpx("repos/o/r", None, paced=False) == {"ok": True}


def test_gh_api_run_httpx_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(
        transport,
        "api_get",
        _fake_api_get([(503, "unavailable", {}), (200, '{"ok": true}', {})]),
    )
    assert checks._gh_api_run_httpx("repos/o/r", None, paced=False) == {"ok": True}


def test_gh_api_run_httpx_not_found(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(404, "Not Found", {})]))
    with pytest.raises(checks.NotFoundError):
        checks._gh_api_run_httpx("repos/o/r", None, paced=False)


def test_gh_api_run_httpx_non_json(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, "not json", {})]))
    with pytest.raises(checks.TakenError):
        checks._gh_api_run_httpx("repos/o/r", None, paced=False)


def test_gh_api_run_httpx_timeout_maps(monkeypatch):
    def fake(endpoint, params=None, timeout=60):
        raise transport.TransportTimeout("timed out")

    monkeypatch.setattr(transport, "api_get", fake)
    with pytest.raises(checks.TakenError, match="timed out"):
        checks._gh_api_run_httpx("repos/o/r", None, paced=False)


def test_gh_api_run_httpx_preemptive_limit_maps(monkeypatch):
    def fake(endpoint, params=None, timeout=60):
        raise transport.TransportRateLimited(1893456000)

    monkeypatch.setattr(transport, "api_get", fake)
    with pytest.raises(checks.RateLimitError, match="2030-01-01"):
        checks._gh_api_run_httpx("repos/o/r", None, paced=False)


# --- transport dispatch in _rest_get ----------------------------------------


def test_rest_get_httpx_selected(monkeypatch):
    monkeypatch.setenv("TAKEN_TRANSPORT", "httpx")
    seen = {}

    def fake_run_httpx(endpoint, params, paced):
        seen.update(endpoint=endpoint, params=params, paced=paced)
        return {"via": "httpx"}

    monkeypatch.setattr(checks, "_gh_api_run_httpx", fake_run_httpx)
    assert checks._rest_get("repos/o/r", {"a": "b"}, paced=False) == {"via": "httpx"}
    assert seen == {"endpoint": "repos/o/r", "params": {"a": "b"}, "paced": False}


def test_rest_get_falls_back_on_auth_error(monkeypatch):
    monkeypatch.setenv("TAKEN_TRANSPORT", "httpx")

    def fake_run_httpx(endpoint, params, paced):
        raise transport.TransportAuthError("nope")

    monkeypatch.setattr(checks, "_gh_api_run_httpx", fake_run_httpx)
    captured = {}

    def fake_run(cmd, endpoint, paced):
        captured["cmd"] = cmd
        return {"via": "subprocess"}

    monkeypatch.setattr(checks, "_gh_api_run", fake_run)
    assert checks._rest_get("repos/o/r", None, paced=False) == {"via": "subprocess"}
    assert captured["cmd"][:4] == ["gh", "api", "--method", "GET"]


def test_rest_get_default_is_subprocess(monkeypatch):
    monkeypatch.delenv("TAKEN_TRANSPORT", raising=False)
    called = []

    def fake_run(cmd, endpoint, paced):
        called.append(True)
        return {"via": "subprocess"}

    monkeypatch.setattr(checks, "_gh_api_run", fake_run)
    assert checks._rest_get("repos/o/r", None, paced=False) == {"via": "subprocess"}
    assert called == [True]


def test_gh_api_uses_httpx_when_selected(monkeypatch):
    """End to end through gh_api with the flag on: transport, not subprocess."""
    monkeypatch.setenv("TAKEN_TRANSPORT", "httpx")
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)

    def fake_run_httpx(endpoint, params, paced):
        assert paced is False
        return {"ok": True}

    def fake_run(cmd, endpoint, paced):
        raise AssertionError("subprocess path must not run when httpx is selected")

    monkeypatch.setattr(checks, "_gh_api_run_httpx", fake_run_httpx)
    monkeypatch.setattr(checks, "_gh_api_run", fake_run)
    assert checks.gh_api("repos/octo/repo") == {"ok": True}
