"""Integration tests — real DbgEng, real process, real pybag.

Two test modes:

1. **Attach to running** — launches crash_app in ``wait`` mode (blocked on
   stdin), attaches with DebugSession, inspects threads and call stack.
   Verifies the attach / query path without needing a live exception.

2. **Catch a crash** — uses pybag's ``UserDbg.create()`` to launch crash_app
   *under* the debugger so we deterministically catch its crash on the next
   ``go()``. This is how the real product will behave when the user attaches
   *before* the crash; the only difference vs attach-then-crash is timing,
   not debug state.
"""

from __future__ import annotations

import contextlib
import subprocess

import pytest

from stackly.session import DebugSession, DebugSessionError


@pytest.mark.integration
def test_attach_local_to_waiting_process(crash_app_waiting: subprocess.Popen) -> None:
    session = DebugSession()
    try:
        result = session.attach_local(crash_app_waiting.pid)
        assert result.status == "attached", f"attach failed: {result.message}"
        assert result.pid == crash_app_waiting.pid
        assert result.is_remote is False
        # Process name is best-effort; if DbgEng's enumeration listed us we
        # should see "crash_app.exe" — but don't fail the attach test on it.
        if result.process_name:
            assert "crash_app" in result.process_name.lower()
    finally:
        session.close()


@pytest.mark.integration
def test_get_threads_returns_at_least_main(crash_app_waiting: subprocess.Popen) -> None:
    session = DebugSession()
    try:
        attach = session.attach_local(crash_app_waiting.pid)
        assert attach.status == "attached"
        threads = session.get_threads()
        assert len(threads) >= 1
        assert any(t.is_current for t in threads)
        assert all(t.tid > 0 for t in threads)
    finally:
        session.close()


@pytest.mark.integration
def test_get_callstack_returns_frames(crash_app_waiting: subprocess.Popen) -> None:
    """The waiting process is blocked inside fgets; we should see *some* stack."""
    session = DebugSession()
    try:
        attach = session.attach_local(crash_app_waiting.pid)
        assert attach.status == "attached"
        frames = session.get_callstack(max_frames=32)
        assert len(frames) >= 1
        # Top frame should be a valid address inside some module.
        top = frames[0]
        assert top.instruction_pointer > 0 or top.function is not None
    finally:
        session.close()


@pytest.mark.integration
def test_catch_null_deref_crash_via_create(crash_app_path) -> None:
    """Launch crash_app under the debugger and catch the null-deref crash.

    This exercises the Tier A crash-triage path end-to-end:
    attach → run → crash → get_exception → get_callstack.

    We reach through to pybag's ``UserDbg.create()`` directly here because
    Stackly's public API is about attaching to existing processes; creating
    under debugger control is test-only.
    """
    from stackly.env import ensure_dbgeng_on_path

    ensure_dbgeng_on_path()
    from pybag.userdbg import UserDbg

    dbg = UserDbg()
    # Use CreateProcess (pybag's `.create`) — initial_break lets us attach
    # before main runs.
    dbg.create(f'"{crash_app_path}" null', initial_break=True)

    # Now drive the process through a DebugSession so we test our own code path.
    session = DebugSession()
    session._dbg = dbg  # type: ignore[attr-defined]  # hand-off for test only
    session._process_name = "crash_app.exe"  # type: ignore[attr-defined]
    try:
        # Resume — will crash on the null deref.
        dbg.go()

        exc = session.get_exception()
        assert exc is not None, "expected an exception after the crash"
        assert exc.code == 0xC0000005, f"got {exc.code:#x}, expected 0xC0000005"
        assert exc.code_name == "EXCEPTION_ACCESS_VIOLATION"

        frames = session.get_callstack(max_frames=32)
        assert len(frames) >= 1
        # crash_null should appear somewhere in the top few frames.
        top_funcs = " ".join(f.function or "" for f in frames[:5])
        assert "crash_null" in top_funcs, f"crash_null not in top frames: {top_funcs}"
    finally:
        with contextlib.suppress(Exception):
            dbg.terminate()
        session._dbg = None  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            dbg.Release()


@pytest.mark.integration
def test_detach_process_releases_target(crash_app_waiting: subprocess.Popen) -> None:
    """detach() must release pybag from the target without killing the session.

    Proves the cleanup gap surfaced by Phase 2a research is closed:
    after detach() the session is back to its pre-attach state, query
    tools fail with "Not attached", and a subsequent attach_local works.
    """
    session = DebugSession()
    try:
        attach = session.attach_local(crash_app_waiting.pid)
        assert attach.status == "attached", f"initial attach failed: {attach.message}"
        assert session._dbg is not None  # type: ignore[attr-defined]

        session.detach()
        assert session._dbg is None  # type: ignore[attr-defined]

        with pytest.raises(DebugSessionError, match="Not attached"):
            session.get_callstack()

        # Re-attach after detach — proves detach() did not leave torn state.
        reattach = session.attach_local(crash_app_waiting.pid)
        assert reattach.status == "attached", f"re-attach failed: {reattach.message}"
    finally:
        session.close()
