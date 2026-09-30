"""Rate-limit and retry behavior of the `gh` subprocess transport."""

import pytest

from taken import checks


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def stub_run(monkeypatch, script, calls):
    """Stub checks.subprocess.run: pop one FakeProc per call, record calls."""

    def fake_run(*args, **kwargs):
        calls.append(args)
        return script.pop(0)

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)
    monkeypatch.setattr(checks.time, "sleep", lambda seconds: None)


def test_rate_limit_retried_then_raises(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [FakeProc(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)")] * 10,
        calls,
    )
    with pytest.raises(checks.RateLimitError, match="rate limit exceeded"):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == checks.RETRY_ATTEMPTS  # bounded retries, then give up


def test_rate_limit_429_retried_then_succeeds(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [
            FakeProc(1, "", "gh: HTTP 429: too many requests"),
            FakeProc(0, '{"ok": true}', ""),
        ],
        calls,
    )
    assert checks.gh_api("repos/octo/repo") == {"ok": True}
    assert len(calls) == 2


def test_rate_limit_message_includes_reset_time(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [
            FakeProc(
                1,
                "",
                "gh: API rate limit exceeded. This will reset at 2026-09-26 04:00:00 UTC.",
            )
        ]
        * 10,
        calls,
    )
    with pytest.raises(checks.RateLimitError) as exc:
        checks.gh_api("repos/octo/repo")
    assert "2026-09-26 04:00:00" in str(exc.value)
    assert "No verdict was recorded" in str(exc.value)
    assert len(calls) == checks.RETRY_ATTEMPTS


def test_secondary_rate_limit_message_distinguishes(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [
            FakeProc(
                1,
                "",
                "gh: You have exceeded a secondary rate limit. "
                "Please wait a few minutes before you try again. (HTTP 403)",
            )
        ]
        * 10,
        calls,
    )
    with pytest.raises(checks.RateLimitError) as exc:
        checks.gh_api("repos/octo/repo")
    message = str(exc.value)
    assert "secondary rate limit" in message
    assert "not shown by `gh api rate_limit`" in message
    assert "Check `gh api rate_limit`" not in message
    assert len(calls) == checks.RETRY_ATTEMPTS


def test_retry_after_directive_is_honored(monkeypatch):
    calls = []
    sleeps = []
    stub_run(
        monkeypatch,
        [
            FakeProc(1, "", "gh: HTTP 429: too many requests. Retry-After: 45"),
            FakeProc(0, '{"ok": true}', ""),
        ],
        calls,
    )
    monkeypatch.setattr(checks.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert checks.gh_api("repos/octo/repo") == {"ok": True}
    assert sleeps == [45.0]


def test_retry_after_directive_is_capped(monkeypatch):
    assert checks._retry_after_seconds("Retry-After: 3600") == 120.0
    assert checks._retry_after_seconds("no directive here") is None


def test_transient_5xx_retried_then_succeeds(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [
            FakeProc(1, "", "gh: Internal Server Error (HTTP 500)"),
            FakeProc(0, '{"ok": true}', ""),
        ],
        calls,
    )
    assert checks.gh_api("repos/octo/repo") == {"ok": True}
    assert len(calls) == 2


def test_persistent_5xx_raises_after_bounded_retries(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [FakeProc(1, "", "gh: Bad Gateway (HTTP 502)")] * 10,
        calls,
    )
    with pytest.raises(checks.TakenError, match="failed"):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == checks.RETRY_ATTEMPTS


def test_non_transient_error_not_retried(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [FakeProc(1, "", "gh: HTTP 403: Resource not accessible by integration")],
        calls,
    )
    with pytest.raises(checks.TakenError, match="failed"):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == 1


def test_rate_limit_error_is_a_taken_error():
    assert issubclass(checks.RateLimitError, checks.TakenError)


def test_rate_limit_id_containing_404_digits_not_misclassified(monkeypatch):
    """A rate-limit message citing an ID like 40412 must not become NotFoundError."""
    calls = []
    stub_run(
        monkeypatch,
        [FakeProc(1, "", "gh: API rate limit exceeded for installation ID 40412. (HTTP 403)")] * 10,
        calls,
    )
    with pytest.raises(checks.RateLimitError, match="rate limit exceeded"):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == checks.RETRY_ATTEMPTS


def pace_setup(monkeypatch, interval=30.0):
    """Small interval, clean pacing state, recorded sleeps."""
    sleeps = []
    monkeypatch.setattr(checks.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(checks, "SEARCH_MIN_INTERVAL", interval)
    monkeypatch.setattr(checks, "_last_search_at", 0.0)
    return sleeps


def test_search_calls_are_paced(monkeypatch):
    calls = []
    stub_run(monkeypatch, [FakeProc(0, '{"items": []}', "")] * 2, calls)
    sleeps = pace_setup(monkeypatch)
    checks.gh_api("search/issues", {"q": "x"})
    checks.gh_api("search/issues", {"q": "x"})
    assert len(calls) == 2
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_search_retry_attempts_are_paced(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [
            FakeProc(1, "", "gh: You have exceeded a secondary rate limit. (HTTP 403)"),
            FakeProc(0, '{"items": []}', ""),
        ],
        calls,
    )
    sleeps = pace_setup(monkeypatch)
    checks.gh_api("search/issues", {"q": "x"})
    assert len(calls) == 2
    assert any(s >= 29.0 for s in sleeps)  # retry attempt waited out the interval


def test_non_search_calls_are_not_paced(monkeypatch):
    calls = []
    stub_run(monkeypatch, [FakeProc(0, '{"ok": true}', "")] * 2, calls)
    sleeps = pace_setup(monkeypatch)
    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo")
    assert len(calls) == 2
    assert sleeps == []


def test_pace_search_skips_wait_when_interval_elapsed(monkeypatch):
    sleeps = []
    monkeypatch.setattr(checks.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(checks, "SEARCH_MIN_INTERVAL", 2.0)
    monkeypatch.setattr(checks, "_last_search_at", checks.time.monotonic() - 10.0)
    checks._pace_search()
    assert sleeps == []
