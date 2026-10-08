"""Hermetic tests for phase 3: GraphQL over the httpx transport.

Why hermetic: this environment's egress proxy answers every GraphQL
request with a canned introspection schema, so no live GraphQL round
trip can be verified here (`taken --rest` is forced locally for the
same reason). These tests stub the HTTP layer instead and assert
request construction, auth, error mapping, retry discipline, and the
points-based preemptive sleep. The REST smoke path (which shares the
same client, auth, and pacing) is proven live by the phase 1/2 work.
"""

import argparse
import os
import sys
import types
from unittest import mock

import httpx
import pytest

from taken import checks, graphql, transport


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """GraphQL transport tests must not touch the real cache."""
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)


@pytest.fixture()
def clean_env(monkeypatch):
    """Scrub env, module state, and optional helpers per test."""
    for var in (
        "TAKEN_GRAPHQL_TRANSPORT",
        "TAKEN_TRANSPORT",
        "TAKEN_GRAPHQL",
        "TAKEN_REST",
        "TAKEN_PERSISTENT_SESSION",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(transport, "_surrogate", None)
    monkeypatch.setattr(transport, "_client", None)
    monkeypatch.setattr(transport, "_rate_limit_state", {"remaining": None, "reset": None})
    monkeypatch.setattr(transport, "_graphql_rate_state", {"remaining": None, "reset": None})
    monkeypatch.setattr(transport, "_throttle_checked", False)
    monkeypatch.setattr(transport, "_throttle_fn", None)
    monkeypatch.setattr(transport, "_CREDENTIAL_BROKER_DIR", "/nonexistent-broker-dir")
    monkeypatch.setattr(transport, "_THROTTLE_BIN_DIR", "/nonexistent-throttle-dir")
    monkeypatch.delitem(sys.modules, "dynamic_credentials", raising=False)
    monkeypatch.delitem(sys.modules, "gh_throttle", raising=False)


def _install_fake_broker(monkeypatch, surrogates):
    """Broker returning the given surrogates in order, one per call."""
    calls = {"n": 0}
    mod = types.ModuleType("dynamic_credentials")

    def entry(name, kind):
        idx = min(calls["n"], len(surrogates) - 1)
        calls["n"] += 1
        return {"surrogate": surrogates[idx]}

    mod.dynamic_credential_entry = entry
    monkeypatch.setitem(sys.modules, "dynamic_credentials", mod)
    return calls


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stand-in for httpx.Client; handler decides the response or raises."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.handler(url, json, headers)


def _install_client(monkeypatch, handler):
    client = _FakeClient(handler)
    monkeypatch.setattr(transport, "_get_client", lambda: client)
    return client


def _ok_payload(**overrides):
    payload = {
        "data": {
            "repository": {"pushedAt": "2026-10-01T00:00:00Z"},
            "rateLimit": {
                "limit": 5000,
                "cost": 12,
                "remaining": 4988,
                "resetAt": "2026-10-08T03:00:00Z",
            },
        }
    }
    payload.update(overrides)
    return payload


# --- transport.api_graphql -------------------------------------------------


def test_api_graphql_posts_json_to_graphql_endpoint(clean_env, monkeypatch):
    import json as jsonlib

    _install_fake_broker(monkeypatch, ["hsurr:test"])
    client = _install_client(
        monkeypatch, lambda url, body, headers: _FakeResponse(200, jsonlib.dumps(_ok_payload()))
    )
    status, payload = transport.api_graphql("query { viewer { login } }", {"a": 1})
    assert status == 200
    assert payload["data"]["rateLimit"]["remaining"] == 4988
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://api.github.com/graphql"
    assert call["json"] == {"query": "query { viewer { login } }", "variables": {"a": 1}}
    assert call["headers"]["Authorization"] == "Bearer hsurr:test"
    assert call["headers"]["Content-Type"] == "application/json"
    # Points noted from the rateLimit block.
    assert transport._graphql_rate_state["remaining"] == 4988


def test_api_graphql_no_credential(clean_env, monkeypatch):
    _install_client(monkeypatch, lambda url, body, headers: _FakeResponse(200, "{}"))
    with pytest.raises(transport.TransportAuthError):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_401_refreshes_once(clean_env, monkeypatch):
    import json as jsonlib

    _install_fake_broker(monkeypatch, ["hsurr:old", "hsurr:new"])
    seen_auth = []

    def handler(url, body, headers):
        seen_auth.append(headers["Authorization"])
        if len(seen_auth) == 1:
            return _FakeResponse(401, "unauthorized")
        return _FakeResponse(200, jsonlib.dumps(_ok_payload()))

    _install_client(monkeypatch, handler)
    status, _payload = transport.api_graphql("query { viewer { login } }")
    assert status == 200
    assert seen_auth == ["Bearer hsurr:old", "Bearer hsurr:new"]


def test_api_graphql_401_twice_raises_transport_error(clean_env, monkeypatch):
    _install_fake_broker(monkeypatch, ["hsurr:old", "hsurr:new"])
    _install_client(monkeypatch, lambda url, body, headers: _FakeResponse(401, "nope"))
    # A persistent 401 after the refresh means the credential itself is
    # rejected: fail fast so the caller falls back to REST.
    with pytest.raises(transport.TransportError):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_timeout_maps(clean_env, monkeypatch):
    _install_fake_broker(monkeypatch, ["hsurr:test"])

    def handler(url, body, headers):
        raise httpx.TimeoutException("too slow")

    _install_client(monkeypatch, handler)
    with pytest.raises(transport.TransportTimeout):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_connect_error_maps(clean_env, monkeypatch):
    _install_fake_broker(monkeypatch, ["hsurr:test"])

    def handler(url, body, headers):
        raise httpx.ConnectError("refused")

    _install_client(monkeypatch, handler)
    with pytest.raises(transport.TransportError):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_429_maps_to_rate_limited(clean_env, monkeypatch):
    _install_fake_broker(monkeypatch, ["hsurr:test"])
    _install_client(monkeypatch, lambda url, body, headers: _FakeResponse(429, "slow down"))
    with pytest.raises(transport.TransportRateLimited):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_non_json_maps(clean_env, monkeypatch):
    _install_fake_broker(monkeypatch, ["hsurr:test"])
    _install_client(monkeypatch, lambda url, body, headers: _FakeResponse(200, "not json"))
    with pytest.raises(transport.TransportError):
        transport.api_graphql("query { viewer { login } }")


def test_api_graphql_errors_array_passes_through(clean_env, monkeypatch):
    """The transport does not interpret the errors array; the caller does."""
    import json as jsonlib

    _install_fake_broker(monkeypatch, ["hsurr:test"])
    payload = {"errors": [{"type": "RATE_LIMITED", "message": "slow"}], "data": None}
    _install_client(
        monkeypatch, lambda url, body, headers: _FakeResponse(200, jsonlib.dumps(payload))
    )
    status, got = transport.api_graphql("query { viewer { login } }")
    assert status == 200
    assert got["errors"][0]["type"] == "RATE_LIMITED"


def test_preemptive_sleep_graphql_triggers_on_low_points(clean_env, monkeypatch):
    import time as timemod

    monkeypatch.setattr(
        transport,
        "_graphql_rate_state",
        {"remaining": 50, "reset": timemod.time() + 60},
    )
    slept = {}
    monkeypatch.setattr(timemod, "sleep", lambda s: slept.setdefault("s", s))
    transport._preemptive_sleep_graphql()
    assert slept["s"] > 60


def test_preemptive_sleep_graphql_quiet_when_healthy(clean_env, monkeypatch):
    import time as timemod

    monkeypatch.setattr(
        transport,
        "_graphql_rate_state",
        {"remaining": 4988, "reset": timemod.time() + 60},
    )
    slept = []
    monkeypatch.setattr(timemod, "sleep", slept.append)
    transport._preemptive_sleep_graphql()
    assert slept == []


def test_preemptive_sleep_graphql_fails_fast_on_distant_reset(clean_env, monkeypatch):
    import time as timemod

    monkeypatch.setattr(
        transport,
        "_graphql_rate_state",
        {"remaining": 5, "reset": timemod.time() + 3600},
    )
    with pytest.raises(transport.TransportRateLimited):
        transport._preemptive_sleep_graphql()


def test_parse_graphql_ts(clean_env):
    assert transport._parse_graphql_ts("2026-10-08T03:00:00Z") is not None
    assert transport._parse_graphql_ts("2026-10-08T03:00:00+00:00") is not None
    assert transport._parse_graphql_ts("garbage") is None
    assert transport._parse_graphql_ts(None) is None


# --- selection -------------------------------------------------------------


def _args(**kw):
    args = argparse.Namespace(graphql=False, persistent_session=False, rest=False)
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def test_use_httpx_graphql_env(clean_env, monkeypatch):
    assert graphql.use_httpx_graphql() is False
    monkeypatch.setenv("TAKEN_GRAPHQL_TRANSPORT", "httpx")
    assert graphql.use_httpx_graphql() is True
    monkeypatch.setenv("TAKEN_GRAPHQL_TRANSPORT", "HTTPX")
    assert graphql.use_httpx_graphql() is True
    monkeypatch.setenv("TAKEN_GRAPHQL_TRANSPORT", "subprocess")
    assert graphql.use_httpx_graphql() is False


def test_fetch_mode_httpx_selection(clean_env, monkeypatch):
    monkeypatch.setattr(checks, "_github_identity", lambda: "someone")
    with mock.patch.dict(os.environ, {"TAKEN_GRAPHQL_TRANSPORT": "httpx"}):
        assert graphql.fetch_mode(_args()) == "httpx"
        assert graphql.fetch_mode(None) == "httpx"
    # The rest escape hatch still wins over the httpx opt-in.
    with mock.patch.dict(os.environ, {"TAKEN_GRAPHQL_TRANSPORT": "httpx", "TAKEN_REST": "1"}):
        assert graphql.fetch_mode(_args()) == "rest"
    # The persistent session still wins over everything.
    with mock.patch.dict(
        os.environ,
        {"TAKEN_GRAPHQL_TRANSPORT": "httpx", "TAKEN_PERSISTENT_SESSION": "1"},
    ):
        assert graphql.fetch_mode(_args()) == "persistent"
    # Explicit opt-in beats the anonymous default.
    monkeypatch.setattr(checks, "_github_identity", lambda: None)
    with mock.patch.dict(os.environ, {"TAKEN_GRAPHQL_TRANSPORT": "httpx"}):
        assert graphql.fetch_mode(_args()) == "httpx"


def test_select_fetch_httpx_uses_transport(clean_env, monkeypatch):
    seen = {}

    def fake_api_graphql(query, variables=None, timeout=60.0):
        seen["query"] = query
        seen["variables"] = variables
        return 200, {"data": {"ok": True}}

    monkeypatch.setattr(transport, "api_graphql", fake_api_graphql)
    fetch = graphql._select_fetch("httpx", None)
    payload = fetch("query Q { x }", {"n": 1})
    assert payload == {"data": {"ok": True}}
    assert seen["query"] == "query Q { x }"
    assert seen["variables"] == {"n": 1}


# --- graphql_via_httpx error mapping ---------------------------------------


def test_graphql_via_httpx_maps_transport_errors(clean_env, monkeypatch):
    def boom(kind):
        def fake(query, variables=None, timeout=60.0):
            raise kind

        return fake

    monkeypatch.setattr(transport, "api_graphql", boom(transport.TransportRateLimited(0)))
    with pytest.raises(checks.RateLimitError):
        graphql.graphql_via_httpx("query Q { x }", {})

    monkeypatch.setattr(transport, "api_graphql", boom(transport.TransportTimeout("slow")))
    with pytest.raises(checks.TakenError):
        graphql.graphql_via_httpx("query Q { x }", {})

    monkeypatch.setattr(transport, "api_graphql", boom(transport.TransportAuthError("nope")))
    with pytest.raises(checks.TakenError):
        graphql.graphql_via_httpx("query Q { x }", {})


def test_graphql_via_httpx_retries_rate_limit(clean_env, monkeypatch):
    calls = {"n": 0}

    def flaky(query, variables):
        calls["n"] += 1
        if calls["n"] < 3:
            raise checks.RateLimitError("points spent")
        return {"data": {"ok": True}}

    monkeypatch.setattr(graphql, "_fetch_via_httpx_transport", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    payload = graphql.graphql_via_httpx("query Q { x }", {})
    assert payload == {"data": {"ok": True}}
    assert calls["n"] == 3


# --- run_checks_with_fallback ----------------------------------------------


def _canned_issue_payload():
    return {
        "data": {
            "repository": {
                "pushedAt": "2026-10-01T00:00:00Z",
                "issue": {
                    "state": "OPEN",
                    "title": "Test issue",
                    "url": "https://github.com/o/r/issues/1",
                    "createdAt": "2026-09-01T00:00:00Z",
                    "author": {"login": "alice"},
                    "assignees": {"nodes": [{"login": "bob"}]},
                    "labels": {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"name": "bug"}],
                    },
                    "comments": {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "author": {"login": "carol"},
                                "body": "looks good to me",
                                "createdAt": "2026-09-02T00:00:00Z",
                                "url": "https://github.com/o/r/issues/1#c1",
                            }
                        ],
                    },
                    "timelineItems": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    },
                },
                "ai1": {"text": "no AI policy here"},
                "mergedPRs": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                },
            },
            "rateLimit": {
                "limit": 5000,
                "cost": 12,
                "remaining": 4988,
                "resetAt": "2026-10-08T03:00:00Z",
            },
        }
    }


def test_run_checks_with_fallback_httpx_success(clean_env, monkeypatch):
    monkeypatch.setattr(
        graphql, "_select_fetch", lambda mode, session: lambda q, v: _canned_issue_payload()
    )
    findings = graphql.run_checks_with_fallback("o", "r", 1, mode="httpx")
    assert findings["transport"] == "httpx"
    assert findings["target"] == "o/r#1"
    assert findings["issue"]["title"] == "Test issue"
    assert findings["issue"]["author"] == "alice"
    assert findings["issue"]["labels"] == ["bug"]
    assert findings["ai_policy"]["verdict"] == "none-found"
    assert set(findings) >= {
        "target",
        "issue",
        "linked_prs",
        "claimants",
        "thresholds",
        "ai_policy",
        "repo_health",
        "scan_truncated",
        "stages_skipped",
        "transport",
    }


def test_run_checks_with_fallback_httpx_falls_back_to_rest(clean_env, monkeypatch):
    def boom(mode, session):
        raise checks.TakenError("boom")

    monkeypatch.setattr(graphql, "_select_fetch", boom)
    rest_findings = {"transport": "rest"}
    monkeypatch.setattr(checks, "run_checks", lambda *a, **k: dict(rest_findings))
    findings = graphql.run_checks_with_fallback("o", "r", 1, mode="httpx")
    assert findings["transport"] == "rest"
    assert "httpx" in findings["transport_fallback"]
    assert "REST" in findings["transport_fallback"]


def test_run_checks_with_fallback_httpx_notfound_reraises(clean_env, monkeypatch):
    def missing(mode, session):
        raise checks.NotFoundError("gone")

    monkeypatch.setattr(graphql, "_select_fetch", missing)
    called = []
    monkeypatch.setattr(checks, "run_checks", lambda *a, **k: called.append(True))
    with pytest.raises(checks.NotFoundError):
        graphql.run_checks_with_fallback("o", "r", 1, mode="httpx")
    assert called == []


def test_run_checks_graphql_shape_parity_httpx_payload(clean_env, monkeypatch):
    """The findings shape is identical regardless of which transport fetched."""
    monkeypatch.setattr(
        graphql, "_select_fetch", lambda mode, session: lambda q, v: _canned_issue_payload()
    )
    findings = graphql.run_checks_graphql("o", "r", 1, mode="httpx")
    assert findings["issue"]["state"] == "open"
    assert findings["issue"]["assignees"] == ["bob"]
    assert findings["linked_prs"] == []
    assert findings["repo_health"]["pushed_recently"] is True
    assert findings["scan_truncated"] == {
        "timeline": False,
        "comments": False,
        "labels": False,
    }
    assert findings["stages_skipped"] == []
