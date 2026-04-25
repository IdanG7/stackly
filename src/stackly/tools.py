"""MCP tool adapters.

Each function is a thin wrapper over a ``DebugSession`` method. Keeping all
tools in one file (rather than eight files for eight 5-line functions) makes
it easier to scan the full tool surface at once. If a tool grows beyond 20
lines of adapter logic, split it into its own module at that point.

Tools are registered against a ``FastMCP`` instance by :func:`register`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from stackly.models import (
    AttachResult,
    Breakpoint,
    CallFrame,
    ExceptionInfo,
    Local,
    StepResult,
    ThreadInfo,
    WatchResult,
)
from stackly.session import DebugSession


def register(mcp: FastMCP, session: DebugSession) -> None:
    """Wire all Stackly tools onto ``mcp``, backed by ``session``."""

    # ---- Tier A — crash triage ----

    @mcp.tool()
    def attach_process(
        pid: int | None = None,
        process_name: str | None = None,
        conn_str: str | None = None,
    ) -> AttachResult:
        """Attach to a running Windows process.

        Provide either ``pid`` or ``process_name``. Pass ``conn_str`` (e.g.
        ``tcp:server=192.168.1.10,port=5555``) to attach via a remote
        ``dbgsrv.exe``; omit it for a local attach.
        """
        if pid is None and process_name is None:
            return AttachResult(
                pid=0, status="failed", message="Provide either pid or process_name."
            )
        # process_name lookup is local-only; remote process enumeration would
        # need dbgsrv to expose it and pybag doesn't currently wrap that.
        if pid is None and process_name is not None:
            return AttachResult(
                pid=0,
                status="failed",
                message="process_name lookup not yet supported — pass pid.",
            )
        assert pid is not None
        if conn_str:
            return session.attach_remote(conn_str, pid)
        return session.attach_local(pid)

    @mcp.tool()
    def detach_process() -> None:
        """Release the target process without stopping the MCP server.

        Counterpart to ``attach_process``. After this returns, subsequent
        query tools raise "Not attached" until a new ``attach_process`` is
        issued. Intended for clients (e.g. the ``stackly fix`` agent)
        that want to hand the target back to the OS on exit while keeping
        a long-lived server running.
        """
        session.detach()

    @mcp.tool()
    def get_exception() -> ExceptionInfo | None:
        """Return info about the most recent exception on the attached process.

        Returns None if no exception has fired (e.g. a clean break with no
        crash). For the typical crash-triage flow this is the first tool an
        AI client should call after ``attach_process``.
        """
        return session.get_exception()

    @mcp.tool()
    def get_callstack(max_frames: int = 64) -> list[CallFrame]:
        """Return the current thread's call stack.

        Frames include function, module, and (when symbols are available)
        source file + line number. Innermost frame is index 0.
        """
        return session.get_callstack(max_frames=max_frames)

    @mcp.tool()
    def get_threads() -> list[ThreadInfo]:
        """List all threads in the attached process."""
        return session.get_threads()

    @mcp.tool()
    def get_locals(frame_index: int = 0) -> list[Local]:
        """Return local variables visible in a given stack frame.

        Known limitation: DbgEng's expression evaluator does not natively
        render STL containers; ``std::string`` and similar appear as raw
        memory. Primitives, pointers, and POD structs come through correctly.
        """
        return session.get_locals(frame_index=frame_index)

    # ---- Tier B — active debugging ----

    @mcp.tool()
    def set_breakpoint(location: str) -> Breakpoint:
        """Set a breakpoint at ``module!symbol`` or ``file.cpp:42``."""
        return session.set_breakpoint(location)

    @mcp.tool()
    def step_next() -> StepResult:
        """Step over one source line on the current thread."""
        return session.step_over()

    @mcp.tool()
    def continue_execution() -> None:
        """Resume execution until the next event (crash, breakpoint, exit)."""
        session.continue_execution()

    # ---- Tier C — watch / auto-detection ----

    @mcp.tool()
    async def watch_for_crash(
        pid: int,
        poll_s: int = 1,
        timeout_s: int | None = None,
    ) -> WatchResult:
        """Block until a break-worthy exception fires on the attached process.

        Call after attach_process on the same MCP session. Returns a
        WatchResult discriminated union: WatchException on crash,
        WatchTimedOut on deadline expiry, WatchTargetExited on clean exit.

        Poll interval is clamped to pybag's 1-second minimum granularity;
        smaller values are silently raised to 1 second.
        """
        from functools import partial

        import anyio
        return await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            partial(
                session.wait_for_exception,
                pid=pid,
                poll_s=poll_s,
                timeout_s=timeout_s,
            )
        )
