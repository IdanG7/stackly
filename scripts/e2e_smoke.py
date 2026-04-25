"""End-to-end smoke test against a running Stackly MCP server.

Launches:
  1. stackly serve (HTTP on 127.0.0.1:8585)
  2. crash_app.exe wait (blocks on stdin)

Then connects as an MCP client, calls list_tools, attach_process, get_threads,
and get_callstack, and prints results. Exits 0 on success, 1 on any failure.

Usage:
  uv run python scripts/e2e_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent.parent
CRASH_APP = ROOT / "tests" / "fixtures" / "crash_app" / "build" / "Debug" / "crash_app.exe"
MCP_URL = "http://127.0.0.1:8585/mcp"


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


@asynccontextmanager
async def spawn_server():
    """Run `stackly serve` in a subprocess and wait for it to be ready."""
    log("starting stackly serve …")
    proc = subprocess.Popen(
        ["uv", "run", "stackly", "serve", "--port", "8585"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    try:
        # Wait for "Uvicorn running" line.
        assert proc.stdout is not None
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            log(f"server: {line.rstrip()}")
            if "Uvicorn running" in line:
                break
        else:
            raise TimeoutError("server did not start within 30s")
        yield proc
    finally:
        log("stopping stackly serve …")
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def spawn_crash_app_waiting() -> subprocess.Popen:
    log(f"launching crash_app wait: {CRASH_APP}")
    p = subprocess.Popen(
        [str(CRASH_APP), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    # Wait for the PID line so we know main() is running.
    assert p.stdout is not None
    line = p.stdout.readline().decode(errors="replace")
    log(f"crash_app: {line.rstrip()}")
    if "crash_app pid=" not in line:
        raise RuntimeError(f"unexpected crash_app output: {line!r}")
    time.sleep(0.3)
    return p


async def run_client_flow(pid: int) -> int:
    """Open MCP connection, drive tools, return exit code."""
    try:
        async with (
            streamablehttp_client(MCP_URL) as (read, write, _session_id_cb),
            ClientSession(read, write) as session,
        ):
            log("initializing MCP session …")
            await session.initialize()

            log("listing tools …")
            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            log(f"tools exposed: {tool_names}")
            expected = {
                "attach_process",
                "continue_execution",
                "detach_process",
                "get_callstack",
                "get_exception",
                "get_locals",
                "get_threads",
                "set_breakpoint",
                "step_next",
                "watch_for_crash",
            }
            missing = expected - set(tool_names)
            if missing:
                log(f"FAIL: missing tools {missing}")
                return 1

            log(f"calling attach_process(pid={pid}) …")
            attach = await session.call_tool("attach_process", {"pid": pid})
            log(f"attach.content: {attach.content}")
            log(f"attach.structuredContent: {attach.structuredContent}")
            status = (attach.structuredContent or {}).get("status")
            if status != "attached":
                log(f"FAIL: attach_process returned {status!r}")
                return 1

            log("calling get_threads …")
            threads = await session.call_tool("get_threads", {})
            log(f"threads.structuredContent: {threads.structuredContent}")

            log("calling get_callstack …")
            stack = await session.call_tool("get_callstack", {"max_frames": 10})
            log(f"callstack.structuredContent: {stack.structuredContent}")

            log("SUCCESS — all tool calls completed")
            return 0
    except Exception as e:
        log(f"FAIL: exception during client flow: {e!r}")
        import traceback

        traceback.print_exc()
        return 1


async def main() -> int:
    if not CRASH_APP.exists():
        log(f"crash_app not built at {CRASH_APP}")
        log("  run: tests/fixtures/crash_app/build.ps1")
        return 1

    async with spawn_server():
        await asyncio.sleep(1)  # settle
        crash = spawn_crash_app_waiting()
        try:
            exit_code = await run_client_flow(crash.pid)
        finally:
            # DbgEng holds the attached process; detach via another MCP call
            # before killing so Windows will honor SIGKILL. In practice we
            # just stop the server (which detaches implicitly) and then kill.
            pass

    # Server is now stopped → DbgEng released crash_app. Safe to kill.
    if crash.poll() is None:
        log("killing crash_app …")
        crash.kill()
        try:
            crash.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("crash_app did not exit in 5s; leaving it for the OS to reap")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
