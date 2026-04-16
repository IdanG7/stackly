"""Tests for fix/mcp_client.py server-lifecycle half. Pure unit — no real server spawned."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from debugbridge.fix.mcp_client import ensure_server_running


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
