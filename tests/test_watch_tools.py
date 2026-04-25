"""Tests for the watch_for_crash MCP tool (task 2.5.1.3). Pure unit — no pybag, no DbgEng."""

from __future__ import annotations

import asyncio
from functools import partial
from unittest.mock import MagicMock, patch

from mcp.server.fastmcp import FastMCP

from stackly import tools
from stackly.models import WatchTargetExited
from stackly.session import DebugSession


def _build_mcp_with_tools() -> tuple[FastMCP, DebugSession]:
    """Create a FastMCP instance and register all DebugBridge tools on it."""
    mcp = FastMCP("test-watch-tools")
    session = MagicMock(spec=DebugSession)
    tools.register(mcp, session)
    return mcp, session


def test_watch_for_crash_is_async_and_offloads_blocking_work() -> None:
    """watch_for_crash must be registered as an async tool.

    A sync tool would freeze the FastMCP event loop for the entire duration of
    the blocking poll loop inside session.wait_for_exception.
    """
    mcp, _session = _build_mcp_with_tools()
    tm = mcp._tool_manager

    assert "watch_for_crash" in tm._tools, (
        "watch_for_crash tool not registered — was tools.register() updated?"
    )

    tool_obj = tm._tools["watch_for_crash"]

    # FastMCP records is_async=True for async def functions
    assert tool_obj.is_async is True, (
        "watch_for_crash must be 'async def' — a sync tool blocks the event loop"
    )

    # Double-check via asyncio inspection of the underlying function
    assert asyncio.iscoroutinefunction(tool_obj.fn) is True, (
        "watch_for_crash.fn must be a coroutine function"
    )


def test_watch_for_crash_calls_session_wait_for_exception_via_thread_offload() -> None:
    """watch_for_crash delegates to session.wait_for_exception through anyio.to_thread.run_sync.

    The anyio offload is critical — without it, the blocking poll loop inside
    wait_for_exception would freeze the FastMCP asyncio event loop.
    """
    mcp, session = _build_mcp_with_tools()

    canned_result = WatchTargetExited(elapsed_s=5.0)

    run_sync_calls: list[partial] = []

    async def fake_run_sync(fn, *args, **kwargs):
        run_sync_calls.append(fn)
        return canned_result

    with patch("anyio.to_thread.run_sync", new=fake_run_sync):
        # Extract the tool's underlying async function and invoke it directly
        tool_obj = mcp._tool_manager._tools["watch_for_crash"]
        tool_fn = tool_obj.fn

        result = asyncio.run(tool_fn(pid=1234, poll_s=2, timeout_s=10))

    # run_sync must have been called exactly once
    assert len(run_sync_calls) == 1, (
        f"anyio.to_thread.run_sync called {len(run_sync_calls)} times, expected 1"
    )

    offloaded = run_sync_calls[0]

    # The offloaded argument must be a functools.partial whose .func is
    # session.wait_for_exception
    assert isinstance(offloaded, partial), (
        f"Expected a functools.partial, got {type(offloaded)!r}"
    )
    assert offloaded.func is session.wait_for_exception, (
        f"partial.func must be session.wait_for_exception, got {offloaded.func!r}"
    )

    # Keywords must carry through unchanged
    assert offloaded.keywords == {"pid": 1234, "poll_s": 2, "timeout_s": 10}, (
        f"partial.keywords mismatch: {offloaded.keywords!r}"
    )

    # The tool must return the value run_sync produced
    assert result == canned_result
