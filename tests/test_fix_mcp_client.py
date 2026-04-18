"""Tests for fix/mcp_client.py — server lifecycle + crash capture. Pure unit — no real server spawned."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from debugbridge.fix.mcp_client import capture_crash, ensure_server_running


def _bind_dummy_listener(host: str, port: int) -> socket.socket:
    """Bind a socket to (host, port) so TCP probe succeeds. Caller closes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    return sock


def test_ensure_server_running_detects_existing() -> None:
    """When something is listening on the port, ensure_server_running returns None
    (meaning: we did not spawn a subprocess) and makes NO Popen call."""
    # Pick a high port unlikely to collide with anything else on the dev box.
    host = "127.0.0.1"
    port = 58585
    listener = _bind_dummy_listener(host, port)
    try:
        with patch("debugbridge.fix.mcp_client.subprocess.Popen") as mock_popen:
            result = ensure_server_running(host=host, port=port)
        assert result is None
        mock_popen.assert_not_called()
    finally:
        listener.close()


def test_ensure_server_running_spawns_when_absent_and_times_out() -> None:
    """When no server is listening and the mocked Popen never emits 'Uvicorn running',
    ensure_server_running raises TimeoutError after a shortened deadline."""
    host = "127.0.0.1"
    port = 58586  # Assume nothing is on this port; if test flakes, try a higher one.

    # Mock Popen so we don't actually spawn uvicorn. The mock's stdout yields
    # irrelevant lines forever, so the readiness scan should hit its deadline.
    fake_stdout = MagicMock()
    fake_stdout.readline.side_effect = [
        "some noise\n",
        "more noise\n",
        "",  # EOF — readline() returns empty string; ensure_server_running should then
        # either break out of the loop or rely on the deadline. Either way it should
        # timeout because "Uvicorn running" never appears.
    ] + [""] * 1000  # Keep returning EOF if called again

    fake_proc = MagicMock()
    fake_proc.stdout = fake_stdout
    fake_proc.poll.return_value = None  # Still "running"

    with (
        patch("debugbridge.fix.mcp_client.subprocess.Popen", return_value=fake_proc) as mock_popen,
        pytest.raises(TimeoutError, match="did not become ready"),
    ):
        # Pass an explicit short deadline so we fail fast instead of waiting 30s.
        ensure_server_running(host=host, port=port, startup_timeout_s=1.0)

    mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# capture_crash tests (task 2a.1.2)
# ---------------------------------------------------------------------------


def _make_mock_tool(name: str) -> MagicMock:
    """Create a mock tool object with the given name.

    MagicMock auto-generates a `.name` attribute that shadows the mock's own
    internal name tracking, so we use a plain object wrapper instead.
    """
    tool = MagicMock()
    tool.name = name
    return tool


def test_capture_crash_tool_presence_check_rejects_foreign_server() -> None:
    """When the MCP server exposes tools that are NOT DebugBridge tools,
    capture_crash must raise an exception whose message contains 'non-DebugBridge'."""

    foreign_tools = [_make_mock_tool(n) for n in ["ping", "pong"]]

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=foreign_tools))

    # Build the nested async-context-manager chain:
    #   async with streamablehttp_client(url) as (read, write, _):
    #       async with ClientSession(read, write) as session:
    mock_transport_ctx = AsyncMock()
    mock_transport_ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock(), MagicMock()))
    mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "debugbridge.fix.mcp_client.streamablehttp_client",
            return_value=mock_transport_ctx,
        ),
        patch(
            "debugbridge.fix.mcp_client.ClientSession",
            return_value=mock_session_ctx,
        ),
        pytest.raises(RuntimeError, match="non-DebugBridge"),
    ):
        capture_crash(pid=0, mcp_url="http://localhost:8585/mcp")


def _route_tool_calls(name: str, arguments: dict | None = None) -> MagicMock:
    """Side-effect function for mock_session.call_tool — routes by tool name."""
    result = MagicMock()
    if name == "attach_process":
        result.structuredContent = {
            "pid": 42,
            "status": "attached",
            "process_name": "test.exe",
            "is_remote": False,
            "message": None,
        }
    elif name == "get_exception":
        result.structuredContent = {
            "code": 0xC0000005,
            "code_name": "EXCEPTION_ACCESS_VIOLATION",
            "address": 0xDEAD,
            "description": "Access violation",
            "is_first_chance": True,
            "faulting_thread_tid": None,
        }
    elif name == "get_callstack":
        result.structuredContent = {
            "result": [
                {
                    "index": 0,
                    "function": "crash_null",
                    "module": "app",
                    "file": "crash.cpp",
                    "line": 42,
                    "instruction_pointer": 0x400000,
                },
            ]
        }
    elif name == "get_threads":
        result.structuredContent = {
            "result": [
                {
                    "id": 0,
                    "tid": 1234,
                    "state": "stopped",
                    "is_current": True,
                    "frame_count": 1,
                },
            ]
        }
    elif name == "get_locals":
        result.structuredContent = {
            "result": [
                {"name": "x", "type": "int", "value": "42"},
            ]
        }
    elif name == "detach_process":
        result.structuredContent = {"status": "detached"}
    else:
        result.structuredContent = None
    return result


def test_capture_crash_builds_crash_capture_from_mcp_responses() -> None:
    """Full capture flow: mock every MCP tool call, verify CrashCapture is correct."""
    tool_names = [
        "attach_process",
        "detach_process",
        "get_exception",
        "get_callstack",
        "get_threads",
        "get_locals",
        "set_breakpoint",
        "step_next",
        "continue_execution",
    ]
    mock_tools = [_make_mock_tool(n) for n in tool_names]

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=mock_tools))
    mock_session.call_tool = AsyncMock(side_effect=_route_tool_calls)

    mock_transport_ctx = AsyncMock()
    mock_transport_ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock(), MagicMock()))
    mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "debugbridge.fix.mcp_client.streamablehttp_client",
            return_value=mock_transport_ctx,
        ),
        patch(
            "debugbridge.fix.mcp_client.ClientSession",
            return_value=mock_session_ctx,
        ),
    ):
        capture = capture_crash(pid=42, mcp_url="http://localhost:8585/mcp")

    # Validate the CrashCapture structure
    assert capture.pid == 42
    assert capture.process_name == "test.exe"
    assert capture.exception is not None
    assert capture.exception.code_name == "EXCEPTION_ACCESS_VIOLATION"
    assert len(capture.callstack) == 1
    assert capture.callstack[0].function == "crash_null"
    assert capture.callstack[0].module == "app"
    assert len(capture.threads) == 1
    assert capture.threads[0].tid == 1234
    assert len(capture.locals_) == 1
    assert capture.locals_[0].name == "x"
    # crash_hash: sha1("EXCEPTION_ACCESS_VIOLATION@app!crash_null")[:8]
    assert len(capture.crash_hash) == 8
    assert all(c in "0123456789abcdef" for c in capture.crash_hash)
