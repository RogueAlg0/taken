"""--verbose tests: API usage summary goes to stderr, stdout stays clean."""

import json
from datetime import datetime, timezone

import pytest

from taken import checks
from taken.cli import main


class Proc:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


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


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(checks, "gh_api", make_fake())


def test_verbose_summary_goes_to_stderr(fake_api, capsys):
    assert main(["octo/repo#2", "--verbose"]) == 0
    out = capsys.readouterr()
    assert "API usage:" in out.err
    assert "API usage:" not in out.out


def test_verbose_json_stdout_unaffected(fake_api, capsys):
    assert main(["--json", "--verbose", "octo/repo#2"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["verdict"] == "GO"
    assert "API usage:" in out.err


def test_verbose_stats_count_real_calls(monkeypatch):
    """Real gh_api (subprocess mocked): calls and cache hits are counted."""
    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        return Proc(json.dumps({"ok": True}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    checks.reset_api_stats()
    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo/issues/2")
    summary = checks.api_stats_summary()
    assert "API usage: 2 calls, 0 cache hits, 0 cache misses" in summary
    assert "repos/octo/repo: 1 call" in summary
    assert "repos/octo/repo/issues/2: 1 call" in summary


def test_verbose_stats_count_cache_hits(monkeypatch, tmp_path):
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    monkeypatch.setattr(checks, "_IDENTITY", 583231)
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", True)
    checks._MEM_CACHE.clear()

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        return Proc(json.dumps({"ok": True}))

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    checks.reset_api_stats()
    checks.gh_api("repos/octo/repo")
    checks.gh_api("repos/octo/repo")
    summary = checks.api_stats_summary()
    assert "API usage: 1 call, 1 cache hit, 1 cache miss" in summary
