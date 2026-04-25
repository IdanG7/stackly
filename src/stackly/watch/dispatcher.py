"""Watch command dispatcher — monitors a process for crashes and auto-invokes fix.

Attaches to a live process via the Stackly MCP server, calls the
``watch_for_crash`` MCP tool (which blocks server-side until a break-worthy
exception fires), then dispatches the Phase 2a fix agent (``run_handoff`` or
``run_autonomous``) exactly as if the developer had run ``stackly fix`` by hand.

Architecture constraint (PLAN.md decision #1): this module does NOT import
``stackly.session``. All debugger-state access goes through MCP.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from rich.progress import Progress, SpinnerColumn, TextColumn

from stackly.fix.dispatcher import run_autonomous, run_handoff
from stackly.fix.mcp_client import ensure_server_running, shutdown_server
from stackly.fix.models import CrashCapture
from stackly.fix.worktree import compute_crash_hash, ensure_gitignore
from stackly.models import (
    WatchException,
    WatchResult,
    WatchTargetExited,
    WatchTimedOut,
)

log = logging.getLogger(__name__)


class AttachFailedError(Exception):
    """Raised by _watch_once when attach_process returns status='failed'.

    Caught by run_watch's stay-resident loop to exit cleanly instead of retrying.
    """


@dataclass
class _WatchState:
    """Mutable state shared between the stay-resident loop and signal handlers."""

    claude_proc: subprocess.Popen | None = None
    server_proc: subprocess.Popen | None = None
    did_spawn_server: bool = False
    _handled: bool = field(default=False, repr=False)


async def _detach_via_mcp(mcp_url: str) -> None:
    """Best-effort detach: open a short-lived MCP session and call detach_process.

    Wraps the entire attempt in a try/except — if the target is already gone or
    the server is unreachable, the exception is swallowed so the signal handler
    can still proceed to exit.
    """
    try:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=True,
        )

        def _factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return http_client

        async with (
            streamablehttp_client(mcp_url, httpx_client_factory=_factory) as (read, write, _),
            ClientSession(read, write) as s,
        ):
            await s.initialize()
            await s.call_tool("detach_process", {})
    except Exception:
        log.debug("best-effort MCP detach failed (target may already be gone)", exc_info=True)


def _detach_in_background_thread(mcp_url: str, timeout_s: float = 5.0) -> None:
    """Run ``_detach_via_mcp`` in a freshly-spawned thread with its own event loop.

    Why: the SIGINT handler fires on the main thread while ``run_watch`` is
    already inside an active ``asyncio.run(_watch_once(...))`` call. Calling
    ``asyncio.run()`` a second time from the same thread raises
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``,
    which silently breaks the best-effort detach (Codex review P1).

    A fresh thread has no running loop, so ``asyncio.run`` there is safe. We
    join with a bounded timeout so a stuck detach never blocks process exit.
    """

    def _worker() -> None:
        try:
            asyncio.run(_detach_via_mcp(mcp_url))
        except Exception:
            log.debug("detach thread raised", exc_info=True)

    thread = threading.Thread(target=_worker, name="stackly-watch-detach", daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    if thread.is_alive():
        log.debug("detach thread did not finish within %.1fs; exiting anyway", timeout_s)


def _install_watch_signal_handlers(state: _WatchState, mcp_url: str) -> None:
    """Install SIGINT (and SIGBREAK on Windows) handlers for clean shutdown.

    The handler:
    1. Is idempotent — second invocation is a no-op.
    2. Terminates any in-flight claude subprocess.
    3. Best-effort MCP detach in a fresh thread (avoids nested ``asyncio.run``).
    4. Shuts down the MCP server if we spawned it.
    5. Raises SystemExit(130).

    Mirrors the pattern in fix/dispatcher.py:_install_signal_handlers.
    """

    def handler(signum: int, frame: object) -> None:
        if state._handled:
            return
        state._handled = True

        # Terminate any in-flight claude subprocess
        if state.claude_proc is not None and state.claude_proc.poll() is None:
            state.claude_proc.terminate()
            try:
                state.claude_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                state.claude_proc.kill()

        # Best-effort MCP detach in a dedicated thread so we don't nest
        # ``asyncio.run`` on a thread that's already inside one.
        _detach_in_background_thread(mcp_url, timeout_s=5.0)

        # Shut down server if we spawned it
        if state.did_spawn_server and state.server_proc is not None:
            shutdown_server(state.server_proc)

        raise SystemExit(130)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]


def _hash_from_watch_exception(exc: WatchException) -> str:
    """Compute a crash hash from a WatchException for stay-resident dedup.

    Builds a minimal CrashCapture (no callstack) and delegates to
    ``compute_crash_hash`` from fix/worktree so the formula is identical
    to the one used by capture_crash on the server side.
    """
    capture = CrashCapture(
        pid=0,
        exception=exc.exception,
        callstack=[],
        threads=[],
        locals_=[],
        crash_hash="placeholder",
    )
    return compute_crash_hash(capture)


async def _watch_once(
    pid: int,
    mcp_url: str,
    poll_s: int,
    timeout_s: int | None,
    conn_str: str | None,
) -> WatchResult:
    """Open an MCP session, attach, call watch_for_crash, return the parsed result.

    Uses an httpx.AsyncClient with read=None (unbounded read timeout) so
    long-running watches don't get cut off by the default 300-second limit
    (Architecture Decision #5).

    Raises:
        AttachFailedError: when attach_process returns status='failed'. This is
            caught by run_watch's stay-resident loop to exit cleanly (Wave 0
            finding: re-attach to a crashed PID always fails on pybag 2.2.16 +
            Windows 11).
    """
    from pydantic import TypeAdapter

    from stackly.models import AttachResult

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None),
        follow_redirects=True,
    )

    # streamablehttp_client takes httpx_client_factory matching McpHttpClientFactory protocol.
    # We return our pre-configured client that has read=None timeout.
    def _factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return http_client

    async with (
        streamablehttp_client(
            mcp_url,
            httpx_client_factory=_factory,
        ) as (read, write, _),
        ClientSession(read, write) as s,
    ):
        await s.initialize()

        attach_args: dict = {"pid": pid}
        if conn_str is not None:
            attach_args["conn_str"] = conn_str
        attach_raw = await s.call_tool("attach_process", attach_args)
        if attach_raw.structuredContent and isinstance(attach_raw.structuredContent, dict):
            content = attach_raw.structuredContent
            # Only interpret as AttachResult if it carries the expected fields.
            if "status" in content:
                attach_result = AttachResult.model_validate(content)
                if attach_result.status == "failed":
                    raise AttachFailedError(
                        attach_result.message or "attach_process returned status='failed'"
                    )

        result = await s.call_tool(
            "watch_for_crash",
            {"pid": pid, "poll_s": poll_s, "timeout_s": timeout_s},
            read_timeout_seconds=timedelta(days=30),
        )
        return TypeAdapter(WatchResult).validate_python(result.structuredContent)


def run_watch(
    repo: Path,
    pid: int,
    host: str,
    port: int,
    auto: bool,
    build_cmd: str | None,
    test_cmd: str | None,
    model: str | None,
    max_attempts: int,
    conn_str: str | None,
    max_crashes: int,
    max_wait_minutes: int | None,
    quiet: bool,
    poll_seconds: int = 1,
) -> int:
    """Synchronous entry point. Runs the watch loop, dispatches on crash.

    Returns 0 on clean exit (timed out, target exited, or crash(es) handled).

    Parameters
    ----------
    poll_seconds:
        Poll interval in seconds passed to ``watch_for_crash``. Clamped to 1s
        minimum server-side (pybag floor). Wired through from the ``--poll-seconds``
        CLI flag (Codex review P2).
    """
    ensure_gitignore(repo)
    server_proc = ensure_server_running(host, port)
    mcp_url = f"http://{host}:{port}/mcp"
    timeout_s = max_wait_minutes * 60 if max_wait_minutes is not None else None

    # Signal handler state — constructed before installing handlers.
    state = _WatchState(
        server_proc=server_proc,
        did_spawn_server=server_proc is not None,
    )
    _install_watch_signal_handlers(state, mcp_url)

    # Dedup tracking for stay-resident mode (max_crashes > 1).
    last_crash_hash: str | None = None

    try:
        for _crash_idx in range(max_crashes):
            try:
                if quiet:
                    log.info("[stackly] watching pid %s...", pid)
                    watch_result = asyncio.run(
                        _watch_once(
                            pid=pid,
                            mcp_url=mcp_url,
                            poll_s=poll_seconds,
                            timeout_s=timeout_s,
                            conn_str=conn_str,
                        )
                    )
                else:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                    ) as progress:
                        task_id = progress.add_task(
                            f"waiting for crash on pid {pid}", total=None
                        )
                        watch_result = asyncio.run(
                            _watch_once(
                                pid=pid,
                                mcp_url=mcp_url,
                                poll_s=poll_seconds,
                                timeout_s=timeout_s,
                                conn_str=conn_str,
                            )
                        )
                        progress.update(task_id, description=f"crash detected on pid {pid}")
            except AttachFailedError as exc:
                # Wave 0 finding: re-attach to a crashed PID always fails.
                # Exit cleanly instead of retrying.
                log.info("target no longer attachable: %s", exc)
                break

            if isinstance(watch_result, WatchException):
                # Dedup check (stay-resident only).
                if max_crashes > 1 and last_crash_hash is not None:
                    current_hash = _hash_from_watch_exception(watch_result)
                    if current_hash == last_crash_hash:
                        log.info("already seen this crash, skipping dispatch")
                        continue

                # Dispatch.
                if auto:
                    fix_result = run_autonomous(
                        repo=repo,
                        pid=pid,
                        host=host,
                        port=port,
                        build_cmd=build_cmd,
                        test_cmd=test_cmd,
                        model=model or "sonnet",
                        max_attempts=max_attempts,
                        conn_str=conn_str,
                    )
                else:
                    fix_result = run_handoff(
                        repo=repo,
                        pid=pid,
                        host=host,
                        port=port,
                        conn_str=conn_str,
                    )

                # Update dedup hash for stay-resident mode.
                if max_crashes > 1:
                    last_crash_hash = fix_result.crash_hash

                if max_crashes == 1:
                    break

            elif isinstance(watch_result, WatchTimedOut):
                log.info("watch timed out after %.1fs", watch_result.elapsed_s)
                return 0

            elif isinstance(watch_result, WatchTargetExited):
                log.info("target process exited cleanly after %.1fs", watch_result.elapsed_s)
                return 0

        return 0
    finally:
        if state.did_spawn_server and server_proc is not None:
            shutdown_server(server_proc)
