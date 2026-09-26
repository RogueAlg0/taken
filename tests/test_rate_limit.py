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


def test_rate_limit_is_dedicated_error(monkeypatch):
    calls = []
    stub_run(
        monkeypatch,
        [FakeProc(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)")],
        calls,
    )
    with pytest.raises(checks.RateLimitError, match="rate limit exceeded"):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == 1  # hard stop: never retried


def test_rate_limit_429_is_dedicated_error(monkeypatch):
    calls = []
    stub_run(monkeypatch, [FakeProc(1, "", "gh: HTTP 429: too many requests")], calls)
    with pytest.raises(checks.RateLimitError):
        checks.gh_api("repos/octo/repo")
    assert len(calls) == 1


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
        ],
        calls,
    )
    with pytest.raises(checks.RateLimitError) as exc:
        checks.gh_api("repos/octo/repo")
    assert "2026-09-26 04:00:00" in str(exc.value)
    assert "No verdict was recorded" in str(exc.value)


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
