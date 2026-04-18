"""MCP client lifecycle for the fix agent.

Provides server lifecycle (spawn/shutdown) and crash capture via MCP.

Server lifecycle (task 2a.1.1): auto-spawns ``debugbridge serve`` if one isn't
already listening on the requested host:port, then waits for the Streamable-HTTP
transport to report "Uvicorn running" before returning. Shutdown sends SIGBREAK
(on Windows) to the process group and falls back to SIGKILL after a grace period.

Crash capture (task 2a.1.2): connects as an MCP client via streamablehttp_client,
calls attach_process / get_exception / get_callstack / get_threads / get_locals,
and assembles a CrashCapture Pydantic model with crash-hash computed from
``worktree.compute_crash_hash``.

Architectural constraint (PLAN.md decision #1): this module does NOT import
``debugbridge.session``. All debugger-state access goes through MCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from debugbridge.fix.models import CallFrame, CrashCapture, ExceptionInfo, Local, ThreadInfo
from debugbridge.fix.worktree import compute_crash_hash

logger = logging.getLogger(__name__)


def _port_has_listener(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True iff something already accepts TCP connections at (host, port)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def ensure_server_running(
    host: str = "127.0.0.1",
    port: int = 8585,
    startup_timeout_s: float = 30.0,
) -> subprocess.Popen | None:
    """Return a Popen if we spawned ``debugbridge serve``; None if one was already up.

    Raises TimeoutError when we do spawn but the server doesn't emit
    ``"Uvicorn running"`` on stdout within ``startup_timeout_s`` seconds.

    Windows-specific: we launch with CREATE_NEW_PROCESS_GROUP so shutdown can
    deliver CTRL_BREAK_EVENT to the whole group without killing this process.
    """
    if _port_has_listener(host, port):
        return None

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "debugbridge",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )

    deadline = time.monotonic() + startup_timeout_s
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # Either EOF or no output yet. Brief sleep to avoid spinning.
            if proc.poll() is not None:
                raise TimeoutError(
                    f"debugbridge serve exited (rc={proc.returncode}) before emitting 'Uvicorn running'"
                )
            time.sleep(0.05)
            continue
        if "Uvicorn running" in line:
            return proc

    # Timed out waiting for readiness — shoot the process we spawned before raising.
    _terminate(proc)
    raise TimeoutError(
        f"debugbridge serve did not become ready on {host}:{port} within {startup_timeout_s:.1f}s"
    )


def shutdown_server(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    """Send CTRL_BREAK (Windows) or SIGTERM (POSIX) and fall back to kill."""
    if proc.poll() is not None:
        return
    _terminate(proc, grace_s=grace_s)


def _terminate(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass

    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)


_REQUIRED_TOOLS = {"attach_process", "detach_process"}


def _parse_list_result(
    structured: dict | list | None,
) -> list[dict]:
    """Extract a list of dicts from MCP structuredContent.

    Handles both ``{"result": [...]}`` wrapper and bare list shapes.
    Returns empty list on None or unexpected types.
    """
    if structured is None:
        return []
    if isinstance(structured, list):
        return structured
    if isinstance(structured, dict):
        inner = structured.get("result", structured)
        if isinstance(inner, list):
            return inner
    return []


async def _capture_async(
    pid: int,
    mcp_url: str,
    conn_str: str | None = None,
) -> CrashCapture:
    """Connect to a running DebugBridge MCP server and capture crash state.

    Opens a streamablehttp_client connection, verifies we're talking to a real
    DebugBridge server (R9 mitigation), then drives the attach/inspect tool
    sequence and returns a fully-populated CrashCapture.

    Raises RuntimeError if the server is not a DebugBridge instance.
    """
    async with streamablehttp_client(mcp_url) as (read, write, _session_id):  # noqa: SIM117
        async with ClientSession(read, write) as session:
            await session.initialize()

            # R9 mitigation: verify this is actually a DebugBridge server.
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            missing = _REQUIRED_TOOLS - tool_names
            if missing:
                raise RuntimeError(
                    f"Port is bound by a non-DebugBridge MCP server; "
                    f"missing tools: {sorted(missing)}. "
                    f"Pass --port N or stop the other process."
                )

            # 1. Attach to the target process.
            attach_args: dict = {"pid": pid}
            if conn_str is not None:
                attach_args["conn_str"] = conn_str
            attach_result = await session.call_tool("attach_process", attach_args)
            attach_data = attach_result.structuredContent or {}

            process_name = attach_data.get("process_name")
            binary_path = attach_data.get("binary_path")

            # 2. Get exception info (may be None for non-crashed processes).
            exception: ExceptionInfo | None = None
            try:
                exc_result = await session.call_tool("get_exception", {})
                exc_data = exc_result.structuredContent
                if exc_data:
                    exception = ExceptionInfo.model_validate(exc_data)
            except Exception:
                logger.debug("get_exception failed or returned unparseable data", exc_info=True)

            # 3. Get call stack.
            callstack: list[CallFrame] = []
            try:
                stack_result = await session.call_tool("get_callstack", {"max_frames": 32})
                raw_frames = _parse_list_result(stack_result.structuredContent)
                callstack = [CallFrame.model_validate(f) for f in raw_frames]
            except Exception:
                logger.debug("get_callstack failed or returned unparseable data", exc_info=True)

            # 4. Get threads.
            threads: list[ThreadInfo] = []
            try:
                threads_result = await session.call_tool("get_threads", {})
                raw_threads = _parse_list_result(threads_result.structuredContent)
                threads = [ThreadInfo.model_validate(t) for t in raw_threads]
            except Exception:
                logger.debug("get_threads failed or returned unparseable data", exc_info=True)

            # 5. Get locals for frame 0.
            locals_: list[Local] = []
            try:
                locals_result = await session.call_tool("get_locals", {"frame_index": 0})
                raw_locals = _parse_list_result(locals_result.structuredContent)
                locals_ = [Local.model_validate(loc) for loc in raw_locals]
            except Exception:
                logger.debug("get_locals failed or returned unparseable data", exc_info=True)

            # 6. Build the CrashCapture (crash_hash computed below).
            capture = CrashCapture(
                pid=attach_data.get("pid", pid),
                process_name=process_name,
                binary_path=binary_path,
                exception=exception,
                callstack=callstack,
                threads=threads,
                locals_=locals_,
                crash_hash="unknown",  # placeholder — computed next
            )
            capture.crash_hash = compute_crash_hash(capture)
            return capture


def capture_crash(
    pid: int,
    mcp_url: str,
    conn_str: str | None = None,
) -> CrashCapture:
    """Synchronous wrapper around :func:`_capture_async`.

    Connects to the DebugBridge MCP server at ``mcp_url``, attaches to
    process ``pid``, captures crash state, and returns a :class:`CrashCapture`.

    Raises RuntimeError if the server is not a DebugBridge instance (R9).
    """
    return asyncio.run(_capture_async(pid, mcp_url, conn_str))
