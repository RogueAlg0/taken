"""Web console discover: partial results when a label search fails (#205).

docs/console/py/webshim.py is a Pyodide browser shim, but it imports
cleanly under CPython, so these tests exercise run_discover_web directly
with the network layer stubbed. The fixture sandboxes webshim's synthetic
`taken` package so it cannot leak into the rest of the test session.
"""

import os
import sys

import pytest

SHIM_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "docs", "console", "py"))


@pytest.fixture
def webshim():
    import importlib.util

    saved = {
        key: sys.modules[key] for key in ("taken", "taken.verdict", "checks") if key in sys.modules
    }
    for key in ("taken", "taken.verdict", "checks"):
        sys.modules.pop(key, None)
    sys.path.insert(0, SHIM_DIR)
    try:
        spec = importlib.util.spec_from_file_location(
            "webshim", os.path.join(SHIM_DIR, "webshim.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webshim"] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.path.remove(SHIM_DIR)
        for key in ("webshim", "taken", "taken.verdict", "checks"):
            sys.modules.pop(key, None)
        sys.modules.update(saved)


def search_item(number):
    return {
        "number": number,
        "repository_url": "https://api.github.com/repos/octo/repo",
        "updated_at": "2026-09-20T12:00:00Z",
    }


def stub_network(monkeypatch, shim, search_behavior, go=True, calls=None):
    """search_behavior maps a label to "fail" or a list of search items."""

    def fake_search(query, per_page=50):
        for label, behavior in search_behavior.items():
            if f'label:"{label}"' in query:
                if behavior == "fail":
                    raise shim.checks.TakenError("rate limited (403)")
                return behavior
        raise AssertionError(f"unexpected query: {query}")

    def fake_run_checks(owner, repo, number, me=None):
        if calls is not None:
            calls.append((owner, repo, number))
        return {
            "repo_health": {"contributors": 5, "pushed_at": ""},
            "issue": {"author": "alice"},
        }

    verdict = shim.GO if go else shim._verdict.TAKEN
    monkeypatch.setattr(shim.checks, "search_issues", fake_search)
    monkeypatch.setattr(shim.checks, "run_checks", fake_run_checks)
    monkeypatch.setattr(shim.checks, "fetch_comments", lambda *a, **k: ([], None))
    monkeypatch.setattr(shim.checks, "friendly_labels", lambda findings: [])
    monkeypatch.setattr(shim.checks, "welcoming_signals", lambda findings: [])
    monkeypatch.setattr(shim, "decide", lambda findings: (verdict, []))


def discover(shim, **kwargs):
    params = {
        "limit": 3,
        "language": None,
        "label": None,
        "min_contributors": 0,
        "me": None,
    }
    params.update(kwargs)
    return shim.run_discover_web(**params)


def test_first_label_fails_keeps_second_label_candidates(monkeypatch, webshim):
    stub_network(
        monkeypatch,
        webshim,
        {"good first issue": "fail", "help wanted": [search_item(7)]},
    )
    out = discover(webshim)
    assert "octo/repo#7" in out
    assert not out.startswith("error: live discover failed")
    assert 'note: search for label "good first issue" failed' in out
    assert "partial results" in out


def test_all_labels_fail_still_errors(monkeypatch, webshim):
    stub_network(
        monkeypatch,
        webshim,
        {"good first issue": "fail", "help wanted": "fail"},
    )
    out = discover(webshim)
    assert out == "error: live discover failed: rate limited (403)"


def test_single_label_failure_is_total_error(monkeypatch, webshim):
    stub_network(monkeypatch, webshim, {"good first issue": "fail"})
    out = discover(webshim, label="good first issue")
    assert out == "error: live discover failed: rate limited (403)"


def test_failed_label_noted_when_nothing_is_go(monkeypatch, webshim):
    stub_network(
        monkeypatch,
        webshim,
        {"good first issue": "fail", "help wanted": [search_item(7)]},
        go=False,
    )
    out = discover(webshim)
    assert out.startswith("no GO candidates found live")
    assert 'note: search for label "good first issue" failed' in out


def test_candidates_deduplicated_across_labels(monkeypatch, webshim):
    calls = []
    stub_network(
        monkeypatch,
        webshim,
        {"good first issue": [search_item(7)], "help wanted": [search_item(7)]},
        calls=calls,
    )
    out = discover(webshim)
    assert "octo/repo#7" in out
    assert calls == [("octo", "repo", 7)]


def test_no_failures_no_notes(monkeypatch, webshim):
    stub_network(
        monkeypatch,
        webshim,
        {"good first issue": [search_item(7)], "help wanted": [search_item(8)]},
    )
    out = discover(webshim)
    assert "note: search for label" not in out
    assert "octo/repo#7" in out
    assert "octo/repo#8" in out
