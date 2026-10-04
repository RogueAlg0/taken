"""Tests for issue #318: unguarded datetime.fromisoformat on checks and GraphQL paths."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from taken import checks, graphql


def test_repo_push_info_malformed_timestamp():
    """Malformed pushed_at from GitHub should not raise ValueError."""
    fake_repo_data = {"pushed_at": "not-a-valid-timestamp"}
    with patch.object(checks, "gh_api", return_value=fake_repo_data):
        pushed_at, recent = checks._repo_push_info("owner", "repo")
        assert pushed_at == "not-a-valid-timestamp"
        assert recent is False


def test_repo_push_info_valid_timestamp():
    """Valid pushed_at within window returns recent=True."""
    recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_repo_data = {"pushed_at": recent_ts}
    with patch.object(checks, "gh_api", return_value=fake_repo_data):
        pushed_at, recent = checks._repo_push_info("owner", "repo")
        assert pushed_at == recent_ts
        assert recent is True


def test_repo_recent_merges_malformed_timestamp():
    """Malformed merged_at in PRs should be skipped without raising ValueError."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    prs_page = [
        {"merged_at": "invalid-iso-string"},
        {"merged_at": None},
        {
            "merged_at": (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        },
    ]
    with patch.object(checks, "gh_api", return_value=prs_page):
        merges = checks._repo_recent_merges("owner", "repo", cutoff, pulls_pages=1)
        assert merges == 1


def test_graphql_pushed_recency_malformed_timestamp():
    """Malformed pushedAt in GraphQL payload should return recent=False without raising."""
    repo_payload = {"pushedAt": "2026-99-99T99:99:99Z"}
    pushed_at, recent = graphql._pushed_recency(repo_payload, window_days=30)
    assert pushed_at == "2026-99-99T99:99:99Z"
    assert recent is False


def test_graphql_count_recent_merges_malformed_timestamp():
    """Malformed mergedAt in GraphQL mergedPRs should be skipped without raising."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    repo_payload = {
        "mergedPRs": {
            "nodes": [
                {"mergedAt": "malformed-ts-1"},
                {"mergedAt": None},
                {
                    "mergedAt": (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                },
            ]
        }
    }
    count = graphql._count_recent_merges(repo_payload, cutoff)
    assert count == 1


def test_graphql_oldest_merged_at_malformed_timestamp():
    """Malformed mergedAt in oldest merged node returns fallback datetime.min."""
    payload = {"nodes": [{"mergedAt": "corrupted-date"}]}
    oldest = graphql._oldest_merged_at(payload)
    assert oldest == datetime.min.replace(tzinfo=timezone.utc)
