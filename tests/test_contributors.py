"""Tests for the contributor-breadth health signal."""

from taken import checks


def commit(login=None, email=None):
    entry = {"commit": {"author": {"email": email or "dev@example.com"}}}
    entry["author"] = {"login": login} if login is not None else None
    return entry


def make_fake(pages):
    calls = []

    def fake(endpoint, params=None):
        calls.append((endpoint, params))
        page = int((params or {}).get("page", "1"))
        return pages[page - 1] if page - 1 < len(pages) else []

    fake.calls = calls
    return fake


def test_counts_distinct_logins_once(monkeypatch):
    fake = make_fake([[commit("alice"), commit("alice"), commit("bob")]])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 2


def test_excludes_bots(monkeypatch):
    fake = make_fake([[commit("alice"), commit("github-actions[bot]"), commit("dependabot[bot]")]])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 1


def test_falls_back_to_email_without_github_user(monkeypatch):
    fake = make_fake([[commit(None, "solo@example.com"), commit("alice")]])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 2


def test_empty_history_counts_zero(monkeypatch):
    fake = make_fake([[]])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 0


def test_stops_after_short_page(monkeypatch):
    fake = make_fake([[commit("alice")]])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 1
    assert len(fake.calls) == 1  # no second page fetched


def test_caps_at_three_pages(monkeypatch):
    full = [commit(f"dev{i}") for i in range(100)]
    fake = make_fake([full, full, full, full])
    monkeypatch.setattr(checks, "gh_api", fake)
    assert checks.count_recent_contributors("octo", "repo") == 100
    assert len(fake.calls) == 3


def test_repo_health_reports_contributors(monkeypatch):
    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo":
            return {"pushed_at": "2026-09-26T10:00:00Z"}
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint.startswith("repos/octo/repo/commits"):
            return [commit("alice"), commit("bob")]
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(checks, "gh_api", fake)
    health = checks.check_repo_health("octo", "repo")
    assert health["contributors"] == 2
    assert health["contributors_window_days"] == checks.CONTRIBUTORS_WINDOW_DAYS
    assert "stars" not in health
