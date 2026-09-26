"""Repo scan tests: `taken owner/repo` discovers open issues automatically."""

import json
from datetime import datetime, timezone

from taken import checks
from taken.cli import main, parse_target


def make_fake(state, issue_numbers, labels_map=None):
    """state maps issue number to 'taken', 'go', or 'boom'.

    labels_map optionally maps issue number to a list of label names.
    """

    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues":
            items = [{"number": n, "title": f"issue {n}"} for n in issue_numbers]
            items.append({"number": 99, "title": "a pr", "pull_request": {"url": "x"}})
            return items
        if "/issues/" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            kind = state.get(number, "go")
            if kind == "boom":
                raise checks.TakenError("simulated `gh` failure")
            if endpoint.endswith("/timeline"):
                return []
            if endpoint.endswith("/comments"):
                return []
            labels = [{"name": name} for name in (labels_map or {}).get(number, [])]
            return {
                "state": "open",
                "title": f"issue {number}",
                "labels": labels,
                "assignees": [{"login": "dk5488"}] if kind == "taken" else [],
                "comments": 0,
                "user": {"login": "someone"},
                "html_url": f"https://github.com/octo/repo/issues/{number}",
                "created_at": "2026-01-01T00:00:00Z",
            }
        if "/contents/" in endpoint:
            raise checks.NotFoundError(endpoint)
        if endpoint.startswith("repos/octo/repo/pulls"):
            return []
        if endpoint == "repos/octo/repo":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"pushed_at": now, "stargazers_count": 4}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fake


def test_parse_target_kinds():
    assert parse_target("octo/repo#123") == ("issue", "octo", "repo", 123)
    assert parse_target("https://github.com/octo/repo/issues/123") == (
        "issue",
        "octo",
        "repo",
        123,
    )
    assert parse_target("octo/repo") == ("repo", "octo", "repo")
    assert parse_target("not a target") is None


def test_scan_checks_each_open_issue(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "taken", 2: "go"}, [1, 2]))
    assert main(["octo/repo"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4  # the PR #99 is filtered out; then a GO recommendation
    assert lines[0].startswith("TAKEN   octo/repo#1")
    assert lines[1].startswith("GO      octo/repo#2")
    assert lines[2] == ""
    assert lines[3] == "1 GO candidate: octo/repo#2"


def test_scan_recommends_multiple_go_candidates(monkeypatch, capsys):
    fake = make_fake(
        {1: "taken", 2: "go", 3: "go"},
        [1, 2, 3],
        labels_map={3: ["good first issue"], 2: ["bug"]},
    )
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["octo/repo"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    # Friendly-labeled issues come first and are annotated.
    assert lines[-1] == "2 GO candidates: octo/repo#3 (good first issue), octo/repo#2"


def test_scan_recommendation_annotates_friendly_labels(monkeypatch, capsys):
    fake = make_fake({1: "go"}, [1], labels_map={1: ["Help Wanted", "docs"]})
    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["octo/repo"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "1 GO candidate: octo/repo#1 (Help Wanted)"


def test_scan_no_go_candidates_says_so(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "taken", 2: "taken"}, [1, 2]))
    assert main(["octo/repo"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "no GO candidates in this scan."


def test_scan_issue_error_does_not_stop_scan(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "boom", 2: "go"}, [1, 2]))
    assert main(["octo/repo"]) == 3
    out = capsys.readouterr()
    assert "octo/repo#1" in out.err
    assert "GO      octo/repo#2" in out.out


def test_scan_limit(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "taken", 2: "go"}, [1, 2]))
    assert main(["--limit", "1", "octo/repo"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("TAKEN")
    assert lines[2] == "no GO candidates in this scan."


def test_scan_label_filter(monkeypatch, capsys):
    seen = {}

    def fake(endpoint, params=None):
        if endpoint == "repos/octo/repo/issues":
            seen["labels"] = (params or {}).get("labels")
        return make_fake({2: "go"}, [2])(endpoint, params)

    monkeypatch.setattr(checks, "gh_api", fake)
    assert main(["--label", "good first issue", "octo/repo"]) == 0
    assert seen["labels"] == "good first issue"
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("GO")
    assert lines[2] == "1 GO candidate: octo/repo#2"


def test_scan_no_open_issues(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({}, []))
    assert main(["octo/repo"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "no open issues found" in out.err


def test_scan_json(monkeypatch, capsys):
    monkeypatch.setattr(checks, "gh_api", make_fake({1: "go"}, [1]))
    assert main(["--json", "octo/repo"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(r["target"], r["verdict"]) for r in data] == [("octo/repo#1", "GO")]
