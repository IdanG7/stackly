"""MCP client lifecycle for the fix agent (task 2a.1.1 — server spawn/shutdown half).

This module auto-spawns ``debugbridge serve`` if one isn't already listening on
the requested host:port, then waits for the Streamable-HTTP transport to report
"Uvicorn running" before returning. Shutdown sends SIGBREAK (on Windows) to the
process group and falls back to SIGKILL after a grace period.

The crash-capture call (task 2a.1.2) is intentionally *not* here yet — keeping
the spawn/shutdown half on its own lets us unit-test them without any real
MCP plumbing.

Architectural constraint (PLAN.md decision #1): this module does NOT import
``debugbridge.session``. All debugger-state access goes through MCP.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import subprocess
import sys
import time


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


# TODO(task 2a.1.2): add async capture_crash(pid, mcp_url, conn_str=None) -> CrashCapture
#   - open streamablehttp_client + ClientSession
#   - call initialize()
#   - verify tool set includes attach_process + detach_process (R9 mitigation)
#   - call attach_process, get_exception, get_callstack, get_threads, get_locals
#   - import compute_crash_hash from debugbridge.fix.worktree (lands in 2a.3.1)
#   - return a CrashCapture Pydantic model
