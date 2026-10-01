"""--debug tests: machine-readable JSON report with timings, retries, rate limits."""

import json
import subprocess
from datetime import datetime, timezone

import pytest

from taken import checks
from taken.cli import main


class Proc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_fake():
    """Minimal gh_api fake covering the single-issue check pipeline."""

    def fake(endpoint, params=None):
        if endpoint.endswith("/timeline"):
            return []
        if endpoint.endswith("/comments"):
            return []
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint.startswith("repos/octo/repo/commits"):
            return []
        if endpoint == "repos/octo/repo":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"pushed_at": now, "stargazers_count": 4}
        if "/issues/" in endpoint:
            return {
                "state": "open",
                "title": "issue 2",
                "labels": [],
                "assignees": [],
                "comments": 0,
                "user": {"login": "someone"},
                "html_url": "https://github.com/octo/repo/issues/2",
                "created_at": "2026-01-01T00:00:00Z",
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fake


RATE_LIMIT_JSON = json.dumps(
    {
        "resources": {
            "core": {"limit": 5000, "remaining": 4999, "reset": 1790000000},
            "search": {"limit": 30, "remaining": 29, "reset": 1790000060},
        }
    }
)


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake())
    monkeypatch.setattr(checks, "rate_limit_snapshot", lambda: {"core_remaining": 4999})


def _stderr_json(out):
    """Extract the --debug JSON blob from stderr (it follows the human summary)."""
    start = out.err.index("{")
    return json.loads(out.err[start:])


def test_debug_report_has_all_keys(fake_api, capsys):
    assert main(["octo/repo#2", "--debug"]) == 0
    out = capsys.readouterr()
    report = _stderr_json(out)
    for key in (
        "total_seconds",
        "phases",
        "api_calls",
        "cache_hits",
        "cache_misses",
        "endpoints",
        "bytes_total",
        "retries",
        "backoff_seconds",
        "rate_limit",
    ):
        assert key in report, f"missing key: {key}"
    assert report["rate_limit"]["start"]["core_remaining"] == 4999
    assert report["rate_limit"]["end"]["core_remaining"] == 4999


def test_debug_implies_verbose_summary(fake_api, capsys):
    assert main(["octo/repo#2", "--debug"]) == 0
    out = capsys.readouterr()
    assert "API usage:" in out.err
    assert "API usage:" not in out.out


def test_debug_json_stdout_unaffected(fake_api, capsys):
    assert main(["--json", "--debug", "octo/repo#2"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["verdict"] == "GO"
    _stderr_json(out)  # stderr parses as JSON too


def test_debug_report_printed_on_failure(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise checks.TakenError("boom")

    monkeypatch.setattr("taken.cli.check_one", boom)
    monkeypatch.setattr(checks, "rate_limit_snapshot", lambda: None)
    assert main(["octo/repo#2", "--debug"]) == 3
    out = capsys.readouterr()
    report = _stderr_json(out)
    assert "total_seconds" in report
    assert report["rate_limit"] == {"start": None, "end": None}


def test_debug_report_contains_only_safe_stats():
    # Hermetic: record the stats directly instead of timing a subprocess
    # call. Going through gh_api() mixes a real wall-clock measurement
    # into phases["rest"], which made the exact == 0.5 below flaky under
    # CI load (observed 0.501).
    checks.reset_api_stats()
    checks.record_api_call("repos/octo/repo")
    checks.record_bytes(12)
    checks.record_phase("rest", 0.5)
    checks.record_retry(1.25)
    blob = checks.debug_report(2.5, {"core_remaining": 1}, {"core_remaining": 0})
    lowered = blob.lower()
    for secret_word in ("token", "bearer", "authorization", "password", "secret"):
        assert secret_word not in lowered
    report = json.loads(blob)
    assert report["bytes_total"] > 0
    assert report["phases"]["rest"] == 0.5
    assert report["retries"] == 1
    assert report["backoff_seconds"] == 1.25
    assert report["api_calls"] == 1


def test_retry_and_backoff_recorded(monkeypatch):
    """One throttled attempt then success: retry counted, sleep time tracked."""
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)
    calls = {"n": 0}

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return Proc("", returncode=1, stderr="API rate limit exceeded")
        return Proc(json.dumps({"ok": True}))

    slept = []
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    monkeypatch.setattr(checks.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(checks.random, "uniform", lambda a, b: 0.0)
    checks.reset_api_stats()
    assert checks.gh_api("repos/octo/repo") == {"ok": True}
    data = checks.api_stats_data()
    assert data["retries"] == 1
    assert data["backoff_seconds"] == pytest.approx(sum(slept))
    assert data["backoff_seconds"] > 0


def test_bytes_and_phases_recorded(monkeypatch):
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)
    monkeypatch.setattr(checks.subprocess, "run", lambda *a, **k: Proc(json.dumps({"ok": True})))
    checks.reset_api_stats()
    checks.gh_api("repos/octo/repo")
    data = checks.api_stats_data()
    assert data["bytes_total"] == len(json.dumps({"ok": True}).encode("utf-8"))
    assert data["phases"]["rest"] >= 0
    assert "cache" not in data["phases"]  # cache disabled: no cache phase recorded


def test_rate_limit_snapshot_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(checks.subprocess, "run", boom)
    checks.reset_api_stats()
    assert checks.rate_limit_snapshot() is None


def test_rate_limit_snapshot_parses(monkeypatch):
    monkeypatch.setattr(checks.subprocess, "run", lambda *a, **k: Proc(RATE_LIMIT_JSON))
    checks.reset_api_stats()
    snap = checks.rate_limit_snapshot()
    assert snap["core_remaining"] == 4999
    assert snap["core_limit"] == 5000
    assert snap["search_remaining"] == 29
    assert snap["core_reset"].startswith("2026-")
    data = checks.api_stats_data()
    assert data["calls"] == {"rate_limit": 1}
