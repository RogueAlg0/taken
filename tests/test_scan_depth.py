"""Scan-depth tests: timeline and comment scans must page past the first 100
results, so a busy issue cannot hide a linked PR or a claimant comment on a
later page and silently earn a GO."""

import subprocess

import pytest

from taken import checks


def _timeline_event(url):
    return {"event": "cross-referenced", "source": {"issue": {"html_url": url}}}


def test_timeline_finds_pr_on_page_two(monkeypatch):
    seen_pages = []

    def fake(endpoint, params=None):
        if endpoint.endswith("/timeline"):
            page = int((params or {}).get("page", "1"))
            seen_pages.append(page)
            if page == 1:
                return [{"event": "labeled"}] * 100
            return [_timeline_event("https://github.com/octo/repo/pull/7")]
        return {
            "number": 7,
            "title": "fix",
            "state": "open",
            "merged_at": None,
            "user": {"login": "someone"},
            "html_url": "https://github.com/octo/repo/pull/7",
        }

    monkeypatch.setattr(checks, "gh_api", fake)
    linked = checks.check_timeline("octo", "repo", 1)
    assert seen_pages == [1, 2]
    assert [pr["number"] for pr in linked] == [7]


def test_timeline_stops_on_a_short_page(monkeypatch):
    calls = []

    def fake(endpoint, params=None):
        calls.append((params or {}).get("page"))
        return [{"event": "labeled"}] * 30

    monkeypatch.setattr(checks, "gh_api", fake)
    checks.check_timeline("octo", "repo", 1)
    assert calls == ["1"]


def test_comment_scan_finds_claimant_on_page_two(monkeypatch):
    def fake(endpoint, params=None):
        if endpoint.endswith("/comments"):
            page = int((params or {}).get("page", "1"))
            if page == 1:
                return [
                    {
                        "user": {"login": "user%d" % i},
                        "body": "nice idea",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                    for i in range(100)
                ]
            return [
                {
                    "user": {"login": "volunteer"},
                    "body": "Please assign this issue to me",
                    "created_at": "2026-01-02T00:00:00Z",
                    "html_url": "https://github.com/octo/repo/issues/1#issuecomment-1",
                }
            ]
        raise AssertionError("unexpected endpoint " + endpoint)

    monkeypatch.setattr(checks, "gh_api", fake)
    hits = checks.check_claimants("octo", "repo", 1)
    assert [hit["author"] for hit in hits] == ["volunteer"]


def test_bad_page_fails_closed(monkeypatch):
    def fake(endpoint, params=None):
        if endpoint.endswith("/timeline"):
            return None  # API returned JSON null on page 1
        raise AssertionError("unexpected endpoint " + endpoint)

    monkeypatch.setattr(checks, "gh_api", fake)
    with pytest.raises(checks.TakenError):
        checks.check_timeline("octo", "repo", 1)


def test_not_found_message_names_the_resource(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "Not Found (HTTP 404)")

    monkeypatch.setattr(checks, "_CACHE_ENABLED", False)
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    with pytest.raises(checks.NotFoundError, match="not found"):
        checks.gh_api("repos/octo/repo/issues/1")
