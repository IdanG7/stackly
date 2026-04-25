"""Tests for watch/dispatcher.py (tasks 2.5.2.1, 2.5.2.3).

One-shot watch path: MCP client plumbing, dispatch routing, and timeout/exit outcomes.
Stay-resident path: dedup, signal handlers, re-attach failure handling.
All external calls (ensure_server_running, _watch_once, run_handoff, run_autonomous,
httpx, streamablehttp_client, ClientSession) are monkeypatched — no real server needed.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackly.fix.models import CrashCapture, FixResult
from stackly.fix.worktree import compute_crash_hash
from stackly.models import (
    ExceptionInfo,
    WatchException,
    WatchTargetExited,
    WatchTimedOut,
)


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal directory to pass as the repo path (no git needed for these unit tests)."""
    return tmp_path


def _canned_fix_result() -> FixResult:
    return FixResult(ok=True, mode="handoff", crash_hash="abc12345")


def _canned_exception() -> WatchException:
    return WatchException(
        exception=ExceptionInfo(
            code=0xC0000005,
            code_name="EXCEPTION_ACCESS_VIOLATION",
            address=0,
            description="",
            is_first_chance=False,
            faulting_thread_tid=1,
        )
    )


def _fake_popen() -> subprocess.Popen:
    """Return a MagicMock that looks like a subprocess.Popen for did_spawn=True scenarios."""
    fake = MagicMock(spec=subprocess.Popen)
    fake.poll.return_value = None
    return fake


# ---------------------------------------------------------------------------
# Test 1: one-shot handoff dispatch on WatchException
# ---------------------------------------------------------------------------


def test_run_watch_one_shot_invokes_run_handoff_on_exception(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch with auto=False, max_crashes=1: on WatchException calls run_handoff once."""
    import stackly.watch.dispatcher as watch_dispatcher_module

    fake_proc = _fake_popen()

    # (a) ensure_gitignore — no-op
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_gitignore",
        lambda repo: None,
    )

    # (b) ensure_server_running — returns a fake Popen (we spawned it)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: fake_proc,
    )

    # (c) shutdown_server — no-op (we spawned, so finally will call it)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "shutdown_server",
        lambda proc, grace_s=5.0: None,
    )

    # (d) _watch_once — returns a canned WatchException
    canned_exc = _canned_exception()

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        return canned_exc

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    # (e) run_handoff — record call args, return canned FixResult
    handoff_calls: list[dict] = []

    def fake_run_handoff(**kwargs):
        handoff_calls.append(kwargs)
        return _canned_fix_result()

    monkeypatch.setattr(watch_dispatcher_module, "run_handoff", fake_run_handoff)

    # (f) run_autonomous — should NOT be called
    autonomous_calls: list[dict] = []

    def fake_run_autonomous(**kwargs):
        autonomous_calls.append(kwargs)
        return _canned_fix_result()

    monkeypatch.setattr(watch_dispatcher_module, "run_autonomous", fake_run_autonomous)

    from stackly.watch.dispatcher import run_watch

    result = run_watch(
        repo=tmp_repo,
        pid=1234,
        host="127.0.0.1",
        port=8585,
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

    assert result == 0, f"Expected return 0, got {result}"
    assert len(handoff_calls) == 1, f"run_handoff must be called once, got {len(handoff_calls)}"
    assert len(autonomous_calls) == 0, "run_autonomous must NOT be called"

    call = handoff_calls[0]
    assert call["repo"] == tmp_repo
    assert call["pid"] == 1234
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8585
    assert call["conn_str"] is None


# ---------------------------------------------------------------------------
# Test 2: one-shot autonomous dispatch on WatchException with auto=True
# ---------------------------------------------------------------------------


def test_run_watch_one_shot_invokes_run_autonomous_on_auto_flag(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch with auto=True, max_crashes=1: on WatchException calls run_autonomous once."""
    import stackly.watch.dispatcher as watch_dispatcher_module

    fake_proc = _fake_popen()

    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: fake_proc,
    )
    monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

    canned_exc = _canned_exception()

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        return canned_exc

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    handoff_calls: list[dict] = []

    def fake_run_handoff(**kwargs):
        handoff_calls.append(kwargs)
        return _canned_fix_result()

    monkeypatch.setattr(watch_dispatcher_module, "run_handoff", fake_run_handoff)

    autonomous_calls: list[dict] = []

    def fake_run_autonomous(**kwargs):
        autonomous_calls.append(kwargs)
        return FixResult(ok=True, mode="auto", crash_hash="abc12345")

    monkeypatch.setattr(watch_dispatcher_module, "run_autonomous", fake_run_autonomous)

    from stackly.watch.dispatcher import run_watch

    result = run_watch(
        repo=tmp_repo,
        pid=1234,
        host="127.0.0.1",
        port=8585,
        auto=True,
        build_cmd="make",
        test_cmd="make test",
        model="sonnet",
        max_attempts=2,
        conn_str=None,
        max_crashes=1,
        max_wait_minutes=None,
        quiet=True,
    )

    assert result == 0
    assert len(autonomous_calls) == 1, f"run_autonomous must be called once, got {len(autonomous_calls)}"
    assert len(handoff_calls) == 0, "run_handoff must NOT be called"

    call = autonomous_calls[0]
    assert call["repo"] == tmp_repo
    assert call["pid"] == 1234
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8585
    assert call["conn_str"] is None


# ---------------------------------------------------------------------------
# Test 3: WatchTargetExited → no dispatch, return 0
# ---------------------------------------------------------------------------


def test_run_watch_returns_0_on_target_exited(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch: when _watch_once returns WatchTargetExited, no dispatch and exit 0."""
    import stackly.watch.dispatcher as watch_dispatcher_module

    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: None,
    )
    monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        return WatchTargetExited(elapsed_s=5.0)

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    handoff_calls: list[dict] = []
    monkeypatch.setattr(
        watch_dispatcher_module, "run_handoff", lambda **kw: handoff_calls.append(kw) or _canned_fix_result()
    )

    autonomous_calls: list[dict] = []
    monkeypatch.setattr(
        watch_dispatcher_module,
        "run_autonomous",
        lambda **kw: autonomous_calls.append(kw) or FixResult(ok=True, mode="auto", crash_hash="x"),
    )

    from stackly.watch.dispatcher import run_watch

    result = run_watch(
        repo=tmp_repo,
        pid=9999,
        host="127.0.0.1",
        port=8585,
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

    assert result == 0
    assert len(handoff_calls) == 0, "run_handoff must NOT be called on target_exited"
    assert len(autonomous_calls) == 0, "run_autonomous must NOT be called on target_exited"


# ---------------------------------------------------------------------------
# Test 4: WatchTimedOut → no dispatch, return 0
# ---------------------------------------------------------------------------


def test_run_watch_returns_0_on_timed_out(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch: when _watch_once returns WatchTimedOut, no dispatch and exit 0."""
    import stackly.watch.dispatcher as watch_dispatcher_module

    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: None,
    )
    monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        return WatchTimedOut(elapsed_s=60.0)

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    handoff_calls: list[dict] = []
    monkeypatch.setattr(
        watch_dispatcher_module, "run_handoff", lambda **kw: handoff_calls.append(kw) or _canned_fix_result()
    )

    autonomous_calls: list[dict] = []
    monkeypatch.setattr(
        watch_dispatcher_module,
        "run_autonomous",
        lambda **kw: autonomous_calls.append(kw) or FixResult(ok=True, mode="auto", crash_hash="x"),
    )

    from stackly.watch.dispatcher import run_watch

    result = run_watch(
        repo=tmp_repo,
        pid=9999,
        host="127.0.0.1",
        port=8585,
        auto=False,
        build_cmd=None,
        test_cmd=None,
        model=None,
        max_attempts=3,
        conn_str=None,
        max_crashes=1,
        max_wait_minutes=1,
        quiet=True,
    )

    assert result == 0
    assert len(handoff_calls) == 0, "run_handoff must NOT be called on timed_out"
    assert len(autonomous_calls) == 0, "run_autonomous must NOT be called on timed_out"


# ---------------------------------------------------------------------------
# Test 5: _watch_once uses unbounded read timeout
# ---------------------------------------------------------------------------


def test_run_watch_uses_unbounded_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_watch_once must construct httpx.AsyncClient with Timeout(..., read=None)."""
    import httpx

    import stackly.watch.dispatcher as watch_dispatcher_module

    # Record httpx.AsyncClient constructor kwargs
    recorded_timeouts: list[httpx.Timeout] = []

    class RecordingAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            timeout = kwargs.get("timeout")
            if isinstance(timeout, httpx.Timeout):
                recorded_timeouts.append(timeout)
            super().__init__(**kwargs)

    monkeypatch.setattr(watch_dispatcher_module.httpx, "AsyncClient", RecordingAsyncClient)

    # Stub streamablehttp_client so it doesn't need a real server
    import contextlib

    @contextlib.asynccontextmanager
    async def fake_streamablehttp_client(url, **kwargs):
        # Yield fake (read, write, get_session_id) tuple
        read_stream = MagicMock()
        write_stream = MagicMock()
        yield read_stream, write_stream, lambda: None

    monkeypatch.setattr(
        watch_dispatcher_module,
        "streamablehttp_client",
        fake_streamablehttp_client,
    )

    # Stub ClientSession to return a mock that supports async context + calls
    class FakeClientSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name, args, **kwargs):
            result = MagicMock()
            # Return a structuredContent that parses as WatchTargetExited
            result.structuredContent = {"outcome": "target_exited", "elapsed_s": 1.0}
            return result

    monkeypatch.setattr(watch_dispatcher_module, "ClientSession", FakeClientSession)

    # Run _watch_once directly
    from stackly.watch.dispatcher import _watch_once

    outcome = asyncio.run(
        _watch_once(pid=1234, mcp_url="http://127.0.0.1:8585/mcp", poll_s=1, timeout_s=None, conn_str=None)
    )

    assert isinstance(outcome, WatchTargetExited), f"Expected WatchTargetExited, got {type(outcome)}"
    assert len(recorded_timeouts) == 1, f"httpx.AsyncClient must be constructed once, got {len(recorded_timeouts)}"

    timeout_obj = recorded_timeouts[0]
    assert timeout_obj.read is None, (
        f"httpx.AsyncClient Timeout must have read=None (unbounded), got read={timeout_obj.read}"
    )


# ---------------------------------------------------------------------------
# Test 6 (task 2.5.2.3): stay-resident dedup — same crash skips dispatch
# ---------------------------------------------------------------------------


def _compute_watch_exception_hash(exc: WatchException) -> str:
    """Compute the crash hash a WatchException would yield via compute_crash_hash.

    We build a minimal CrashCapture (no callstack) so the formula is:
    sha1("{code_name}@unknown!unknown")[:8].
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


def test_run_watch_stay_resident_dedups_duplicate_crashes(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stay-resident (max_crashes=3): second and third iterations see same hash — dispatch skipped.

    run_handoff must be called exactly ONCE (iteration 1).
    Subsequent iterations receive WatchException with the same ExceptionInfo;
    the watch dispatcher computes the same hash, matches last_crash_hash, and skips.
    """
    import logging

    import stackly.watch.dispatcher as watch_dispatcher_module

    canned_exc = _canned_exception()
    expected_hash = _compute_watch_exception_hash(canned_exc)

    fake_proc = _fake_popen()
    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: fake_proc,
    )
    monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

    # _watch_once always returns the same WatchException
    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        return canned_exc

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    # run_handoff returns a FixResult whose crash_hash matches the computed hash
    handoff_calls: list[dict] = []

    def fake_run_handoff(**kwargs):
        handoff_calls.append(kwargs)
        return FixResult(ok=True, mode="handoff", crash_hash=expected_hash)

    monkeypatch.setattr(watch_dispatcher_module, "run_handoff", fake_run_handoff)

    autonomous_calls: list[dict] = []

    def fake_run_autonomous(**kwargs):
        autonomous_calls.append(kwargs)
        return FixResult(ok=True, mode="auto", crash_hash=expected_hash)

    monkeypatch.setattr(watch_dispatcher_module, "run_autonomous", fake_run_autonomous)

    from stackly.watch.dispatcher import run_watch

    with caplog.at_level(logging.INFO, logger="stackly.watch.dispatcher"):
        result = run_watch(
            repo=tmp_repo,
            pid=1234,
            host="127.0.0.1",
            port=8585,
            auto=False,
            build_cmd=None,
            test_cmd=None,
            model=None,
            max_attempts=3,
            conn_str=None,
            max_crashes=3,
            max_wait_minutes=None,
            quiet=True,
        )

    assert result == 0, f"Expected return 0, got {result}"
    assert len(handoff_calls) == 1, (
        f"run_handoff must be called exactly once (dedup skips iterations 2+), "
        f"got {len(handoff_calls)}"
    )
    assert len(autonomous_calls) == 0, "run_autonomous must NOT be called"

    # Check log contains the dedup message for the skipped iterations
    dedup_logs = [r for r in caplog.records if "already seen this crash" in r.message]
    assert len(dedup_logs) >= 1, (
        f"Expected at least one 'already seen this crash' log record; "
        f"got records: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 7 (task 2.5.2.3): SIGINT handler — detach via MCP + SystemExit(130)
# ---------------------------------------------------------------------------


def test_run_watch_sigint_handler_detaches_and_exits_130(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installing the watch signal handler then invoking it:
    - Calls detach_process via MCP (best-effort).
    - Raises SystemExit(130).
    - Is idempotent (second call is a no-op, no double-detach).
    """
    import stackly.watch.dispatcher as watch_dispatcher_module
    from stackly.watch.dispatcher import _install_watch_signal_handlers, _WatchState

    # Track MCP detach calls — we monkeypatch the async detach helper used by the handler.
    detach_calls: list[str] = []

    async def fake_detach_via_mcp(mcp_url: str) -> None:
        detach_calls.append(mcp_url)

    monkeypatch.setattr(watch_dispatcher_module, "_detach_via_mcp", fake_detach_via_mcp)

    mcp_url = "http://127.0.0.1:8585/mcp"

    # Create a state with no server spawned and no claude_proc
    state = _WatchState(
        claude_proc=None,
        server_proc=None,
        did_spawn_server=False,
    )

    _install_watch_signal_handlers(state, mcp_url)

    # Retrieve the installed handler and invoke it directly
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler), "SIGINT handler must be callable"

    with pytest.raises(SystemExit) as exc_info:
        handler(signal.SIGINT, None)

    assert exc_info.value.code == 130, (
        f"SystemExit must have code 130, got {exc_info.value.code}"
    )
    assert len(detach_calls) == 1, (
        f"detach_via_mcp must be called once; got {len(detach_calls)}"
    )
    assert detach_calls[0] == mcp_url

    # Idempotency: second call must be a no-op (no exception, no extra detach)
    handler(signal.SIGINT, None)
    assert len(detach_calls) == 1, (
        "Handler is not idempotent: detach was called a second time"
    )


# ---------------------------------------------------------------------------
# Test 8 (task 2.5.2.3): stay-resident exits cleanly on re-attach failure
# ---------------------------------------------------------------------------


def test_run_watch_stay_resident_exits_on_reattach_failure(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stay-resident (max_crashes=3): iteration 1 succeeds; iteration 2 raises
    AttachFailedError — run_watch must log 'target no longer attachable' and return 0
    without a third iteration.
    """
    import logging

    import stackly.watch.dispatcher as watch_dispatcher_module
    from stackly.watch.dispatcher import AttachFailedError

    canned_exc = _canned_exception()
    fake_proc = _fake_popen()
    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: fake_proc,
    )
    monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

    # _watch_once: first call succeeds (returns WatchException), second raises AttachFailedError
    call_count = 0

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return canned_exc
        raise AttachFailedError("attach failed: target is dead")

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    handoff_calls: list[dict] = []

    def fake_run_handoff(**kwargs):
        handoff_calls.append(kwargs)
        expected_hash = _compute_watch_exception_hash(canned_exc)
        return FixResult(ok=True, mode="handoff", crash_hash=expected_hash)

    monkeypatch.setattr(watch_dispatcher_module, "run_handoff", fake_run_handoff)

    from stackly.watch.dispatcher import run_watch

    with caplog.at_level(logging.INFO, logger="stackly.watch.dispatcher"):
        result = run_watch(
            repo=tmp_repo,
            pid=1234,
            host="127.0.0.1",
            port=8585,
            auto=False,
            build_cmd=None,
            test_cmd=None,
            model=None,
            max_attempts=3,
            conn_str=None,
            max_crashes=3,
            max_wait_minutes=None,
            quiet=True,
        )

    assert result == 0, f"Expected return 0, got {result}"

    # Only iteration 1 dispatched; iteration 2 raised AttachFailedError before dispatch
    assert len(handoff_calls) == 1, (
        f"run_handoff must be called exactly once (reattach failed on iteration 2), "
        f"got {len(handoff_calls)}"
    )
    assert call_count == 2, f"_watch_once must have been called exactly 2 times, got {call_count}"

    # Log must contain the re-attach failure message
    attach_fail_logs = [r for r in caplog.records if "target no longer attachable" in r.message]
    assert len(attach_fail_logs) >= 1, (
        f"Expected 'target no longer attachable' log; "
        f"got records: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 9 (task 2.5.3.2): quiet flag suppresses Rich Progress spinner
# ---------------------------------------------------------------------------


def test_run_watch_quiet_flag_suppresses_spinner(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch(quiet=True) must NOT instantiate rich.progress.Progress.
    run_watch(quiet=False) MUST instantiate rich.progress.Progress.
    """
    import stackly.watch.dispatcher as watch_dispatcher_module

    # --- shared mock setup helpers ---

    def _setup_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
        monkeypatch.setattr(
            watch_dispatcher_module,
            "ensure_server_running",
            lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: None,
        )
        monkeypatch.setattr(watch_dispatcher_module, "shutdown_server", lambda proc, grace_s=5.0: None)

        async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
            return WatchTargetExited(elapsed_s=0.1)

        monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    # Track Progress instantiations
    progress_init_calls: list[bool] = []

    import rich.progress as rich_progress_module

    _original_progress_init = rich_progress_module.Progress.__init__

    def recording_progress_init(self, *args, **kwargs):
        progress_init_calls.append(True)
        _original_progress_init(self, *args, **kwargs)

    monkeypatch.setattr(
        watch_dispatcher_module.Progress,
        "__init__",
        recording_progress_init,
    )

    from stackly.watch.dispatcher import run_watch

    # --- quiet=True: Progress must NOT be instantiated ---
    _setup_mocks(monkeypatch)
    progress_init_calls.clear()

    result = run_watch(
        repo=tmp_repo,
        pid=1234,
        host="127.0.0.1",
        port=8585,
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

    assert result == 0
    assert len(progress_init_calls) == 0, (
        f"quiet=True: Progress must NOT be instantiated, but was called {len(progress_init_calls)} time(s)"
    )

    # --- quiet=False: Progress MUST be instantiated ---
    _setup_mocks(monkeypatch)
    progress_init_calls.clear()

    result = run_watch(
        repo=tmp_repo,
        pid=1234,
        host="127.0.0.1",
        port=8585,
        auto=False,
        build_cmd=None,
        test_cmd=None,
        model=None,
        max_attempts=3,
        conn_str=None,
        max_crashes=1,
        max_wait_minutes=None,
        quiet=False,
    )

    assert result == 0
    assert len(progress_init_calls) >= 1, (
        f"quiet=False: Progress MUST be instantiated at least once, but was called {len(progress_init_calls)} time(s)"
    )


# ---------------------------------------------------------------------------
# Test 10: --poll-seconds CLI flag is threaded through to watch_for_crash
#         Regression guard for Codex review P2.
# ---------------------------------------------------------------------------


def test_run_watch_threads_poll_seconds_into_watch_once(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_watch(poll_seconds=N) must pass poll_s=N to _watch_once.

    Previously the dispatcher hardcoded poll_s=1, silently ignoring the CLI's
    ``--poll-seconds`` flag (Codex review P2). This test pins that behavior:
    whatever poll_seconds the caller supplies must reach the inner coroutine.
    """
    import stackly.watch.dispatcher as watch_dispatcher_module

    fake_proc = _fake_popen()

    monkeypatch.setattr(watch_dispatcher_module, "ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(
        watch_dispatcher_module,
        "ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: fake_proc,
    )
    monkeypatch.setattr(
        watch_dispatcher_module,
        "shutdown_server",
        lambda proc, grace_s=5.0: None,
    )
    monkeypatch.setattr(
        watch_dispatcher_module,
        "_install_watch_signal_handlers",
        lambda state, mcp_url: None,
    )

    observed: dict = {}

    async def fake_watch_once(pid, mcp_url, poll_s, timeout_s, conn_str):
        observed["poll_s"] = poll_s
        from stackly.models import WatchTargetExited

        return WatchTargetExited(elapsed_s=0.0)

    monkeypatch.setattr(watch_dispatcher_module, "_watch_once", fake_watch_once)

    from stackly.watch.dispatcher import run_watch

    result = run_watch(
        repo=tmp_repo,
        pid=1234,
        host="127.0.0.1",
        port=8585,
        auto=False,
        build_cmd=None,
        test_cmd=None,
        model=None,
        max_attempts=3,
        conn_str=None,
        max_crashes=1,
        max_wait_minutes=None,
        quiet=True,
        poll_seconds=7,
    )

    assert result == 0
    assert observed.get("poll_s") == 7, (
        f"Expected _watch_once to receive poll_s=7 (from --poll-seconds CLI flag); "
        f"got poll_s={observed.get('poll_s')!r}. Regression on Codex P2."
    )


# ---------------------------------------------------------------------------
# Test 11: SIGINT handler runs detach in a background thread (no nested
#         asyncio.run on the main thread).
#         Regression guard for Codex review P1.
# ---------------------------------------------------------------------------


def test_sigint_handler_runs_detach_in_background_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler must NOT call asyncio.run on the main thread.

    Before the P1 fix, the handler called ``asyncio.run(_detach_via_mcp(...))``
    synchronously. Because ``run_watch`` is already inside an active
    ``asyncio.run(_watch_once(...))`` on the same thread, the nested call
    raised ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``, silently breaking the best-effort detach.

    After the fix, the handler delegates to ``_detach_in_background_thread``
    which spawns a fresh thread with its own loop. This test pins that:
    calling the installed handler must invoke the background-thread helper
    (not ``asyncio.run`` directly).
    """
    import stackly.watch.dispatcher as watch_dispatcher_module
    from stackly.watch.dispatcher import (
        _install_watch_signal_handlers,
        _WatchState,
    )

    state = _WatchState()

    # Record whether the background-thread helper was called.
    bg_calls: list[tuple[str, float]] = []

    def fake_bg(mcp_url: str, timeout_s: float = 5.0) -> None:
        bg_calls.append((mcp_url, timeout_s))

    monkeypatch.setattr(
        watch_dispatcher_module,
        "_detach_in_background_thread",
        fake_bg,
    )

    # Record whether asyncio.run was called directly (it MUST NOT be).
    asyncio_run_calls: list[object] = []
    real_asyncio_run = watch_dispatcher_module.asyncio.run

    def spy_asyncio_run(*args, **kwargs):
        asyncio_run_calls.append(args)
        return real_asyncio_run(*args, **kwargs)

    monkeypatch.setattr(watch_dispatcher_module.asyncio, "run", spy_asyncio_run)

    # Shutdown_server must not raise in this test environment.
    monkeypatch.setattr(
        watch_dispatcher_module,
        "shutdown_server",
        lambda proc, grace_s=5.0: None,
    )

    import signal as signal_mod

    _install_watch_signal_handlers(state, "http://127.0.0.1:9999/mcp")

    handler = signal_mod.getsignal(signal_mod.SIGINT)
    assert callable(handler), "SIGINT handler must be installed"

    # Invoke the handler directly (would be SIGINT in real life).
    with pytest.raises(SystemExit) as exit_info:
        handler(signal_mod.SIGINT, None)

    assert exit_info.value.code == 130

    # The background-thread helper MUST have been called.
    assert len(bg_calls) == 1, (
        f"Expected _detach_in_background_thread to be invoked once; got {len(bg_calls)}. "
        f"Regression on Codex P1."
    )
    assert bg_calls[0][0] == "http://127.0.0.1:9999/mcp"

    # asyncio.run MUST NOT have been called on the main thread by the handler.
    assert asyncio_run_calls == [], (
        f"SIGINT handler called asyncio.run directly {len(asyncio_run_calls)} time(s); "
        f"this is the exact bug Codex P1 flagged. Detach must run in a fresh thread."
    )
