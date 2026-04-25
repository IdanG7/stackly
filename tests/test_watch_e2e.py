"""End-to-end integration test for the watch → dispatch pipeline (task 2.5.3.1).

Approach
--------
We use **Option A (mock-pybag variant)**: spawn a real ``stackly serve``
subprocess on an ephemeral port, monkeypatch ``_watch_once`` to return a
canned ``WatchException`` (avoiding pybag / Debugging-Tools dependency), and
monkeypatch ``run_handoff`` to record invocation kwargs without launching claude.

Why not a fully-live crash:
    ``crash_app null`` crashes in microseconds — before the MCP attach can
    complete.  ``crash_app wait`` exits cleanly on stdin close, so it does not
    fire a catchable exception.  Triggering a genuine Windows exception from
    outside the process (e.g. via ``CTRL_BREAK``) is unreliable across CI
    environments.  The mock-pybag approach still exercises every production code
    path except the inner pybag polling loop, which is already covered by
    ``test_watch_session.py`` and ``test_watch_dispatcher.py``.

Markers:
    @pytest.mark.integration  - auto-skips when crash_app is absent or Debugging
                                Tools are not installed (conftest gate).
    @pytest.mark.slow          - opt-in only: ``pytest -m "integration and slow"``.

What this test proves:
    1. A real ``stackly serve`` can be started and the ephemeral port becomes
       active within the startup timeout.
    2. ``run_watch`` (called from a background thread) finds the live server,
       calls the (mocked) ``_watch_once``, receives a ``WatchException``, and
       dispatches to ``run_handoff``.
    3. ``run_handoff`` is called with the exact ``repo``, ``pid``, ``host``,
       ``port``, and ``conn_str`` kwargs the caller passed to ``run_watch``.
    4. ``run_watch`` returns 0.
    5. Everything completes within 30 seconds.
"""

from __future__ import annotations

import os
import queue
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Bind to port 0 to get an OS-assigned free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout_s: float = 20.0) -> bool:
    """Poll until (host, port) accepts TCP connections or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, TimeoutError):
            time.sleep(0.2)
    return False


def _make_tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository in tmp_path suitable for run_watch."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_watch_dispatches_run_handoff_against_live_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch → dispatch wire-up against a live stackly serve subprocess.

    The test monkeypatches two callables inside ``stackly.watch.dispatcher``:
    - ``_watch_once``: returns a canned ``WatchException`` so we don't need pybag.
    - ``run_handoff``: records its kwargs and returns a canned ``FixResult`` so
      we don't need a real claude session.

    Everything else is real: the server subprocess, port-readiness polling,
    ``run_watch``'s entry-point call, ``ensure_server_running`` finding the
    port already active (returns None), and the dispatch routing logic.
    """
    # --- Imports needed for the canned objects ---
    import stackly.watch.dispatcher as watch_dispatcher_module
    from stackly.fix.models import FixResult
    from stackly.models import ExceptionInfo, WatchException

    # --- Step 1: spin up a real stackly serve on an ephemeral port ---
    host = "127.0.0.1"
    port = _find_free_port()

    root = Path(__file__).resolve().parent.parent
    server_proc = subprocess.Popen(
        ["uv", "run", "stackly", "serve", "--host", host, "--port", str(port)],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    try:
        # Wait until the server is ready (port active).
        assert _wait_for_port(host, port, timeout_s=20.0), (
            f"stackly serve did not open port {port} within 20s"
        )

        # --- Step 2: set up a minimal git repo ---
        repo = _make_tmp_git_repo(tmp_path)

        # --- Step 3: monkeypatch _install_watch_signal_handlers → no-op ---
        # signal.signal() only works in the main thread; since run_watch runs in
        # a background thread in this test, we skip signal-handler installation.
        # Signal-handler behaviour is already covered by test_watch_dispatcher.py.
        monkeypatch.setattr(
            watch_dispatcher_module,
            "_install_watch_signal_handlers",
            lambda state, mcp_url: None,
        )

        # --- Step 5: monkeypatch _watch_once → canned WatchException ---
        canned_exception = WatchException(
            exception=ExceptionInfo(
                code=0xC0000005,
                code_name="EXCEPTION_ACCESS_VIOLATION",
                address=0xDEADBEEF,
                description="access violation",
                is_first_chance=False,
                faulting_thread_tid=1,
            )
        )

        async def fake_watch_once(
            pid: int,
            mcp_url: str,
            poll_s: int,
            timeout_s: int | None,
            conn_str: str | None,
        ) -> WatchException:
            # Simulate a brief detection delay so run_watch enters the loop.
            import asyncio
            await asyncio.sleep(0.05)
            return canned_exception

        monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

        # --- Step 6: monkeypatch run_handoff → record kwargs, return FixResult ---
        handoff_calls: list[dict] = []

        def fake_run_handoff(**kwargs: object) -> FixResult:
            handoff_calls.append(dict(kwargs))
            return FixResult(ok=True, mode="handoff", crash_hash="abc12345")

        monkeypatch.setattr(watch_dispatcher_module, "run_handoff", fake_run_handoff)

        # --- Step 7: call run_watch in a background thread ---
        # Use a queue to capture the return value from the thread.
        result_queue: queue.Queue[int | Exception] = queue.Queue()
        fake_pid = 99999  # arbitrary — _watch_once is mocked

        def _run_watch_thread() -> None:
            from stackly.watch.dispatcher import run_watch

            try:
                rc = run_watch(
                    repo=repo,
                    pid=fake_pid,
                    host=host,
                    port=port,
                    auto=False,
                    build_cmd=None,
                    test_cmd=None,
                    model=None,
                    max_attempts=3,
                    conn_str=None,
                    max_crashes=1,
                    max_wait_minutes=None,
                    quiet=True,
                )
                result_queue.put(rc)
            except Exception as exc:
                result_queue.put(exc)

        watcher_thread = threading.Thread(target=_run_watch_thread, daemon=True)
        watcher_thread.start()

        # --- Step 8: collect result with generous timeout ---
        watcher_thread.join(timeout=30.0)
        assert not watcher_thread.is_alive(), (
            "run_watch thread did not complete within 30 s"
        )

        result = result_queue.get_nowait()
        if isinstance(result, Exception):
            raise AssertionError(f"run_watch raised an exception: {result!r}") from result

        # --- Step 9: assertions ---
        assert result == 0, f"run_watch returned {result!r}, expected 0"

        assert len(handoff_calls) == 1, (
            f"run_handoff must be called exactly once; got {len(handoff_calls)} call(s)"
        )
        call = handoff_calls[0]
        assert call["repo"] == repo, f"wrong repo: {call['repo']!r}"
        assert call["pid"] == fake_pid, f"wrong pid: {call['pid']!r}"
        assert call["host"] == host, f"wrong host: {call['host']!r}"
        assert call["port"] == port, f"wrong port: {call['port']!r}"
        assert call["conn_str"] is None, f"expected conn_str=None, got {call['conn_str']!r}"

    finally:
        # --- Teardown: shut down the server ---
        if os.name == "nt":
            try:
                server_proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                server_proc.kill()
        else:
            server_proc.terminate()
        try:
            server_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=3)
