"""Tests for issue #503: --limit must reach --health mode.

`taken --health owner/repo --limit 5` used to parse the flag and then
silently ignore it: run_health built HealthOptions without issue_limit,
so the scan always used the 100-issue default. The parser default is
None so each mode applies its own default (20 for scan/discover, 100
for health); an explicit --limit always wins.
"""

from taken import checks, cli, health


def _health_options(monkeypatch, argv):
    """Parse argv, run run_health with a stubbed repo_health, return options."""
    args = cli.build_parser().parse_args(argv)
    captured = {}

    def fake_repo_health(owner, repo, options=None, me=None, now=None):
        captured["options"] = options
        return health.RepoHealth("o", "r")

    monkeypatch.setattr(health, "repo_health", fake_repo_health)
    assert cli.run_health("o", "r", args) == 0
    return captured["options"]


def test_health_limit_forwarded_to_issue_limit(monkeypatch):
    """The exact repro from #503: --limit 5 must cap the health listing at 5."""
    options = _health_options(monkeypatch, ["--health", "o/r", "--limit", "5"])
    assert options.issue_limit == 5


def test_health_limit_defaults_to_100(monkeypatch):
    """Without --limit, --health mode keeps its 100-issue default."""
    options = _health_options(monkeypatch, ["--health", "o/r"])
    assert options.issue_limit == health.DEFAULT_ISSUE_LIMIT == 100


def test_scan_limit_default_stays_20(monkeypatch):
    """The parser default change must not move scan mode off 20."""
    captured = {}

    def fake_list_open_issues(owner, repo, limit=None, label=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(checks, "list_open_issues", fake_list_open_issues)
    args = cli.build_parser().parse_args(["o/r"])
    cli._expand_repo_target("o/r", "o", "r", args, jobs=[])
    assert captured["limit"] == 20


def test_scan_limit_explicit_still_wins(monkeypatch):
    captured = {}

    def fake_list_open_issues(owner, repo, limit=None, label=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(checks, "list_open_issues", fake_list_open_issues)
    args = cli.build_parser().parse_args(["--limit", "7", "o/r"])
    cli._expand_repo_target("o/r", "o", "r", args, jobs=[])
    assert captured["limit"] == 7


def test_discover_limit_default_stays_20():
    """The parser default change must not move --discover mode off 20."""
    args = cli.build_parser().parse_args(["--discover"])
    assert cli._discover_options(args, None).limit == 20


def test_discover_limit_explicit_still_wins():
    args = cli.build_parser().parse_args(["--discover", "--limit", "7"])
    assert cli._discover_options(args, None).limit == 7
