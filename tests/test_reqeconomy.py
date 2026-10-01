from taken import checks


def test_repo_memo_same_key_fetched_once(monkeypatch):
    """Concurrent get() on one key: fn runs exactly once, all waiters
    share the value."""
    import threading
    import time

    memo = checks.RepoMemo()
    calls = []

    def slow_fn():
        calls.append(1)
        time.sleep(0.2)
        return {"v": 1}

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(memo.get("k", slow_fn))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert results == [{"v": 1}] * 8


def test_repo_memo_different_keys_run_in_parallel(monkeypatch):
    """Different keys must not serialize: two slow fetches overlap."""
    import threading

    memo = checks.RepoMemo()
    entered = []
    gate = threading.Event()

    def slow_fn(key):
        def fn():
            entered.append(key)
            if len(entered) == 2:
                gate.set()
            assert gate.wait(timeout=5), f"{key} never overlapped"
            return key

        return fn

    out = {}
    t1 = threading.Thread(target=lambda: out.update(a=memo.get("a", slow_fn("a"))))
    t2 = threading.Thread(target=lambda: out.update(b=memo.get("b", slow_fn("b"))))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert out == {"a": "a", "b": "b"}


def test_repo_memo_failed_fetch_retries():
    """A failed owner fetch must not poison the memo: a waiter retries."""
    memo = checks.RepoMemo()
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return "ok"

    try:
        memo.get("k", flaky)
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert memo.get("k", flaky) == "ok"
    assert len(attempts) == 2
