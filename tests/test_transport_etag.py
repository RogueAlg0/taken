"""ETag conditional-request tests (phase 2 of the httpx migration).

All hermetic: transport.api_get is faked, the cache lives in a tmp dir,
and no test touches the network. Existing tests under tests/ are
untouched; these cover only the new ETag behavior.
"""

import json

import pytest

from taken import checks, transport


@pytest.fixture
def cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TAKEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(checks, "_CACHE_ENABLED", True)
    # Pre-seed the memoized identity so these tests don't pay for a lookup.
    monkeypatch.setattr(checks, "_IDENTITY", "octocat")
    monkeypatch.setattr(checks, "_IDENTITY_FETCHED", True)
    checks._MEM_CACHE.clear()
    checks.reset_api_stats()


@pytest.fixture
def httpx_env(monkeypatch):
    monkeypatch.setenv("TAKEN_TRANSPORT", "httpx")


def _fake_api_get(responses, seen=None):
    def fake(endpoint, params=None, timeout=60, etag=None):
        if seen is not None:
            seen["etag"] = etag
        return responses.pop(0)

    return fake


def _cache_file_for(tmp_path):
    v2 = tmp_path / "cache" / "v2"
    (cache_file,) = [p for p in v2.iterdir() if p.suffix == ".json"]
    return cache_file


def _expire(cache_file):
    entry = json.loads(cache_file.read_text())
    entry["fetched_at"] -= checks.CACHE_TTL_SECONDS + 1
    cache_file.write_text(json.dumps(entry))
    # Drop the in-memory copy so the stale file entry is actually consulted.
    checks._MEM_CACHE.clear()
    return entry


# --- transport.api_get / _single_get ----------------------------------------


def test_single_get_sends_if_none_match(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200
        text = '{"ok": true}'
        headers = {}

    class FakeClient:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(headers=headers)
            return FakeResp()

    monkeypatch.setattr(transport, "_get_client", lambda: FakeClient())
    status, body, _ = transport._single_get("repos/o/r", None, "Bearer x", 60, etag='"abc"')
    assert status == 200
    assert seen["headers"]["If-None-Match"] == '"abc"'
    assert seen["headers"]["Authorization"] == "Bearer x"


def test_single_get_no_etag_no_conditional_header(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200
        text = '{"ok": true}'
        headers = {}

    class FakeClient:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(headers=headers)
            return FakeResp()

    monkeypatch.setattr(transport, "_get_client", lambda: FakeClient())
    transport._single_get("repos/o/r", None, "Bearer x", 60)
    assert "If-None-Match" not in seen["headers"]


def test_api_get_forwards_etag(monkeypatch):
    seen = {}

    def fake_single_get(e, p, b, t, etag=None):
        seen["etag"] = etag
        return 200, "{}", {}

    monkeypatch.setattr(transport, "_single_get", fake_single_get)
    monkeypatch.setattr(transport, "_pace", lambda path: None)
    monkeypatch.setattr(transport, "_preemptive_sleep", lambda: None)
    monkeypatch.setattr(transport, "_resolve_bearer", lambda: "Bearer test")
    transport.api_get("repos/o/r", etag='"e1"')
    assert seen["etag"] == '"e1"'


# --- _gh_api_run_httpx_impl --------------------------------------------------


def test_impl_returns_data_and_etag(monkeypatch):
    monkeypatch.setattr(
        transport, "api_get", _fake_api_get([(200, '{"ok": true}', {"etag": '"xyz"'})])
    )
    data, etag = checks._gh_api_run_httpx_impl("repos/o/r", None, False)
    assert data == {"ok": True}
    assert etag == '"xyz"'


def test_impl_no_etag_in_response(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"ok": true}', {})]))
    data, etag = checks._gh_api_run_httpx_impl("repos/o/r", None, False)
    assert data == {"ok": True}
    assert etag is None


def test_impl_sends_etag_when_given(monkeypatch):
    seen = {}
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"ok": true}', {})], seen))
    checks._gh_api_run_httpx_impl("repos/o/r", None, False, etag='"abc"')
    assert seen["etag"] == '"abc"'


def test_impl_304_raises_not_modified(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(304, "", {})]))
    with pytest.raises(checks._NotModified):
        checks._gh_api_run_httpx_impl("repos/o/r", None, False, etag='"abc"')


def test_impl_304_not_counted_as_consuming_call(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(304, "", {})]))
    with pytest.raises(checks._NotModified):
        checks._gh_api_run_httpx_impl("repos/o/r", None, False, etag='"abc"')
    assert checks._API_STATS["calls"].get("repos/o/r", 0) == 0


def test_impl_wrapper_keeps_plain_dict_contract(monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"ok": true}', {})]))
    assert checks._gh_api_run_httpx("repos/o/r", None, paced=False) == {"ok": True}


# --- cache entry helpers -----------------------------------------------------


def test_cache_write_stores_etag(cache_env, tmp_path):
    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1}, etag='"e1"')
    entry = json.loads(_cache_file_for(tmp_path).read_text())
    assert entry["etag"] == '"e1"'
    assert entry["data"] == {"v": 1}


def test_cache_write_without_etag(cache_env, tmp_path):
    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1})
    entry = json.loads(_cache_file_for(tmp_path).read_text())
    assert entry["etag"] is None


def test_cache_entry_ignores_ttl(cache_env, tmp_path):
    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1}, etag='"e1"')
    cache_file = _cache_file_for(tmp_path)
    _expire(cache_file)
    # TTL-expired: _cache_read misses, but the raw entry is still there.
    assert checks._cache_read(key) is None
    entry = checks._cache_entry(key)
    assert entry["data"] == {"v": 1}
    assert entry["etag"] == '"e1"'


def test_cache_entry_backward_compatible_no_etag_key(cache_env, tmp_path):
    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1})
    cache_file = _cache_file_for(tmp_path)
    entry = json.loads(cache_file.read_text())
    del entry["etag"]
    cache_file.write_text(json.dumps(entry))
    checks._MEM_CACHE.clear()
    assert checks._cache_entry(key).get("etag") is None


# --- gh_api end to end (hermetic) --------------------------------------------


def test_gh_api_304_serves_stale_and_refreshes_ttl(cache_env, httpx_env, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        transport, "api_get", _fake_api_get([(200, '{"v": 1}', {"etag": '"e1"'})], seen)
    )
    first = checks.gh_api("repos/octo/repo")
    assert first == {"v": 1}
    assert seen["etag"] is None  # first fetch: no ETag yet, plain GET

    cache_file = _cache_file_for(tmp_path)
    old = _expire(cache_file)
    assert old["etag"] == '"e1"'

    monkeypatch.setattr(transport, "api_get", _fake_api_get([(304, "", {})], seen))
    second = checks.gh_api("repos/octo/repo")
    assert second == {"v": 1}  # same parsed JSON as a 200
    assert seen["etag"] == '"e1"'  # conditional request sent

    refreshed = json.loads(cache_file.read_text())
    assert refreshed["fetched_at"] > old["fetched_at"]  # TTL restarted
    assert refreshed["etag"] == '"e1"'
    assert refreshed["data"] == {"v": 1}


def test_gh_api_304_not_counted_as_consuming_call(cache_env, httpx_env, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 1}', {"etag": '"e1"'})]))
    checks.gh_api("repos/octo/repo")
    _expire(_cache_file_for(tmp_path))
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(304, "", {})]))
    checks.gh_api("repos/octo/repo")
    # One counted call (the initial 200); the 304 revalidation is free.
    assert checks._API_STATS["calls"].get("repos/octo/repo", 0) == 1


def test_gh_api_200_after_expiry_updates_etag(cache_env, httpx_env, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 1}', {"etag": '"e1"'})]))
    checks.gh_api("repos/octo/repo")
    _expire(_cache_file_for(tmp_path))
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 2}', {"etag": '"e2"'})]))
    assert checks.gh_api("repos/octo/repo") == {"v": 2}
    entry = json.loads(_cache_file_for(tmp_path).read_text())
    assert entry["etag"] == '"e2"'
    assert entry["data"] == {"v": 2}


def test_gh_api_200_without_etag_clears_stored_etag(cache_env, httpx_env, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 1}', {"etag": '"e1"'})]))
    checks.gh_api("repos/octo/repo")
    _expire(_cache_file_for(tmp_path))
    # Resource changed and the response carries no ETag: the stale ETag
    # must not survive attached to the new body.
    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 2}', {})]))
    assert checks.gh_api("repos/octo/repo") == {"v": 2}
    entry = json.loads(_cache_file_for(tmp_path).read_text())
    assert entry["etag"] is None


def test_gh_api_no_conditional_without_stored_etag(cache_env, httpx_env, tmp_path, monkeypatch):
    seen = {}
    # Prime the cache the legacy way: entry without any etag key.
    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1})
    cache_file = _cache_file_for(tmp_path)
    entry = json.loads(cache_file.read_text())
    del entry["etag"]
    cache_file.write_text(json.dumps(entry))
    _expire(cache_file)

    monkeypatch.setattr(transport, "api_get", _fake_api_get([(200, '{"v": 1}', {})], seen))
    assert checks.gh_api("repos/octo/repo") == {"v": 1}
    assert seen["etag"] is None  # plain GET, no If-None-Match


def test_gh_api_subprocess_path_ignores_etag(cache_env, tmp_path, monkeypatch):
    # Subprocess transport (TAKEN_TRANSPORT unset): ETags are an
    # httpx-transport feature; behavior must be exactly as before.
    monkeypatch.delenv("TAKEN_TRANSPORT", raising=False)
    assert not transport.use_httpx()

    key = checks._cache_key("repos/octo/repo", None)
    checks._cache_write(key, {"v": 1}, etag='"e1"')
    _expire(_cache_file_for(tmp_path))

    calls = []

    class Proc:
        returncode = 0
        stdout = '{"v": 2}'
        stderr = ""

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        return Proc()

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    assert checks.gh_api("repos/octo/repo") == {"v": 2}
    assert len(calls) == 1
    # Plain `gh api` invocation, no conditional-request machinery.
    assert calls[0][:3] == ["gh", "api", "--method"]
