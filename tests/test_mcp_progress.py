"""MCP callers see progress while the synchronous tools are still running."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context

from taken import checks, discover, mcp_server


def context_for(progress):
    session = SimpleNamespace(report_progress=AsyncMock(side_effect=progress))
    request = ServerRequestContext(
        session=session,
        lifespan_context={},
        protocol_version="2025-06-18",
        method="tools/call",
    )
    return Context(request_context=request, mcp_server=mcp_server.mcp)


@pytest.mark.parametrize("failure", [False, True])
def test_scan_reports_progress_before_finishing(monkeypatch, failure):
    events = []

    async def progress(done, total, message=None):
        events.append((done, total))

    def check(owner, repo, number, **kwargs):
        assert events[0] == (0, 2)
        if failure and number == 2:
            raise checks.TakenError("cannot fetch issue")
        return dict(
            target=f"{owner}/{repo}#{number}",
            verdict="GO",
            reasons=[],
            friendly_labels=[],
            welcoming=[],
        )

    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **kw: [{"number": 1}, {"number": 2}])
    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")
    monkeypatch.setattr(mcp_server, "_check_one", check)

    async def run():
        result = await mcp_server.mcp.call_tool(
            "scan_repo", {"owner": "o", "repo": "r"}, context=context_for(progress)
        )
        assert not result.is_error

    asyncio.run(run())
    assert events == [(0, 2), (1, 2), (2, 2)]


def test_discover_forwards_existing_progress_callback(monkeypatch):
    events = []

    async def progress(done, total, message=None):
        events.append((done, total))

    def fake_discover(options):
        assert options.on_progress is not None
        options.on_progress(0, 3)
        options.on_progress(1, 3)
        assert events == [(0, 3), (1, 3)]
        return discover.DiscoverResults([], total=3, verified=1)

    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")
    monkeypatch.setattr(discover, "discover", fake_discover)

    async def run():
        result = await mcp_server.mcp.call_tool(
            "discover_candidates", {}, context=context_for(progress)
        )
        assert not result.is_error

    asyncio.run(run())
    # Ranking may finish early: do not pretend all three candidates were verified.
    assert events == [(0, 3), (1, 3)]


def test_progress_context_is_not_part_of_the_tool_schema():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    for tool in tools:
        assert "ctx" not in tool.input_schema["properties"]


def test_empty_scan_reports_zero_total(monkeypatch):
    events = []

    async def progress(done, total, message=None):
        events.append((done, total))

    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **kw: [])
    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")

    async def run():
        result = await mcp_server.mcp.call_tool(
            "scan_repo", {"owner": "o", "repo": "r"}, context=context_for(progress)
        )
        assert not result.is_error

    asyncio.run(run())
    assert events == [(0, 0)]


def test_sdk_call_outside_request_keeps_progress_optional(monkeypatch):
    def fake_discover(options):
        assert options.on_progress is None
        return discover.DiscoverResults()

    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")
    monkeypatch.setattr(discover, "discover", fake_discover)
    result = asyncio.run(mcp_server.mcp.call_tool("discover_candidates", {}))
    assert not result.is_error


def test_scan_reports_fast_completion_without_reordering_results(monkeypatch):
    events = []
    release = threading.Event()
    mcp_server.budget.activate("test-user")

    async def progress(done, total, message=None):
        events.append((done, total))
        if done == 1:
            release.set()

    def check(owner, repo, number, **kwargs):
        if number == 1:
            assert release.wait(2), "fast second issue must report before the first finishes"
        return dict(
            target=f"{owner}/{repo}#{number}",
            verdict="GO",
            reasons=[],
            friendly_labels=[],
            welcoming=[],
        )

    monkeypatch.setattr(checks, "list_open_issues", lambda *a, **kw: [{"number": 1}, {"number": 2}])
    monkeypatch.setattr(mcp_server.graphql, "fetch_mode", lambda: "rest")
    monkeypatch.setattr(mcp_server, "_check_one", check)

    async def run():
        result = await mcp_server.mcp.call_tool(
            "scan_repo", {"owner": "o", "repo": "r"}, context=context_for(progress)
        )
        assert not result.is_error
        rows = json.loads(result.content[0].text)["results"]
        assert [row["target"] for row in rows] == ["o/r#1", "o/r#2"]

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert events == [(0, 2), (1, 2), (2, 2)]
