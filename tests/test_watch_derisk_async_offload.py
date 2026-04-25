"""Derisk: FastMCP async tool + anyio.to_thread.run_sync plumbing.

Task 2.5.0.2 — empirically validates that an async FastMCP tool that offloads
blocking work via ``await anyio.to_thread.run_sync(...)`` does NOT freeze the
asyncio event loop, so a second tool call can roundtrip while the first is
still blocked.

This is a pure FastMCP / anyio validation — no pybag, no Debugging Tools, no
real processes. It runs on Windows and Linux alike and does not require
@pytest.mark.integration prerequisites (crash_app, dbgeng). It is tagged
@pytest.mark.integration only so it participates in the same skip-gate
infrastructure as other integration tests; because the gate only fires when
crash_app / dbgeng are absent — conditions that don't apply here — the test
runs by default in ``uv run pytest``.

Observed ping-while-blocked latency (measured during RED→GREEN run):
  ping elapsed: ~0.009 s  (9 ms)  on Windows 11 / Python 3.14
  slow_blocker duration: ~2.0 s

Architecture decision validated:
  RESEARCH.md §3.2 claims async tools offloaded via anyio.to_thread.run_sync
  keep the event loop responsive. This test confirms that claim empirically.
  If this test were to fail (ping > 500 ms), the whole "blocking-poll inside
  an MCP tool" approach would be infeasible and Phase 2.5 would need to
  redesign watch_for_crash as a client-side poll instead.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLOW_BLOCKER_SLEEP_S = 2.0
_PING_MAX_LATENCY_S = 0.5
_INITIAL_TASK_RAMP_S = 0.1  # time to let slow_blocker start before pinging


def _build_test_server() -> FastMCP:
    """Minimal FastMCP with two tools: slow_blocker (async) and ping (sync)."""
    app = FastMCP("derisk-async-offload")

    @app.tool()
    async def slow_blocker() -> str:
        """Offloads a blocking sleep to a thread; must NOT freeze the event loop."""
        await anyio.to_thread.run_sync(lambda: time.sleep(_SLOW_BLOCKER_SLEEP_S))  # type: ignore[attr-defined]
        return "done"

    @app.tool()
    def ping() -> str:
        """Trivial sync tool; roundtrip must complete in < 500 ms even while
        slow_blocker is running."""
        return "pong"

    return app


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_async_tool_with_thread_offload_does_not_block_event_loop() -> None:
    """ping roundtrips in < 500 ms while slow_blocker (~2 s) is still running.

    Steps:
    1. Build a minimal FastMCP with slow_blocker (async, thread-offloaded) and
       ping (sync).
    2. Connect an in-process client via create_connected_server_and_client_session.
    3. Fire slow_blocker as an asyncio background task.
    4. Wait _INITIAL_TASK_RAMP_S to ensure the blocker is truly in-flight.
    5. Call ping and measure wall-clock latency.
    6. Assert ping latency < _PING_MAX_LATENCY_S (0.5 s).
    7. Await blocker completion and assert total wall-clock for blocker >= 1.5 s
       (confirming it actually blocked).
    """

    async def _run() -> tuple[float, float]:
        """Returns (ping_elapsed_s, blocker_elapsed_s)."""
        app = _build_test_server()

        async with create_connected_server_and_client_session(
            app,
            read_timeout_seconds=timedelta(seconds=30),
        ) as client:
            blocker_start = time.perf_counter()

            # Fire slow_blocker as a background asyncio task
            blocker_task: asyncio.Task[object] = asyncio.create_task(
                client.call_tool("slow_blocker", {})
            )

            # Let the blocker task start executing (reach the anyio.to_thread call)
            await asyncio.sleep(_INITIAL_TASK_RAMP_S)

            # Call ping while slow_blocker is still blocked in its thread
            ping_start = time.perf_counter()
            ping_result = await client.call_tool("ping", {})
            ping_elapsed = time.perf_counter() - ping_start

            # Collect the blocker result
            _blocker_result = await blocker_task
            blocker_elapsed = time.perf_counter() - blocker_start

            # Sanity-check content (narrow the MCP content union to TextContent)
            from mcp.types import TextContent

            assert ping_result.isError is False
            first = ping_result.content[0] if ping_result.content else None
            ping_text = first.text if isinstance(first, TextContent) else ""
            assert ping_text == "pong", f"unexpected ping response: {ping_text!r}"

            return ping_elapsed, blocker_elapsed

    ping_elapsed, blocker_elapsed = asyncio.run(_run())

    # Core assertion: event loop was NOT frozen — ping came back quickly
    assert ping_elapsed < _PING_MAX_LATENCY_S, (
        f"ping took {ping_elapsed:.3f} s (>= {_PING_MAX_LATENCY_S} s) — "
        f"event loop was blocked by slow_blocker. "
        f"CRITICAL: anyio.to_thread.run_sync is NOT keeping the loop free. "
        f"Phase 2.5 watch_for_crash architecture is infeasible as designed; "
        f"redesign as client-side poll before proceeding."
    )

    # Sanity: slow_blocker actually ran for roughly the expected duration
    assert blocker_elapsed >= _SLOW_BLOCKER_SLEEP_S * 0.75, (
        f"slow_blocker finished in {blocker_elapsed:.3f} s — "
        f"expected >= {_SLOW_BLOCKER_SLEEP_S * 0.75:.1f} s. "
        f"The sleep may not have been blocking correctly."
    )
