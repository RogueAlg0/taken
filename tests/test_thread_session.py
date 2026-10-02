"""Thread-local GraphQL sessions (issue #317) and fail-closed fallback (#320)."""

import threading
from unittest import mock

from taken import checks, graphql
from taken.cli import check_one


def test_thread_session_stable_within_thread():
    assert graphql.thread_session() is graphql.thread_session()


def test_thread_session_distinct_across_threads():
    seen = []

    def worker():
        seen.append(graphql.thread_session())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(session) for session in seen}) == 4
    assert all(isinstance(session, graphql.PersistentGraphQLSession) for session in seen)


def test_check_one_persistent_mode_skips_process_singleton():
    captured = {}

    def fake_run_checks(owner, repo, number, **kwargs):
        captured.update(kwargs)
        return {"transport": "persistent"}

    with (
        mock.patch.object(graphql, "run_checks_with_fallback", side_effect=fake_run_checks),
        mock.patch.object(
            graphql, "get_session", side_effect=AssertionError("must not use the shared singleton")
        ),
        mock.patch("taken.cli.decide", return_value=("GO", [])),
    ):
        target, verdict, _reasons, _findings = check_one("o", "r", 1, me=None, mode="persistent")

    assert target == "o/r#1"
    assert verdict == "GO"
    assert isinstance(captured["session"], graphql.PersistentGraphQLSession)


def test_check_one_honors_explicit_session():
    sentinel = object()
    captured = {}

    def fake_run_checks(owner, repo, number, **kwargs):
        captured.update(kwargs)
        return {"transport": "persistent"}

    with (
        mock.patch.object(graphql, "run_checks_with_fallback", side_effect=fake_run_checks),
        mock.patch("taken.cli.decide", return_value=("GO", [])),
    ):
        check_one("o", "r", 1, me=None, mode="persistent", session=sentinel)

    assert captured["session"] is sentinel


def test_persistent_transport_failure_falls_back_to_rest():
    with (
        mock.patch.object(
            graphql, "run_checks_graphql", side_effect=checks.TakenError("connection lost")
        ),
        mock.patch.object(checks, "run_checks", return_value={}),
    ):
        findings = graphql.run_checks_with_fallback("o", "r", 1, mode="persistent")

    assert findings["transport"] == "rest"
    assert "transport_fallback" in findings
    assert "persistent" in findings["transport_fallback"]
